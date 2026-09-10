import streamlit as st
import pandas as pd
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
import datetime
import calendar
import hashlib
import os
import io
import json
import zipfile
import html
from fpdf import FPDF

# =================================================================
# 1. CONFIGURAÇÃO DA PÁGINA
# =================================================================
st.set_page_config(page_title="Hospital HELP — Escala de Radiologia", layout="wide", page_icon="🩻", initial_sidebar_state="expanded")

TURNOS = ["Manhã", "Tarde", "Noite"]
MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
         "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
DIAS_SEMANA = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
DIAS_SEMANA_CURTO = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

# =================================================================
# 2. BANCO — POOL DE CONEXÕES + TRANSAÇÕES
# =================================================================

def _database_url():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        try:
            db_url = st.secrets["DATABASE_URL"]
        except Exception:
            pass
    if not db_url:
        st.error("DATABASE_URL ausente nas configurações (Secrets/Environment).")
        st.stop()
    return db_url


@st.cache_resource
def get_db_pool():
    return ThreadedConnectionPool(
        minconn=1,
        maxconn=8,
        dsn=_database_url(),
        options="-c client_encoding=utf8",
        connect_timeout=10,
    )


def _with_connection(callback, transactional=False):
    """Executa callback(conn) com uma conexão exclusiva do pool e 1 retry."""
    last_exc = None
    for attempt in range(2):
        pool = get_db_pool()
        conn = None
        devolvida = False
        try:
            conn = pool.getconn()
            conn.autocommit = not transactional
            result = callback(conn)
            if transactional:
                conn.commit()
            return result
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            last_exc = exc
            if conn is not None:
                try:
                    pool.putconn(conn, close=True)
                    devolvida = True
                except Exception:
                    pass
            if attempt == 0:
                get_db_pool.clear()
                continue
            raise
        except Exception:
            if transactional and conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None and not devolvida:
                try:
                    if not conn.closed:
                        conn.autocommit = True
                    pool.putconn(conn)
                except Exception:
                    pass
    if last_exc:
        raise last_exc


def execute_query(query: str, params=None) -> None:
    def _exec(conn):
        with conn.cursor() as cur:
            cur.execute(query, params)
    _with_connection(_exec, transactional=False)
    st.cache_data.clear()


def execute_values_query(query: str, rows: list) -> None:
    if not rows:
        return
    def _exec(conn):
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, query, rows)
    _with_connection(_exec, transactional=False)
    st.cache_data.clear()


def execute_transacional(operacoes: list) -> None:
    """Lista de (query, params). params=list => execute_values."""
    def _exec(conn):
        with conn.cursor() as cur:
            for query, params in operacoes:
                if isinstance(params, list):
                    if params:
                        psycopg2.extras.execute_values(cur, query, params)
                else:
                    cur.execute(query, params)
    _with_connection(_exec, transactional=True)
    st.cache_data.clear()


def fetch_data(query: str, params=None) -> pd.DataFrame:
    def _fetch(conn):
        with conn.cursor() as cur:
            cur.execute(query, params)
            if not cur.description:
                return pd.DataFrame()
            columns = [desc[0] for desc in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=columns)
    return _with_connection(_fetch, transactional=False)


def _add_constraint_if_missing(table, name, definition):
    execute_query(f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN
            ALTER TABLE {table} ADD CONSTRAINT {name} {definition};
        END IF;
    END $$;
    """)


@st.cache_resource(show_spinner=False)
def init_db():
    # Estrutura compatível com instalações antigas.
    execute_query("""
        CREATE TABLE IF NOT EXISTS doctors (
            id BIGSERIAL UNIQUE,
            name TEXT PRIMARY KEY,
            ativo BOOLEAN DEFAULT TRUE
        );
        CREATE TABLE IF NOT EXISTS shift_schedule (
            shift_date DATE,
            shift_time VARCHAR(10),
            doctor_name TEXT,
            PRIMARY KEY(shift_date, shift_time)
        );
        CREATE TABLE IF NOT EXISTS fixed_schedule_4w (
            week_num INT,
            weekday INT,
            shift_time VARCHAR(10),
            doctor_name TEXT,
            PRIMARY KEY(week_num, weekday, shift_time)
        );
    """)

    execute_query("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS id BIGSERIAL;")
    execute_query("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS ativo BOOLEAN DEFAULT TRUE;")

    # Migra ativo INTEGER antigo -> BOOLEAN sem quebrar instalações já existentes.
    execute_query("""
    DO $$
    DECLARE tipo_col TEXT;
    BEGIN
        SELECT data_type INTO tipo_col
        FROM information_schema.columns
        WHERE table_name='doctors' AND column_name='ativo' AND table_schema=current_schema();
        IF tipo_col IN ('integer', 'smallint', 'bigint') THEN
            ALTER TABLE doctors ALTER COLUMN ativo DROP DEFAULT;
            ALTER TABLE doctors ALTER COLUMN ativo TYPE BOOLEAN USING (ativo <> 0);
            ALTER TABLE doctors ALTER COLUMN ativo SET DEFAULT TRUE;
        END IF;
    END $$;
    """)
    execute_query("UPDATE doctors SET ativo = TRUE WHERE ativo IS NULL;")

    _add_constraint_if_missing("doctors", "uq_doctors_id", "UNIQUE (id)")

    execute_query("ALTER TABLE shift_schedule ADD COLUMN IF NOT EXISTS doctor_id BIGINT;")
    execute_query("ALTER TABLE fixed_schedule_4w ADD COLUMN IF NOT EXISTS doctor_id BIGINT;")

    # Se o legado tiver nomes na escala que não estão mais em doctors, preserva-os como inativos.
    execute_query("""
        INSERT INTO doctors (name, ativo)
        SELECT DISTINCT doctor_name, FALSE
        FROM shift_schedule
        WHERE doctor_name IS NOT NULL AND BTRIM(doctor_name) <> ''
        ON CONFLICT (name) DO NOTHING;
    """)
    execute_query("""
        INSERT INTO doctors (name, ativo)
        SELECT DISTINCT doctor_name, FALSE
        FROM fixed_schedule_4w
        WHERE doctor_name IS NOT NULL AND BTRIM(doctor_name) <> ''
        ON CONFLICT (name) DO NOTHING;
    """)
    execute_query("""
        UPDATE shift_schedule s SET doctor_id = d.id
        FROM doctors d
        WHERE s.doctor_id IS NULL AND s.doctor_name = d.name;
    """)
    execute_query("""
        UPDATE fixed_schedule_4w s SET doctor_id = d.id
        FROM doctors d
        WHERE s.doctor_id IS NULL AND s.doctor_name = d.name;
    """)

    _add_constraint_if_missing("shift_schedule", "ck_shift_schedule_turno",
                               "CHECK (shift_time IN ('Manhã','Tarde','Noite'))")
    _add_constraint_if_missing("fixed_schedule_4w", "ck_fixed_turno",
                               "CHECK (shift_time IN ('Manhã','Tarde','Noite'))")
    _add_constraint_if_missing("fixed_schedule_4w", "ck_fixed_week",
                               "CHECK (week_num BETWEEN 0 AND 3)")
    _add_constraint_if_missing("fixed_schedule_4w", "ck_fixed_weekday",
                               "CHECK (weekday BETWEEN 0 AND 6)")
    _add_constraint_if_missing("shift_schedule", "fk_shift_doctor",
                               "FOREIGN KEY (doctor_id) REFERENCES doctors(id)")
    _add_constraint_if_missing("fixed_schedule_4w", "fk_fixed_doctor",
                               "FOREIGN KEY (doctor_id) REFERENCES doctors(id)")

    execute_query("CREATE INDEX IF NOT EXISTS idx_shift_schedule_date ON shift_schedule(shift_date);")
    execute_query("CREATE INDEX IF NOT EXISTS idx_shift_schedule_doctor ON shift_schedule(doctor_id, shift_date);")

    # Turnos e valores deixam de ser hardcoded.
    execute_query("""
        CREATE TABLE IF NOT EXISTS shift_types (
            name VARCHAR(10) PRIMARY KEY,
            start_time TIME NOT NULL,
            end_time TIME NOT NULL,
            value NUMERIC(12,2) NOT NULL CHECK (value >= 0)
        );
    """)
    execute_query("""
        INSERT INTO shift_types (name, start_time, end_time, value) VALUES
        ('Manhã', '07:00', '13:00', 750),
        ('Tarde', '13:00', '19:00', 750),
        ('Noite', '19:00', '07:00', 1500)
        ON CONFLICT (name) DO NOTHING;
    """)

    execute_query("""
        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    hoje = datetime.date.today()
    ancora_default = hoje - datetime.timedelta(days=hoje.weekday())
    execute_query("""
        INSERT INTO app_config (key, value)
        VALUES ('rotation_anchor_date', %s)
        ON CONFLICT (key) DO NOTHING;
    """, (ancora_default.isoformat(),))
    return True


try:
    init_db()
except Exception as e:
    st.error("🚨 Falha Crítica: Banco de Dados Inacessível.")
    st.error("O Supabase pode estar pausado ou a variável DATABASE_URL está incorreta.")
    st.code(str(e))
    st.stop()

# =================================================================
# 3. AUXILIARES DE DOMÍNIO
# =================================================================

def month_bounds(ano, mes):
    ini = datetime.date(ano, mes, 1)
    if mes == 12:
        fim = datetime.date(ano + 1, 1, 1)
    else:
        fim = datetime.date(ano, mes + 1, 1)
    return ini, fim


@st.cache_data(ttl=30, show_spinner=False)
def fetch_doctors():
    return fetch_data("SELECT id, name, ativo FROM doctors ORDER BY ativo DESC, name")


@st.cache_data(ttl=60, show_spinner=False)
def fetch_fixed_pattern():
    return fetch_data("""
        SELECT f.week_num, f.weekday, f.shift_time, f.doctor_id,
               COALESCE(d.name, f.doctor_name) AS doctor_name
        FROM fixed_schedule_4w f
        LEFT JOIN doctors d ON d.id = f.doctor_id
        WHERE f.doctor_id IS NOT NULL
        ORDER BY f.week_num, f.weekday, f.shift_time
    """)


@st.cache_data(ttl=20, show_spinner=False)
def fetch_month_schedule(ano, mes):
    ini, fim = month_bounds(ano, mes)
    return fetch_data("""
        SELECT s.shift_date, s.shift_time, s.doctor_id,
               COALESCE(d.name, s.doctor_name) AS doctor_name
        FROM shift_schedule s
        LEFT JOIN doctors d ON d.id = s.doctor_id
        WHERE s.shift_date >= %s AND s.shift_date < %s
        ORDER BY s.shift_date, s.shift_time
    """, (ini, fim))


@st.cache_data(ttl=300, show_spinner=False)
def get_shift_types():
    df = fetch_data("SELECT name, start_time, end_time, value FROM shift_types ORDER BY CASE name WHEN 'Manhã' THEN 1 WHEN 'Tarde' THEN 2 ELSE 3 END")
    if df.empty:
        return pd.DataFrame(columns=['name', 'start_time', 'end_time', 'value'])
    df['value'] = df['value'].astype(float)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def get_rotation_anchor():
    df = fetch_data("SELECT value FROM app_config WHERE key='rotation_anchor_date'")
    if df.empty:
        hoje = datetime.date.today()
        return hoje - datetime.timedelta(days=hoje.weekday())
    return datetime.date.fromisoformat(str(df.iloc[0]['value']))


def set_rotation_anchor(data):
    monday = data - datetime.timedelta(days=data.weekday())
    execute_query("""
        INSERT INTO app_config (key, value) VALUES ('rotation_anchor_date', %s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
    """, (monday.isoformat(),))
    return monday


def cycle_week_for_date(data, anchor):
    monday = data - datetime.timedelta(days=data.weekday())
    return ((monday - anchor).days // 7) % 4


def build_pattern_assignments(ano, mes, df_fix, anchor):
    """Retorna lista (data, turno, doctor_id, doctor_name) para o mês inteiro."""
    if df_fix.empty:
        return []
    fix_map = {}
    for _, r in df_fix.iterrows():
        if pd.notna(r.get('doctor_id')):
            fix_map[(int(r['week_num']), int(r['weekday']), r['shift_time'])] = (
                int(r['doctor_id']), str(r['doctor_name'])
            )
    regs = []
    for day in range(1, calendar.monthrange(ano, mes)[1] + 1):
        dt = datetime.date(ano, mes, day)
        w = cycle_week_for_date(dt, anchor)
        wd = dt.weekday()
        for turno in TURNOS:
            info = fix_map.get((w, wd, turno))
            if info:
                doctor_id, doctor_name = info
                regs.append((dt, turno, doctor_id, doctor_name))
    return regs


def schedule_to_pivot(df, ano, mes):
    if not df.empty:
        tmp = df.copy()
        tmp['dia'] = pd.to_datetime(tmp['shift_date']).dt.day
        pivot = tmp.pivot(index='shift_time', columns='dia', values='doctor_name').reindex(TURNOS).fillna("")
    else:
        pivot = pd.DataFrame(index=TURNOS)
    for day in range(1, calendar.monthrange(ano, mes)[1] + 1):
        if day not in pivot.columns:
            pivot[day] = ""
    return pivot.reindex(columns=range(1, calendar.monthrange(ano, mes)[1] + 1)).fillna("")


def render_schedule_calendar(pivot, ano, mes, medico_alvo=""):
    """Calendário mensal somente-leitura em HTML leve; evita vários st.dataframe no uso cotidiano."""
    calendar.setfirstweekday(calendar.MONDAY)
    weeks = calendar.monthcalendar(ano, mes)
    hoje_local = datetime.date.today()
    dot_class = {"Manhã": "manha", "Tarde": "tarde", "Noite": "noite"}
    turn_code = {"Manhã": "M", "Tarde": "T", "Noite": "N"}
    parts = ["<div class='schedule-calendar-wrap'><div class='schedule-calendar'>"]
    for wd in DIAS_SEMANA_CURTO:
        parts.append(f"<div class='cal-weekday'>{html.escape(wd)}</div>")
    for week in weeks:
        for day in week:
            if day == 0:
                parts.append("<div class='cal-day empty'></div>")
                continue
            dt = datetime.date(ano, mes, day)
            today_cls = " today" if dt == hoje_local else ""
            parts.append(f"<div class='cal-day{today_cls}'><div class='cal-date'>{day:02d}</div>")
            for turno in TURNOS:
                nome = ""
                if day in pivot.columns:
                    raw = pivot.at[turno, day]
                    nome = "" if pd.isna(raw) else str(raw).strip()
                selected = bool(medico_alvo and nome == medico_alvo)
                selected_cls = " selected" if selected else ""
                nome_html = html.escape(nome if nome else "—")
                parts.append(
                    f"<div class='shift-line{selected_cls}'>"
                    f"<span class='turn-dot {dot_class[turno]}'></span>"
                    f"<span class='turn-code'>{turn_code[turno]}</span>"
                    f"<span class='doctor' title='{html.escape(nome, quote=True)}'>{nome_html}</span>"
                    "</div>"
                )
            parts.append("</div>")
    parts.append("</div></div>")
    st.markdown("".join(parts), unsafe_allow_html=True)



def claim_shift_atomic(shift_date, shift_time, doctor_id, doctor_name):
    """Assume um turno vazio de forma atômica.

    ON CONFLICT DO NOTHING impede que dois médicos assumam o mesmo turno ao mesmo
    tempo. Retorna (True, nome) quando a vaga foi assumida e (False, ocupante)
    quando outro usuário chegou primeiro.
    """
    def _claim(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO shift_schedule (shift_date, shift_time, doctor_id, doctor_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (shift_date, shift_time) DO NOTHING
                RETURNING doctor_id;
                """,
                (shift_date, shift_time, int(doctor_id), doctor_name),
            )
            inserted = cur.fetchone()
            if inserted:
                return True, doctor_name

            cur.execute(
                """
                SELECT COALESCE(d.name, s.doctor_name) AS doctor_name
                FROM shift_schedule s
                LEFT JOIN doctors d ON d.id = s.doctor_id
                WHERE s.shift_date = %s AND s.shift_time = %s;
                """,
                (shift_date, shift_time),
            )
            row = cur.fetchone()
            return False, (row[0] if row else "outro médico")

    result = _with_connection(_claim, transactional=True)
    fetch_month_schedule.clear()
    return result


def replace_occupied_shift_atomic(shift_date, shift_time, expected_owner_id, expected_owner_name, doctor_id, doctor_name):
    """Substitui o ocupante de um plantão apenas se ele ainda for o esperado."""
    def _replace(conn):
        with conn.cursor() as cur:
            if expected_owner_id is not None and not pd.isna(expected_owner_id):
                cur.execute(
                    """
                    UPDATE shift_schedule
                    SET doctor_id=%s, doctor_name=%s
                    WHERE shift_date=%s AND shift_time=%s AND doctor_id=%s
                    RETURNING doctor_id;
                    """,
                    (int(doctor_id), doctor_name, shift_date, shift_time, int(expected_owner_id)),
                )
            else:
                cur.execute(
                    """
                    UPDATE shift_schedule
                    SET doctor_id=%s, doctor_name=%s
                    WHERE shift_date=%s AND shift_time=%s AND doctor_id IS NULL AND doctor_name=%s
                    RETURNING doctor_id;
                    """,
                    (int(doctor_id), doctor_name, shift_date, shift_time, expected_owner_name),
                )
            if cur.fetchone():
                return True, doctor_name

            cur.execute(
                """
                SELECT COALESCE(d.name, s.doctor_name)
                FROM shift_schedule s
                LEFT JOIN doctors d ON d.id=s.doctor_id
                WHERE s.shift_date=%s AND s.shift_time=%s;
                """,
                (shift_date, shift_time),
            )
            row = cur.fetchone()
            return False, (row[0] if row else "vaga já alterada")

    result = _with_connection(_replace, transactional=True)
    fetch_month_schedule.clear()
    return result


def swap_with_my_shift_atomic(target_date, target_time, target_owner_id, my_date, my_time, my_doctor_id):
    """Troca dois plantões com travamento das duas linhas para evitar corrida entre usuários."""
    def _swap(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT shift_date, shift_time, doctor_id, doctor_name
                FROM shift_schedule
                WHERE (shift_date=%s AND shift_time=%s)
                   OR (shift_date=%s AND shift_time=%s)
                FOR UPDATE;
                """,
                (target_date, target_time, my_date, my_time),
            )
            rows = cur.fetchall()
            state = {(r[0], r[1]): (r[2], r[3]) for r in rows}
            target = state.get((target_date, target_time))
            mine = state.get((my_date, my_time))
            if not target or not mine:
                return False, "Um dos plantões foi alterado antes da confirmação."

            current_target_id = target[0]
            current_my_id = mine[0]
            if target_owner_id is not None and not pd.isna(target_owner_id):
                if current_target_id != int(target_owner_id):
                    return False, "O plantão escolhido mudou de médico."
            if current_my_id != int(my_doctor_id):
                return False, "Seu plantão escolhido mudou antes da troca."

            target_id, target_name = target
            my_id, my_name = mine
            cur.execute(
                "UPDATE shift_schedule SET doctor_id=%s, doctor_name=%s WHERE shift_date=%s AND shift_time=%s",
                (my_id, my_name, target_date, target_time),
            )
            cur.execute(
                "UPDATE shift_schedule SET doctor_id=%s, doctor_name=%s WHERE shift_date=%s AND shift_time=%s",
                (target_id, target_name, my_date, my_time),
            )
            return True, "Troca realizada."

    result = _with_connection(_swap, transactional=True)
    fetch_month_schedule.clear()
    return result


def render_quick_claim_calendar(df_raw, ano, mes, doctor_name, doctor_id):
    """Calendário operacional: assumir vagas e interagir diretamente com plantões ocupados."""
    calendar.setfirstweekday(calendar.MONDAY)
    weeks = calendar.monthcalendar(ano, mes)
    hoje_local = datetime.date.today()
    occupied = {}
    if not df_raw.empty:
        for _, r in df_raw.iterrows():
            dt = pd.Timestamp(r['shift_date']).date()
            rid = None if pd.isna(r.get('doctor_id')) else int(r['doctor_id'])
            occupied[(dt, r['shift_time'])] = {
                'name': str(r['doctor_name']),
                'id': rid,
            }

    meus = []
    if not df_raw.empty:
        for _, r in df_raw[df_raw['doctor_name'] == doctor_name].sort_values(['shift_date', 'shift_time']).iterrows():
            d = pd.Timestamp(r['shift_date']).date()
            meus.append((d, str(r['shift_time'])))

    emoji_turno = {'Manhã': '🌅', 'Tarde': '☀️', 'Noite': '🌙'}
    st.caption("Toque em **+ Assumir** nas vagas. Toque no **nome de outro médico** para assumir ou trocar.")

    for week_idx, week in enumerate(weeks):
        cols = st.columns(7, gap="small")
        for wd, day in enumerate(week):
            with cols[wd]:
                # Cada data vira uma célula visualmente delimitada, formando uma grade clara.
                with st.container(border=True):
                    if day == 0:
                        st.markdown("<div style='height:176px;opacity:.10;'></div>", unsafe_allow_html=True)
                        continue

                    dt = datetime.date(ano, mes, day)
                    day_has_self = any(
                        occupied.get((dt, turno), {}).get('name') == doctor_name
                        for turno in TURNOS
                    )
                    if day_has_self:
                        # Marcador usado pelo CSS para destacar a caixa inteira do dia.
                        st.markdown("<span class='self-day-anchor'></span>", unsafe_allow_html=True)

                    hoje_badge = " · **Hoje**" if dt == hoje_local else ""
                    st.markdown(f"**{DIAS_SEMANA_CURTO[wd]} {day:02d}**{hoje_badge}")
                    if day_has_self:
                        st.markdown("<span class='self-day-badge'>✓ Você está neste dia</span>", unsafe_allow_html=True)
                    for turno in TURNOS:
                        info = occupied.get((dt, turno))
                        emoji = emoji_turno[turno]
                        if info:
                            atual = info['name']
                            atual_id = info['id']
                            if atual == doctor_name:
                                st.markdown(
                                    f"""
                                    <div class="quick-self-slot">
                                        <span class="self-emoji">{emoji}</span>
                                        <span class="self-check">✓</span>
                                        <span class="self-label">Você</span>
                                        <span class="self-hint">seu plantão</span>
                                    </div>
                                    """,
                                    unsafe_allow_html=True,
                                )
                            else:
                                # O próprio nome do ocupante vira o ponto de entrada para ações rápidas.
                                with st.popover(f"{emoji} {atual}", use_container_width=True):
                                    st.caption(f"{turno} · {day:02d}/{mes:02d} · atualmente com **{atual}**")
                                    if st.button(
                                        "✋ Assumir este plantão",
                                        key=f"takeover_{ano}_{mes}_{day}_{turno}_{doctor_id}",
                                        type="primary",
                                        use_container_width=True,
                                        help=f"Substitui {atual} por {doctor_name} neste plantão.",
                                    ):
                                        ok, owner = replace_occupied_shift_atomic(
                                            dt, turno, atual_id, atual, doctor_id, doctor_name
                                        )
                                        if ok:
                                            st.session_state['claim_flash'] = (
                                                'success',
                                                f"Você assumiu {turno.lower()} de {day:02d}/{mes:02d} no lugar de {atual}."
                                            )
                                        else:
                                            st.session_state['claim_flash'] = (
                                                'warning',
                                                f"Não foi possível assumir: o plantão agora está com {owner}."
                                            )
                                        st.rerun()

                                    opcoes_meus = [x for x in meus if x != (dt, turno)]
                                    if opcoes_meus:
                                        labels = {
                                            x: f"{x[0].strftime('%d/%m')} · {x[1]}"
                                            for x in opcoes_meus
                                        }
                                        meu_escolhido = st.selectbox(
                                            "Trocar com um plantão meu",
                                            opcoes_meus,
                                            format_func=lambda x: labels[x],
                                            key=f"swap_pick_{ano}_{mes}_{day}_{turno}_{doctor_id}",
                                        )
                                        if st.button(
                                            "🔄 Confirmar troca",
                                            key=f"swap_inline_{ano}_{mes}_{day}_{turno}_{doctor_id}",
                                            use_container_width=True,
                                        ):
                                            ok, msg = swap_with_my_shift_atomic(
                                                dt, turno, atual_id,
                                                meu_escolhido[0], meu_escolhido[1], doctor_id,
                                            )
                                            st.session_state['claim_flash'] = (
                                                'success' if ok else 'warning', msg
                                            )
                                            st.rerun()
                                    else:
                                        st.caption("Você ainda não tem outro plantão neste mês para fazer uma troca direta.")
                        else:
                            if st.button(
                                f"＋ {turno}",
                                key=f"claim_{ano}_{mes}_{day}_{turno}_{doctor_id}",
                                use_container_width=True,
                                help=f"Assumir {turno.lower()} de {day:02d}/{mes:02d}",
                            ):
                                ok, owner = claim_shift_atomic(dt, turno, doctor_id, doctor_name)
                                if ok:
                                    st.session_state['claim_flash'] = (
                                        'success',
                                        f"{doctor_name}: {turno.lower()} de {day:02d}/{mes:02d} assumido com sucesso."
                                    )
                                else:
                                    st.session_state['claim_flash'] = (
                                        'warning',
                                        f"Esse turno acabou de ser assumido por {owner}."
                                    )
                                st.rerun()
def current_state_from_edits(all_edits, ano, mes):
    rows = []
    for week_idx, (w_days, ed) in enumerate(all_edits):
        for idx, day in enumerate(w_days):
            if day <= 0:
                continue
            dt = datetime.date(ano, mes, day)
            for row_idx, turno in enumerate(TURNOS):
                col_name = f"w{week_idx}_d{idx}"
                nome = str(ed.at[row_idx, col_name]).strip() if pd.notna(ed.at[row_idx, col_name]) else ""
                if nome:
                    rows.append((dt, turno, nome))
    return rows


def financial_summary_from_rows(rows, shift_types_df):
    value_map = {r['name']: float(r['value']) for _, r in shift_types_df.iterrows()}
    if not rows:
        return pd.DataFrame(columns=['doctor_name', 'Manhã', 'Tarde', 'Noite', 'Total_Plantões', 'Total'])
    df = pd.DataFrame(rows, columns=['shift_date', 'shift_time', 'doctor_name'])
    df['valor'] = df['shift_time'].map(value_map).fillna(0.0)
    counts = df.pivot_table(index='doctor_name', columns='shift_time', values='shift_date', aggfunc='count', fill_value=0)
    for t in TURNOS:
        if t not in counts.columns:
            counts[t] = 0
    counts = counts[TURNOS]
    totals = df.groupby('doctor_name')['valor'].sum()
    counts['Total_Plantões'] = counts.sum(axis=1)
    counts['Total'] = totals
    return counts.reset_index().sort_values('doctor_name')


def ics_escape(text):
    return str(text).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def generate_ics(df_personal, doctor_name, shift_types_df):
    config = {r['name']: r for _, r in shift_types_df.iterrows()}
    now_utc = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Hospital HELP//Escala Radiologia//PT-BR", "CALSCALE:GREGORIAN"]
    for _, r in df_personal.iterrows():
        turno = r['shift_time']
        if turno not in config:
            continue
        cfg = config[turno]
        dt = pd.Timestamp(r['shift_date']).date()
        inicio = cfg['start_time']
        fim = cfg['end_time']
        if isinstance(inicio, str):
            inicio = datetime.time.fromisoformat(inicio)
        if isinstance(fim, str):
            fim = datetime.time.fromisoformat(fim)
        start_dt = datetime.datetime.combine(dt, inicio)
        end_date = dt + datetime.timedelta(days=1) if fim <= inicio else dt
        end_dt = datetime.datetime.combine(end_date, fim)
        uid_src = f"{dt}-{turno}-{doctor_name}"
        uid = hashlib.sha1(uid_src.encode('utf-8')).hexdigest() + "@hospital-help"
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_utc}",
            f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{ics_escape('Plantão Radiologia — ' + turno)}",
            f"DESCRIPTION:{ics_escape('Hospital HELP — ' + doctor_name)}",
            "LOCATION:Hospital HELP",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines).encode('utf-8')

# =================================================================
# 4. BACKUP E RESTAURAÇÃO COMPLETA
# =================================================================

BACKUP_TABLES = {
    'doctors.csv': "SELECT id, name, ativo FROM doctors ORDER BY id",
    'shift_schedule.csv': """SELECT shift_date, shift_time, doctor_id, doctor_name FROM shift_schedule ORDER BY shift_date, shift_time""",
    'fixed_schedule_4w.csv': """SELECT week_num, weekday, shift_time, doctor_id, doctor_name FROM fixed_schedule_4w ORDER BY week_num, weekday, shift_time""",
    'shift_types.csv': "SELECT name, start_time, end_time, value FROM shift_types ORDER BY name",
    'app_config.csv': "SELECT key, value FROM app_config ORDER BY key",
}


def create_full_backup_zip():
    bio = io.BytesIO()
    metadata = {
        'schema_version': 2,
        'created_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'app': 'Hospital HELP — Gestão de Escala de Radiologia',
    }
    with zipfile.ZipFile(bio, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
        for filename, query in BACKUP_TABLES.items():
            df = fetch_data(query)
            zf.writestr(filename, df.to_csv(index=False))
    bio.seek(0)
    return bio.getvalue()


def restore_backup_zip(uploaded_file):
    raw = uploaded_file.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw), 'r') as zf:
        required = set(BACKUP_TABLES.keys())
        missing = required - set(zf.namelist())
        if missing:
            raise ValueError(f"Backup incompleto. Faltando: {', '.join(sorted(missing))}")
        data = {name: pd.read_csv(zf.open(name)) for name in required}

    # Validações mínimas antes de qualquer DELETE.
    for col in ['id', 'name', 'ativo']:
        if col not in data['doctors.csv'].columns:
            raise ValueError(f"doctors.csv sem coluna obrigatória: {col}")
    for col in ['shift_date', 'shift_time', 'doctor_id']:
        if col not in data['shift_schedule.csv'].columns:
            raise ValueError(f"shift_schedule.csv sem coluna obrigatória: {col}")
    if not set(data['shift_schedule.csv']['shift_time'].dropna().unique()).issubset(set(TURNOS)):
        raise ValueError("Backup contém turno inválido em shift_schedule.csv")

    doctors_rows = []
    for _, r in data['doctors.csv'].iterrows():
        doctors_rows.append((int(r['id']), str(r['name']), str(r['ativo']).lower() in ('true', '1', 't', 'yes')))

    schedule_rows = []
    for _, r in data['shift_schedule.csv'].iterrows():
        schedule_rows.append((pd.to_datetime(r['shift_date']).date(), str(r['shift_time']), int(r['doctor_id']),
                              None if pd.isna(r.get('doctor_name')) else str(r.get('doctor_name'))))

    fixed_rows = []
    for _, r in data['fixed_schedule_4w.csv'].iterrows():
        if pd.isna(r.get('doctor_id')):
            continue
        fixed_rows.append((int(r['week_num']), int(r['weekday']), str(r['shift_time']), int(r['doctor_id']),
                           None if pd.isna(r.get('doctor_name')) else str(r.get('doctor_name'))))

    shift_type_rows = []
    for _, r in data['shift_types.csv'].iterrows():
        shift_type_rows.append((str(r['name']), str(r['start_time']), str(r['end_time']), float(r['value'])))

    config_rows = [(str(r['key']), str(r['value'])) for _, r in data['app_config.csv'].iterrows()]

    ops = [
        ("DELETE FROM shift_schedule", None),
        ("DELETE FROM fixed_schedule_4w", None),
        ("DELETE FROM doctors", None),
        ("DELETE FROM shift_types", None),
        ("DELETE FROM app_config", None),
        ("INSERT INTO doctors (id, name, ativo) VALUES %s", doctors_rows),
        ("INSERT INTO shift_schedule (shift_date, shift_time, doctor_id, doctor_name) VALUES %s", schedule_rows),
        ("INSERT INTO fixed_schedule_4w (week_num, weekday, shift_time, doctor_id, doctor_name) VALUES %s", fixed_rows),
        ("INSERT INTO shift_types (name, start_time, end_time, value) VALUES %s", shift_type_rows),
        ("INSERT INTO app_config (key, value) VALUES %s", config_rows),
        ("SELECT setval(pg_get_serial_sequence('doctors','id'), COALESCE((SELECT MAX(id) FROM doctors), 1), true)", None),
    ]
    execute_transacional(ops)


def restore_legacy_schedule_csv(uploaded_file):
    df = pd.read_csv(uploaded_file)
    needed = {'shift_date', 'shift_time', 'doctor_name'}
    if not needed.issubset(df.columns):
        raise ValueError("CSV antigo precisa conter shift_date, shift_time e doctor_name.")
    if not set(df['shift_time'].dropna().unique()).issubset(set(TURNOS)):
        raise ValueError("CSV contém turno inválido.")

    # Garante médicos e depois resolve IDs.
    names = sorted(set(str(x).strip() for x in df['doctor_name'].dropna() if str(x).strip()))
    for name in names:
        execute_query("INSERT INTO doctors (name, ativo) VALUES (%s, TRUE) ON CONFLICT (name) DO NOTHING", (name,))
    docs = fetch_data("SELECT id, name FROM doctors")
    id_by_name = {r['name']: int(r['id']) for _, r in docs.iterrows()}
    rows = []
    for _, r in df.iterrows():
        name = str(r['doctor_name']).strip()
        rows.append((pd.to_datetime(r['shift_date']).date(), str(r['shift_time']), id_by_name[name], name))
    execute_transacional([
        ("DELETE FROM shift_schedule", None),
        ("INSERT INTO shift_schedule (shift_date, shift_time, doctor_id, doctor_name) VALUES %s", rows),
    ])

# =================================================================
# 5. IDENTIDADE VISUAL
# =================================================================
def aplicar_estilo_visual():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');
    :root, .stApp { --background-color:#0D1420!important; --secondary-background-color:#111A29!important; --text-color:#E6EAF2!important; --primary-color:#3B82F6!important; }
    html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"], [data-testid="stHeader"], .main { background-color:#0D1420!important; }
    [data-testid="stHeader"] { background-color:rgba(0,0,0,0)!important; }
    html, body, [class*="css"] { font-family:'Inter',sans-serif; }
    h1,h2,h3,h4,h5,h6 { font-family:'Inter',sans-serif!important; font-weight:700!important; color:#F4F6FA!important; letter-spacing:-0.01em; }
    .stApp label,.stApp .stMarkdown,.stApp .stMarkdown p,.stApp [data-testid="stWidgetLabel"] p,.stApp [data-testid="stWidgetLabel"] { color:#E6EAF2!important; }
    .stApp [data-testid="stCaptionContainer"] { color:#7B8AA3!important; }
    ::-webkit-scrollbar { width:8px; height:8px; } ::-webkit-scrollbar-thumb { background:#2A3547; border-radius:4px; }
    div[data-testid="stMetric"], div[data-testid="metric-container"] { background:#111A29!important; border:1px solid #1E2A3D; border-radius:12px; padding:.9rem 1.1rem .8rem; box-shadow:0 1px 3px rgba(0,0,0,.35); }
    div[data-testid="stMetricValue"] { font-family:'JetBrains Mono',monospace!important; font-weight:600!important; color:#F4F6FA!important; }
    div[data-testid="stMetricLabel"] { font-weight:700!important; color:#5C6A84!important; font-size:.72rem!important; text-transform:uppercase; letter-spacing:.05em; }
    section[data-testid="stSidebar"] { background:#111A29!important; border-right:1px solid #1E2A3D; }
    section[data-testid="stSidebar"] * { color:#E6EAF2!important; }
    .stApp [data-baseweb="select"] > div,.stApp [data-baseweb="input"] > div,.stApp input,.stApp textarea { background-color:#0D1420!important; border:1px solid #26324A!important; color:#E6EAF2!important; border-radius:8px!important; }
    .stApp [data-baseweb="popover"] li { background-color:#111A29!important; color:#E6EAF2!important; }
    .stButton button,.stButton button[kind="secondary"],.stButton button:not([kind="primary"]) { background-color:#1B2A44!important; border:1px solid #2A3D5F!important; color:#8FB4FF!important; font-weight:600; border-radius:8px!important; }
    .stButton button *,.stButton button[kind="secondary"] *,.stButton button:not([kind="primary"]) * { color:#8FB4FF!important; }
    .stButton button:hover,.stButton button:not([kind="primary"]):hover { background-color:#223454!important; border-color:#3B82F6!important; }
    .stButton button[kind="primary"] { background:linear-gradient(135deg,#2563EB,#1D4ED8)!important; border:1px solid #1D4ED8!important; color:#FFF!important; box-shadow:0 4px 12px rgba(37,99,235,.35); }
    .stButton button[kind="primary"] * { color:#FFF!important; }
    div.stDownloadButton > button { background-color:transparent!important; border:1px solid #2A3D5F!important; color:#8FB4FF!important; border-radius:8px!important; font-weight:600; }
    div[data-testid="stDataFrame"],div[data-testid="stDataEditor"] { border-radius:12px; overflow:hidden; border:1px solid #1E2A3D; background:#111A29!important; }
    div[data-testid="stExpander"],div[data-testid="stPopover"] { border:1px solid #1E2A3D!important; border-radius:12px!important; background:#111A29!important; }
    .nav-eyebrow { font-size:.68rem; font-weight:700; letter-spacing:.09em; text-transform:uppercase; color:#5C6A84!important; margin:1rem 0 .5rem .1rem; }
    .turno-legend { display:flex; gap:18px; margin:4px 0 18px 0; flex-wrap:wrap; }
    .turno-legend .item { display:flex; align-items:center; gap:7px; font-size:.82rem; color:#AAB4C8; font-weight:500; }
    .turno-legend .dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
    .dot-manha { background:#F59E0B; }.dot-tarde { background:#06B6D4; }.dot-noite { background:#8B5CF6; }
    .help-header { display:flex; align-items:center; gap:14px; padding:4px 0 18px 0; border-bottom:1px solid #1E2A3D; margin-bottom:20px; }
    .help-header .badge,.sidebar-brand .badge { background:linear-gradient(135deg,#2563EB,#1D4ED8); color:#FFF; font-weight:800; border-radius:10px; display:flex; align-items:center; justify-content:center; box-shadow:0 4px 12px rgba(37,99,235,.35); }
    .help-header .badge { width:46px; height:46px; font-size:1.15rem; }.sidebar-brand .badge { width:44px; height:44px; font-size:1.05rem; flex-shrink:0; }
    .help-header .titles h1 { margin:0; font-size:1.2rem; line-height:1.2; }.help-header .titles span { color:#7B8AA3; font-size:.85rem; }
    .sidebar-brand { display:flex; align-items:center; gap:12px; text-align:left; padding:6px 0 14px 0; }.sidebar-brand .nome { font-weight:700; font-size:.95rem; color:#F4F6FA; }.sidebar-brand .depto { color:#7B8AA3; font-size:.78rem; font-weight:500; }
    .block-container { padding-top:.8rem!important; }
    .compact-brand { display:flex; align-items:center; gap:10px; min-height:44px; }
    .compact-brand .mini-badge { width:34px; height:34px; border-radius:9px; background:linear-gradient(135deg,#2563EB,#1D4ED8); color:#FFF; font-weight:800; display:flex; align-items:center; justify-content:center; }
    .compact-brand .brand-title { font-weight:750; font-size:1rem; color:#F4F6FA; }
    .compact-brand .brand-sub { color:#7B8AA3; font-size:.76rem; margin-top:-2px; }
    .period-hero { text-align:center; padding:4px 0 6px; }
    .period-hero .eyebrow { color:#64748B; font-size:.68rem; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }
    .period-hero .title { color:#F8FAFC; font-size:1.7rem; font-weight:800; letter-spacing:-.03em; line-height:1.15; }
    .schedule-calendar-wrap { width:100%; overflow-x:auto; padding-bottom:6px; }
    .schedule-calendar { min-width:980px; display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:7px; }
    .cal-weekday { color:#64748B; font-size:.68rem; font-weight:800; letter-spacing:.08em; text-align:center; text-transform:uppercase; padding:5px 2px; }
    .cal-day { background:#111A29; border:1px solid #1E2A3D; border-radius:10px; min-height:126px; padding:8px; }
    .cal-day.today { border-color:#3B82F6; box-shadow:0 0 0 1px rgba(59,130,246,.25) inset; }
    .cal-day.empty { background:rgba(17,26,41,.32); border-color:#172132; }
    .cal-date { color:#E2E8F0; font-family:'JetBrains Mono',monospace; font-size:.77rem; font-weight:700; margin-bottom:6px; }
    .shift-line { display:flex; align-items:center; gap:5px; min-height:28px; border-radius:6px; padding:4px 5px; margin:2px 0; background:#0D1420; border:1px solid #192438; overflow:hidden; }
    .shift-line.selected { background:#172C50; border-color:#3B82F6; box-shadow:0 0 0 1px rgba(59,130,246,.18) inset; }
    .shift-line .turn-dot { width:6px; height:6px; border-radius:50%; flex:0 0 6px; }
    .turn-dot.manha { background:#F59E0B; }.turn-dot.tarde { background:#06B6D4; }.turn-dot.noite { background:#8B5CF6; }
    .shift-line .turn-code { color:#64748B; font-size:.61rem; font-weight:800; width:13px; flex:0 0 13px; }
    .shift-line .doctor { color:#DDE5F3; font-size:.72rem; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .shift-line.selected .doctor { color:#FFFFFF; font-weight:800; }

    /* Destaque operacional do próprio médico no calendário rápido.
       O marcador invisível dentro do dia permite realçar a célula inteira via :has(). */
    .self-day-anchor { display:none; }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.self-day-anchor) {
        border-color:#3B82F6!important;
        background:linear-gradient(180deg,rgba(37,99,235,.095),rgba(17,26,41,.96))!important;
        box-shadow:0 0 0 1px rgba(59,130,246,.18), 0 7px 18px rgba(37,99,235,.08)!important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.self-day-anchor):hover {
        border-color:#60A5FA!important;
        box-shadow:0 0 0 1px rgba(96,165,250,.28), 0 8px 22px rgba(37,99,235,.12)!important;
    }
    .quick-self-slot {
        display:flex; align-items:center; gap:7px; width:100%; box-sizing:border-box;
        min-height:38px; margin:5px 0; padding:7px 9px; border-radius:9px;
        background:linear-gradient(135deg,rgba(37,99,235,.30),rgba(29,78,216,.18));
        border:1px solid #3B82F6; border-left:3px solid #60A5FA;
        box-shadow:0 0 0 1px rgba(59,130,246,.08) inset, 0 3px 10px rgba(37,99,235,.13);
    }
    .quick-self-slot .self-emoji { flex:0 0 auto; }
    .quick-self-slot .self-check {
        display:inline-flex; align-items:center; justify-content:center; width:18px; height:18px;
        border-radius:50%; background:#3B82F6; color:#FFF; font-size:.72rem; font-weight:900; flex:0 0 18px;
    }
    .quick-self-slot .self-label { color:#F8FBFF; font-weight:800; font-size:.88rem; letter-spacing:.01em; }
    .quick-self-slot .self-hint { margin-left:auto; color:#93C5FD; font-size:.62rem; font-weight:800; text-transform:uppercase; letter-spacing:.06em; }
    .self-day-badge {
        display:inline-flex; align-items:center; gap:4px; margin:0 0 3px 0; padding:2px 6px;
        border-radius:999px; background:rgba(59,130,246,.14); border:1px solid rgba(96,165,250,.28);
        color:#93C5FD; font-size:.59rem; font-weight:800; text-transform:uppercase; letter-spacing:.06em;
    }
    /* Identificação do médico: ação principal antes de interagir com a escala. */
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.identity-panel-anchor) {
        border:1px solid #3B82F6!important;
        background:linear-gradient(135deg,rgba(37,99,235,.16),rgba(17,26,41,.98))!important;
        box-shadow:0 0 0 1px rgba(59,130,246,.10),0 8px 26px rgba(37,99,235,.12)!important;
        padding:.35rem .45rem .5rem!important;
        margin:.25rem 0 1rem!important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.identity-panel-anchor) [data-baseweb="select"] > div {
        min-height:52px!important;
        border:2px solid #3B82F6!important;
        background:#0B1526!important;
        box-shadow:0 0 0 3px rgba(59,130,246,.10)!important;
        font-size:1rem!important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.identity-panel-anchor) [data-baseweb="select"] > div:hover {
        border-color:#60A5FA!important;
        box-shadow:0 0 0 4px rgba(96,165,250,.13)!important;
    }
    .identity-panel-anchor { display:none; }
    .identity-panel-head { display:flex; align-items:center; gap:12px; padding:5px 2px 10px; }
    .identity-icon {
        width:44px; height:44px; flex:0 0 44px; display:flex; align-items:center; justify-content:center;
        border-radius:12px; background:linear-gradient(135deg,#2563EB,#1D4ED8);
        box-shadow:0 5px 14px rgba(37,99,235,.28); font-size:1.25rem;
    }
    .identity-eyebrow { color:#93C5FD; font-size:.64rem; font-weight:900; letter-spacing:.10em; text-transform:uppercase; }
    .identity-title { color:#F8FAFC; font-size:1.25rem; font-weight:850; letter-spacing:-.02em; line-height:1.15; margin-top:1px; }
    .identity-subtitle { color:#A8B6CC; font-size:.78rem; line-height:1.35; margin-top:3px; }

    .month-summary { color:#7B8AA3; font-size:.82rem; margin:.15rem 0 .6rem; }
    @media (max-width: 700px) { .block-container { padding-left:.65rem!important; padding-right:.65rem!important; } .schedule-calendar { min-width:900px; } .period-hero .title { font-size:1.45rem; } }
    </style>
    """, unsafe_allow_html=True)

aplicar_estilo_visual()

# =================================================================
# 6. LOGIN (MANTIDO COMO ESTAVA — ITEM 11 EXCLUÍDO PELO USUÁRIO)
# =================================================================
if 'auth' not in st.session_state:
    st.session_state['auth'] = False
if not st.session_state['auth']:
    c_login = st.columns([1, 1.2, 1])[1]
    with c_login:
        st.markdown(
            "<div style='text-align:center; margin-top:8vh;'>"
            "<div style='background:linear-gradient(135deg,#2563EB,#1D4ED8); color:#FFFFFF; font-weight:800; font-size:1.8rem; width:64px; height:64px; border-radius:14px; display:flex; align-items:center; justify-content:center; margin:0 auto 16px auto;'>HH</div>"
            "<h2 style='margin-bottom:2px;'>Hospital HELP</h2><p style='color:#7B8AA3; margin-top:0;'>Gestão de Escala — Radiologia</p></div>",
            unsafe_allow_html=True
        )
        pw = st.text_input("Senha de Acesso", type="password", label_visibility="collapsed", placeholder="Senha de acesso")
        if st.button("Entrar", use_container_width=True, type="primary"):
            if hashlib.sha256(str.encode(pw)).hexdigest() == "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4":
                st.session_state['auth'] = True
                st.rerun()
            else:
                st.error("Senha incorreta.")
    st.stop()

# =================================================================
# 7. DADOS GLOBAIS / PERÍODO
# =================================================================
df_docs = fetch_doctors()
df_docs['ativo'] = df_docs['ativo'].fillna(False).astype(bool) if not df_docs.empty else pd.Series(dtype=bool)
active_names = df_docs[df_docs['ativo']]['name'].tolist() if not df_docs.empty else []
all_names = df_docs['name'].tolist() if not df_docs.empty else []
id_by_name = {r['name']: int(r['id']) for _, r in df_docs.iterrows()} if not df_docs.empty else {}
name_by_id = {int(r['id']): r['name'] for _, r in df_docs.iterrows()} if not df_docs.empty else {}

hoje = datetime.date.today()
if 'period_month' not in st.session_state:
    st.session_state['period_month'] = hoje.month
if 'period_year' not in st.session_state:
    st.session_state['period_year'] = hoje.year
if 'page' not in st.session_state:
    st.session_state['page'] = '📅 Escala'
if 'scale_edit_mode' not in st.session_state:
    st.session_state['scale_edit_mode'] = False
if 'show_pattern_preview' not in st.session_state:
    st.session_state['show_pattern_preview'] = False


def _set_page(label):
    st.session_state['page'] = label


def _period_changed():
    st.session_state['show_pattern_preview'] = False
    st.session_state['scale_edit_mode'] = False


def _shift_period(delta):
    mes = int(st.session_state['period_month']) + int(delta)
    ano_local = int(st.session_state['period_year'])
    if mes < 1:
        mes, ano_local = 12, ano_local - 1
    elif mes > 12:
        mes, ano_local = 1, ano_local + 1
    st.session_state['period_month'] = mes
    st.session_state['period_year'] = ano_local
    _period_changed()


def _ir_para_hoje():
    st.session_state['period_month'] = hoje.month
    st.session_state['period_year'] = hoje.year
    _period_changed()


def _toggle_scale_edit():
    st.session_state['scale_edit_mode'] = not bool(st.session_state.get('scale_edit_mode', False))


def _toggle_pattern_preview():
    st.session_state['show_pattern_preview'] = not bool(st.session_state.get('show_pattern_preview', False))


def render_period_selector():
    """Período mensal evidente, com navegação rápida e seletores avançados escondidos."""
    mes_atual = int(st.session_state['period_month'])
    ano_atual = int(st.session_state['period_year'])

    cprev, ctitle, cnext = st.columns([1.15, 4.7, 1.15])
    cprev.button(
        "‹", key=f"period_prev_{st.session_state['page']}", use_container_width=True,
        on_click=_shift_period, args=(-1,), help="Mês anterior"
    )
    with ctitle:
        st.markdown(
            f"<div class='period-hero'><div class='eyebrow'>Escala</div>"
            f"<div class='title'>{MESES[mes_atual-1]} {ano_atual}</div></div>",
            unsafe_allow_html=True,
        )
    cnext.button(
        "›", key=f"period_next_{st.session_state['page']}", use_container_width=True,
        on_click=_shift_period, args=(1,), help="Próximo mês"
    )

    csp1, ctoday, cchoose, csp2 = st.columns([2.3, 1.1, 1.5, 2.3])
    ctoday.button(
        "Hoje", key=f"period_today_{st.session_state['page']}", use_container_width=True,
        on_click=_ir_para_hoje
    )
    with cchoose:
        with st.popover("📅 Outro mês", use_container_width=True):
            years = list(range(hoje.year - 3, hoje.year + 5))
            if ano_atual not in years:
                years = sorted(set(years + [ano_atual]))
            st.selectbox("Mês", range(1, 13), format_func=lambda x: MESES[x-1], key='period_month', on_change=_period_changed)
            st.selectbox("Ano", years, key='period_year', on_change=_period_changed)


# =================================================================
# 8. SIDEBAR / NAVEGAÇÃO
# =================================================================
def nav_button(label, key):
    ativo = st.session_state['page'] == label
    st.sidebar.button(
        label, key=key, type='primary' if ativo else 'secondary', use_container_width=True,
        on_click=_set_page, args=(label,)
    )


with st.sidebar:
    st.markdown("<div class='sidebar-brand'><div class='badge'>HH</div><div><div class='nome'>Hospital HELP</div><div class='depto'>Radiologia</div></div></div>", unsafe_allow_html=True)
    st.divider()

st.sidebar.markdown("<div class='nav-eyebrow'>Dia a dia</div>", unsafe_allow_html=True)
nav_button('📅 Escala', 'nav_escala')
nav_button('🔄 Trocas', 'nav_trocas')

st.sidebar.markdown("<div class='nav-eyebrow'>Planejamento</div>", unsafe_allow_html=True)
nav_button('🔁 Padrão Rotativo', 'nav_padrao')

st.sidebar.markdown("<div class='nav-eyebrow'>Gestão</div>", unsafe_allow_html=True)
nav_button('👥 Equipe', 'nav_equipe')
nav_button('💰 Fechamento RH', 'nav_rh')

st.sidebar.markdown("<div class='nav-eyebrow'>Configurações</div>", unsafe_allow_html=True)
nav_button('⚙️ Turnos e Valores', 'nav_turnos')
nav_button('💾 Backup', 'nav_backup')

mes_num = int(st.session_state['period_month'])
ano = int(st.session_state['period_year'])
mes_nome = MESES[mes_num - 1]
page = st.session_state['page']

# A navegação principal fica concentrada na sidebar, que abre expandida.
# Isso evita duplicar Escala/Trocas numa barra superior e libera espaço vertical para o calendário.

# =================================================================
# 9. PDF OFICIAL — ESCALA SALVA + FECHAMENTO RH
# =================================================================
def generate_pdf_semanal(weeks, pivot, resumo, mes, ano, shift_types_df):
    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.add_page()
    total_semanas = len(weeks)
    if total_semanas <= 4:
        font_tit, font_tab, h_row, margin_w = 18, 9, 7, 5
    elif total_semanas == 5:
        font_tit, font_tab, h_row, margin_w = 16, 8, 6, 3
    else:
        font_tit, font_tab, h_row, margin_w = 14, 7, 4.5, 2

    pdf.set_font("Arial", 'B', font_tit); pdf.set_text_color(0, 45, 98)
    pdf.cell(0, 10, f"HOSPITAL HELP - ESCALA RADIOLOGIA - {mes.upper()} / {ano}", ln=True, align='C'); pdf.ln(2)
    headers = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]
    col_w_label = 25; col_w_day = (pdf.w - (pdf.l_margin + pdf.r_margin) - col_w_label) / 7

    for i, week in enumerate(weeks):
        pdf.set_font("Arial", 'B', font_tab + 1); pdf.set_text_color(0, 45, 98); pdf.cell(0, h_row, f"SEMANA {i+1}", ln=True)
        pdf.set_font("Arial", 'B', font_tab); pdf.set_fill_color(0, 45, 98); pdf.set_text_color(255, 255, 255)
        pdf.cell(col_w_label, h_row, "Turno", 1, 0, 'C', True)
        for idx, day in enumerate(week):
            txt = f"{headers[idx]} {day:02d}" if day > 0 else headers[idx]
            pdf.cell(col_w_day, h_row, txt, 1, 0, 'C', True)
        pdf.ln()
        for shift in TURNOS:
            pdf.set_font("Arial", 'B', font_tab); pdf.set_fill_color(240, 240, 240); pdf.set_text_color(0, 45, 98)
            pdf.cell(col_w_label, h_row, shift.replace('ã', 'a'), 1, 0, 'C', True)
            pdf.set_font("Arial", '', font_tab); pdf.set_text_color(0, 0, 0)
            char_limit = 18 if font_tab >= 9 else (22 if font_tab == 8 else 25)
            for day in week:
                if day == 0:
                    pdf.cell(col_w_day, h_row, "-", 1, 0, 'C')
                else:
                    nome = str(pivot.at[shift, day]) if day in pivot.columns else ""
                    pdf.cell(col_w_day, h_row, nome[:char_limit], 1, 0, 'C')
            pdf.ln()
        pdf.ln(margin_w)

    pdf.add_page(); pdf.set_font("Arial", 'B', 14); pdf.set_text_color(0, 45, 98)
    pdf.cell(0, 10, "FECHAMENTO FINANCEIRO - RH", ln=True, align='L'); pdf.ln(4)
    pdf.set_font("Arial", 'B', 9); pdf.set_fill_color(0, 45, 98); pdf.set_text_color(255, 255, 255)
    widths = [90, 25, 25, 25, 30, 40]
    headers_rh = ["Medico", "Manha", "Tarde", "Noite", "Plantoes", "Total (R$)"]
    for w, h in zip(widths, headers_rh):
        pdf.cell(w, 8, h, 1, 0, 'C', True)
    pdf.ln()
    pdf.set_font("Arial", '', 9); pdf.set_text_color(0, 0, 0); total_geral = 0.0
    for _, r in resumo.iterrows():
        vals = [str(r['doctor_name'])[:36], str(int(r['Manhã'])), str(int(r['Tarde'])), str(int(r['Noite'])), str(int(r['Total_Plantões']))]
        for w, val in zip(widths[:-1], vals):
            pdf.cell(w, 8, val, 1, 0, 'C' if w != widths[0] else 'L')
        total = float(r['Total']); total_geral += total
        pdf.cell(widths[-1], 8, f"{total:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'), 1, 1, 'R')
    pdf.set_font("Arial", 'B', 9); pdf.set_fill_color(240, 240, 240); pdf.set_text_color(0, 45, 98)
    pdf.cell(sum(widths[:-1]), 8, "TOTAL GERAL", 1, 0, 'R', True)
    pdf.cell(widths[-1], 8, f"{total_geral:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'), 1, 1, 'R', True)

    # Rodapé com parâmetros de pagamento usados.
    pdf.ln(5); pdf.set_font("Arial", '', 8); pdf.set_text_color(70, 70, 70)
    valores = " | ".join([f"{r['name']}: R$ {float(r['value']):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.') for _, r in shift_types_df.iterrows()])
    pdf.multi_cell(0, 5, "Valores considerados: " + valores)
    out = pdf.output(dest='S')
    return out.encode('latin-1') if isinstance(out, str) else bytes(out)

# =================================================================
# 10. PÁGINA — ESCALA
# =================================================================
if page == '📅 Escala':
    render_period_selector()
    mes_num = int(st.session_state['period_month'])
    ano = int(st.session_state['period_year'])
    mes_nome = MESES[mes_num - 1]

    df_raw = fetch_month_schedule(ano, mes_num)
    df_pivot = schedule_to_pivot(df_raw, ano, mes_num)
    dias_mes = calendar.monthrange(ano, mes_num)[1]
    total_slots = dias_mes * len(TURNOS)
    filled = len(df_raw)

    # O médico se identifica uma vez na sessão e, depois disso, assume vagas com um único clique.
    if st.session_state.get('medico_alvo_escala') not in ([""] + active_names):
        st.session_state.pop('medico_alvo_escala', None)

    # Identificação é a ação de entrada mais importante da tela: destaque visual forte,
    # sem competir com botões de navegação que já ficam na sidebar.
    with st.container(border=True):
        st.markdown("<span class='identity-panel-anchor'></span>", unsafe_allow_html=True)
        st.markdown(
            "<div class='identity-panel-head'>"
            "<div class='identity-icon'>👤</div>"
            "<div><div class='identity-eyebrow'>IDENTIFIQUE-SE PARA USAR A ESCALA</div>"
            "<div class='identity-title'>Eu sou...</div>"
            "<div class='identity-subtitle'>Escolha seu nome uma vez. Depois é só tocar no plantão que deseja assumir.</div></div>"
            "</div>",
            unsafe_allow_html=True,
        )
        medico_alvo = st.selectbox(
            "Selecione seu nome",
            [""] + active_names,
            key='medico_alvo_escala',
            help="Depois de selecionar seu nome, os turnos vazios ficam disponíveis para assumir com um toque.",
            label_visibility="collapsed",
            placeholder="Toque aqui e escolha seu nome",
        )

    flash = st.session_state.pop('claim_flash', None)
    if flash:
        kind, message = flash
        if kind == 'success':
            st.success(message)
        else:
            st.warning(message)

    st.markdown(
        "<div class='turno-legend'><div class='item'><span class='dot dot-manha'></span>Manhã</div>"
        "<div class='item'><span class='dot dot-tarde'></span>Tarde</div>"
        "<div class='item'><span class='dot dot-noite'></span>Noite</div></div>",
        unsafe_allow_html=True,
    )

    if medico_alvo:
        render_quick_claim_calendar(
            df_raw, ano, mes_num, medico_alvo, id_by_name[medico_alvo]
        )
    else:
        st.info("Escolha seu nome em **Eu sou** para assumir um turno vazio com um toque.")
        render_schedule_calendar(df_pivot, ano, mes_num, "")

    # Informações pessoais são úteis, mas ficam fora do caminho principal.
    if medico_alvo:
        df_pessoal = df_raw[df_raw['doctor_name'] == medico_alvo].copy().sort_values('shift_date')
        with st.expander(f"📅 Meus plantões · {len(df_pessoal)} neste mês"):
            if df_pessoal.empty:
                st.caption(f"Nenhum plantão de {medico_alvo} em {mes_nome}/{ano}.")
            else:
                lista_pessoal = df_pessoal.copy()
                lista_pessoal['Data'] = pd.to_datetime(lista_pessoal['shift_date']).dt.strftime('%d/%m/%Y')
                lista_pessoal = lista_pessoal.rename(columns={'shift_time':'Turno'})
                st.dataframe(lista_pessoal[['Data','Turno']], hide_index=True, use_container_width=True)
                shift_types_df_ics = get_shift_types()
                ics_bytes = generate_ics(df_pessoal, medico_alvo, shift_types_df_ics)
                st.download_button(
                    "📅 Adicionar ao meu calendário (.ics)", data=ics_bytes,
                    file_name=f"Plantões_{medico_alvo}_{mes_nome}_{ano}.ics", mime="text/calendar",
                    use_container_width=True,
                )

    # Para o médico comum basta uma linha de status; controles de gestão ficam recolhidos.
    cobertura = (filled / total_slots * 100) if total_slots else 0
    st.markdown(
        f"<div class='month-summary'>{filled}/{total_slots} turnos cobertos · "
        f"{max(total_slots-filled, 0)} sem médico · {cobertura:.0f}% de cobertura</div>",
        unsafe_allow_html=True,
    )

    with st.expander("⚙️ Administração e ferramentas"):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Turnos cobertos", f"{filled}/{total_slots}")
        c2.metric("Sem médico", max(total_slots - filled, 0))
        c3.metric("Médicos escalados", df_raw['doctor_id'].nunique() if not df_raw.empty else 0)
        c4.metric("Cobertura", f"{cobertura:.0f}%")

        cpattern, cedit = st.columns(2)
        cpattern.button(
            "✨ Aplicar Padrão Rotativo" if not st.session_state['show_pattern_preview'] else "✕ Fechar prévia do padrão",
            key='toggle_pattern_preview_btn', use_container_width=True, on_click=_toggle_pattern_preview
        )
        cedit.button(
            "✏️ Editar escala completa" if not st.session_state['scale_edit_mode'] else "👁️ Fechar editor",
            key='toggle_scale_edit_btn', use_container_width=True, on_click=_toggle_scale_edit
        )

    if st.session_state['show_pattern_preview']:
        st.subheader("Prévia do padrão rotativo")
        anchor = get_rotation_anchor()
        df_fix = fetch_fixed_pattern()
        desired = build_pattern_assignments(ano, mes_num, df_fix, anchor)
        current_map = {(pd.Timestamp(r['shift_date']).date(), r['shift_time']): r['doctor_name'] for _, r in df_raw.iterrows()}
        desired_map = {(r[0], r[1]): r[3] for r in desired}
        all_keys = set(current_map) | set(desired_map)
        iguais = sum(1 for k in all_keys if current_map.get(k) == desired_map.get(k) and current_map.get(k) is not None)
        alterados = sum(1 for k in all_keys if current_map.get(k) and desired_map.get(k) and current_map.get(k) != desired_map.get(k))
        novos = sum(1 for k in all_keys if not current_map.get(k) and desired_map.get(k))
        apagados = sum(1 for k in all_keys if current_map.get(k) and not desired_map.get(k))
        st.caption(f"Ciclo ancorado em {anchor.strftime('%d/%m/%Y')} (Semana 1).")
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Mantidos", iguais); p2.metric("Alterados", alterados); p3.metric("Novos", novos); p4.metric("Ficarão vazios", apagados)
        if alterados or apagados:
            st.warning("Edições manuais divergentes do padrão serão substituídas.")
        trava = st.checkbox("Estou ciente. Substituir a escala deste mês pelo padrão.", key='confirm_apply_pattern')
        if st.button("Aplicar padrão ao mês", type="primary", use_container_width=True, disabled=not trava, key='apply_pattern_month'):
            rows = [(dt, turno, did, nome) for dt, turno, did, nome in desired]
            ini, fim = month_bounds(ano, mes_num)
            execute_transacional([
                ("DELETE FROM shift_schedule WHERE shift_date >= %s AND shift_date < %s", (ini, fim)),
                ("INSERT INTO shift_schedule (shift_date, shift_time, doctor_id, doctor_name) VALUES %s", rows),
            ])
            st.session_state['show_pattern_preview'] = False
            st.rerun()

    if st.session_state['scale_edit_mode']:
        st.subheader("✏️ Edição administrativa da escala")
        st.caption("Use este editor apenas para alterações em lote. Para assumir uma vaga, o médico deve usar o calendário acima.")
        calendar.setfirstweekday(calendar.MONDAY)
        weeks = calendar.monthcalendar(ano, mes_num)
        existing_names = df_raw['doctor_name'].dropna().unique().tolist() if not df_raw.empty else []
        editor_options = [""] + sorted(set(active_names + existing_names))
        all_edits = []
        for i, week in enumerate(weeks):
            st.markdown(f"#### Semana {i+1}")
            w_data = {f"w{i}_d{idx}": (["", "", ""] if day == 0 else [df_pivot.at[t, day] for t in TURNOS]) for idx, day in enumerate(week)}
            df_w = pd.DataFrame(w_data, index=TURNOS).reset_index().rename(columns={'index':'Turno'})
            df_w['Turno'] = df_w['Turno'].map({'Manhã':'🌅 Manhã','Tarde':'☀️ Tarde','Noite':'🌙 Noite'})
            config = {'Turno': st.column_config.TextColumn('Turno', disabled=True, width='small')}
            for idx, day in enumerate(week):
                key = f"w{i}_d{idx}"
                config[key] = (
                    st.column_config.TextColumn(DIAS_SEMANA_CURTO[idx], disabled=True, width='small') if day == 0
                    else st.column_config.SelectboxColumn(f"{DIAS_SEMANA_CURTO[idx]} {day:02d}", options=editor_options, width='small')
                )
            ed = st.data_editor(df_w, column_config=config, hide_index=True, use_container_width=True, key=f"edit_month_w{i}_{ano}_{mes_num}")
            all_edits.append((week, ed))

        current_rows = current_state_from_edits(all_edits, ano, mes_num)
        if st.button("💾 Salvar escala deste mês", type="primary", use_container_width=True, key='save_month_schedule'):
            rows = []
            for dt, turno, nome in current_rows:
                did = id_by_name.get(nome)
                if did is None:
                    st.error(f"Médico não encontrado: {nome}")
                    st.stop()
                rows.append((dt, turno, did, nome))
            ini, fim = month_bounds(ano, mes_num)
            execute_transacional([
                ("DELETE FROM shift_schedule WHERE shift_date >= %s AND shift_date < %s", (ini, fim)),
                ("INSERT INTO shift_schedule (shift_date, shift_time, doctor_id, doctor_name) VALUES %s", rows),
            ])
            st.session_state['scale_edit_mode'] = False
            st.success("Escala salva!")
            st.rerun()

# =================================================================
# 11. PÁGINA — PADRÃO ROTATIVO CONTÍNUO
# =================================================================
elif page == '🔁 Padrão Rotativo':
    st.header("🔁 Padrão Rotativo · ciclo contínuo de 4 semanas")
    anchor = get_rotation_anchor()
    st.caption("A Semana 1 é definida por uma segunda-feira âncora. A partir dela, o ciclo continua sem reiniciar na virada do mês.")
    c1, c2 = st.columns([2, 3])
    nova_ancora = c1.date_input("Segunda-feira de início da Semana 1", value=anchor)
    monday = nova_ancora - datetime.timedelta(days=nova_ancora.weekday())
    c2.info(f"Semana 1: {monday.strftime('%d/%m/%Y')} a {(monday + datetime.timedelta(days=6)).strftime('%d/%m/%Y')} · depois Semanas 2, 3, 4 e reinicia.")
    if st.button("💾 Salvar data âncora"):
        set_rotation_anchor(nova_ancora)
        st.success("Âncora do ciclo atualizada.")
        st.rerun()

    st.divider()
    df_fix_raw = fetch_fixed_pattern()
    existing_pattern_names = df_fix_raw['doctor_name'].dropna().unique().tolist() if not df_fix_raw.empty else []
    pattern_options = [""] + sorted(set(active_names + existing_pattern_names))
    edits = []
    for w_num in range(4):
        start = monday + datetime.timedelta(days=7 * w_num)
        st.markdown(f"#### Semana {w_num + 1} · exemplo {start.strftime('%d/%m')}–{(start + datetime.timedelta(days=6)).strftime('%d/%m')}")
        df_w_raw = df_fix_raw[df_fix_raw['week_num'] == w_num] if not df_fix_raw.empty else pd.DataFrame()
        if not df_w_raw.empty:
            pivot = df_w_raw.pivot(index='shift_time', columns='weekday', values='doctor_name').reindex(TURNOS)
            pivot = pivot.reindex(columns=range(7)).fillna("")
        else:
            pivot = pd.DataFrame("", index=TURNOS, columns=range(7))
        pivot.columns = [str(c) for c in range(7)]
        conf = {str(c): st.column_config.SelectboxColumn(DIAS_SEMANA[c], options=pattern_options, width='small') for c in range(7)}
        ed = st.data_editor(pivot, column_config=conf, use_container_width=True, key=f"pattern_w{w_num}")
        edits.append((w_num, ed))

    if st.button("💾 Salvar padrão rotativo", type='primary', use_container_width=True):
        rows = []
        for w_num, ed in edits:
            for turno in TURNOS:
                for wd in range(7):
                    nome = str(ed.at[turno, str(wd)]).strip() if pd.notna(ed.at[turno, str(wd)]) else ""
                    if nome:
                        rows.append((w_num, wd, turno, id_by_name[nome], nome))
        execute_transacional([
            ("DELETE FROM fixed_schedule_4w", None),
            ("INSERT INTO fixed_schedule_4w (week_num, weekday, shift_time, doctor_id, doctor_name) VALUES %s", rows),
        ])
        st.success("Padrão rotativo salvo.")
        st.rerun()

# =================================================================
# 12. PÁGINA — EQUIPE
# =================================================================
elif page == '👥 Equipe':
    st.header("👥 Equipe médica")
    with st.form('add_doctor', clear_on_submit=True):
        c1, c2 = st.columns([4, 1.5])
        novo = c1.text_input("Nome do médico", placeholder="Nome completo")
        submitted = c2.form_submit_button("➕ Adicionar", use_container_width=True)
        if submitted and novo.strip():
            execute_query("INSERT INTO doctors (name, ativo) VALUES (%s, TRUE) ON CONFLICT (name) DO UPDATE SET ativo=TRUE", (novo.strip(),))
            st.rerun()

    if df_docs.empty:
        st.info("Nenhum médico cadastrado.")
    else:
        st.caption("O ID é a identidade interna do médico. O nome pode ser corrigido sem perder o histórico. Inativar apenas impede novas atribuições.")
        for _, r in df_docs.iterrows():
            c1, c2, c3 = st.columns([4, 1.4, 1.4])
            c1.write(f"{'🟢' if r['ativo'] else '⚪'} {r['name']}")
            c2.caption(f"ID {int(r['id'])}")
            if c3.button("Inativar" if r['ativo'] else "Reativar", key=f"toggle_doc_{int(r['id'])}", use_container_width=True):
                execute_query("UPDATE doctors SET ativo=%s WHERE id=%s", (not bool(r['ativo']), int(r['id'])))
                st.rerun()

        st.divider()
        with st.expander("✏️ Corrigir nome de médico"):
            doc_id_rename = st.selectbox(
                "Médico",
                df_docs['id'].astype(int).tolist(),
                format_func=lambda x: name_by_id.get(int(x), str(x)),
                key='rename_doc_id'
            )
            nome_atual = name_by_id.get(int(doc_id_rename), '')
            novo_nome = st.text_input("Novo nome", value=nome_atual, key='rename_doc_name')
            if st.button("Salvar novo nome", key='rename_doc_btn'):
                novo_nome = novo_nome.strip()
                if not novo_nome:
                    st.error("O nome não pode ficar vazio.")
                elif novo_nome != nome_atual and novo_nome in all_names:
                    st.error("Já existe outro médico com esse nome.")
                else:
                    # doctor_id preserva a identidade; doctor_name legado é sincronizado por compatibilidade/backup.
                    execute_transacional([
                        ("UPDATE doctors SET name=%s WHERE id=%s", (novo_nome, int(doc_id_rename))),
                        ("UPDATE shift_schedule SET doctor_name=%s WHERE doctor_id=%s", (novo_nome, int(doc_id_rename))),
                        ("UPDATE fixed_schedule_4w SET doctor_name=%s WHERE doctor_id=%s", (novo_nome, int(doc_id_rename))),
                    ])
                    st.success("Nome atualizado sem alterar o histórico de plantões.")
                    st.rerun()

# =================================================================
# 13. PÁGINA — TURNOS E VALORES
# =================================================================
elif page == '⚙️ Turnos e Valores':
    st.header("⚙️ Turnos e valores")
    st.caption("Esses valores alimentam o fechamento RH e o PDF. Alterações futuras não exigem editar o código.")
    df_turnos = get_shift_types()
    if df_turnos.empty:
        st.warning("Configuração de turnos ausente.")
    else:
        edit = df_turnos.copy()
        edit['start_time'] = edit['start_time'].astype(str).str[:5]
        edit['end_time'] = edit['end_time'].astype(str).str[:5]
        ed = st.data_editor(
            edit,
            hide_index=True,
            use_container_width=True,
            disabled=['name'],
            column_config={
                'name': st.column_config.TextColumn('Turno'),
                'start_time': st.column_config.TextColumn('Início (HH:MM)'),
                'end_time': st.column_config.TextColumn('Fim (HH:MM)'),
                'value': st.column_config.NumberColumn('Valor (R$)', min_value=0.0, step=50.0, format='%.2f'),
            }
        )
        if st.button("💾 Salvar turnos e valores", type='primary'):
            rows = []
            try:
                for _, r in ed.iterrows():
                    datetime.time.fromisoformat(str(r['start_time']))
                    datetime.time.fromisoformat(str(r['end_time']))
                    rows.append((str(r['start_time']), str(r['end_time']), float(r['value']), str(r['name'])))
            except Exception:
                st.error("Horários precisam estar no formato HH:MM.")
            else:
                execute_transacional([
                    ("UPDATE shift_types SET start_time=%s::time, end_time=%s::time, value=%s WHERE name=%s", row)
                    for row in rows
                ])
                st.success("Configuração atualizada.")
                st.rerun()

# =================================================================
# 14. PÁGINA — FECHAMENTO RH
# =================================================================
elif page == '💰 Fechamento RH':
    render_period_selector()
    mes_num = int(st.session_state['period_month'])
    ano = int(st.session_state['period_year'])
    mes_nome = MESES[mes_num - 1]
    st.header(f"💰 Fechamento RH · {mes_nome} {ano}")

    df_raw = fetch_month_schedule(ano, mes_num)
    shift_types_df = get_shift_types()
    rows = [(pd.Timestamp(r['shift_date']).date(), r['shift_time'], r['doctor_name']) for _, r in df_raw.iterrows()]
    resumo = financial_summary_from_rows(rows, shift_types_df)
    total = float(resumo['Total'].sum()) if not resumo.empty else 0.0
    c1, c2, c3 = st.columns(3)
    c1.metric("Custo da escala", f"R$ {total:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'))
    c2.metric("Plantões", len(rows))
    c3.metric("Médicos", resumo['doctor_name'].nunique() if not resumo.empty else 0)

    if resumo.empty:
        st.info("Sem plantões no período.")
    else:
        display = resumo.rename(columns={'doctor_name':'Médico', 'Total_Plantões':'Plantões', 'Total':'Total (R$)'})
        st.dataframe(
            display, hide_index=True, use_container_width=True,
            column_config={'Total (R$)': st.column_config.NumberColumn(format='R$ %.2f')}
        )

        st.divider()
        st.subheader("📄 Relatório oficial")
        st.caption("Um único PDF com a escala mensal completa e, em seguida, o fechamento financeiro do RH. Não há exportação CSV para usuários.")
        schedule_sig = df_raw[['shift_date','shift_time','doctor_name']].astype(str).to_csv(index=False) if not df_raw.empty else ''
        shift_sig = shift_types_df[['name','start_time','end_time','value']].astype(str).to_csv(index=False) if not shift_types_df.empty else ''
        report_key = hashlib.sha1(f"{ano}-{mes_num}|{schedule_sig}|{shift_sig}".encode('utf-8')).hexdigest()
        if st.session_state.get('rh_pdf_key') != report_key:
            st.session_state.pop('rh_pdf_bytes', None)
            st.session_state['rh_pdf_key'] = report_key

        if st.button("📄 Preparar PDF do Fechamento RH", type='primary', use_container_width=True, key='prepare_rh_pdf'):
            calendar.setfirstweekday(calendar.MONDAY)
            weeks = calendar.monthcalendar(ano, mes_num)
            pivot = schedule_to_pivot(df_raw, ano, mes_num)
            st.session_state['rh_pdf_bytes'] = generate_pdf_semanal(weeks, pivot, resumo, mes_nome, ano, shift_types_df)

        if st.session_state.get('rh_pdf_bytes'):
            st.download_button(
                "⬇️ Baixar PDF oficial", data=st.session_state['rh_pdf_bytes'],
                file_name=f"Fechamento_RH_Escala_{mes_nome}_{ano}.pdf", mime='application/pdf',
                use_container_width=True
            )

# =================================================================
# 15. PÁGINA — TROCAS DE PLANTÃO (SEM WORKFLOW DE APROVAÇÃO/AUDITORIA)
# =================================================================
elif page == '🔄 Trocas':
    render_period_selector()
    mes_num = int(st.session_state['period_month'])
    ano = int(st.session_state['period_year'])
    mes_nome = MESES[mes_num - 1]
    st.header(f"🔄 Trocas de plantão · {mes_nome} {ano}")
    st.caption("Ferramenta operacional para trocar dois plantões já escalados ou substituir o médico de um plantão.")
    df_raw = fetch_month_schedule(ano, mes_num)
    if df_raw.empty:
        st.info("Não há plantões escalados neste mês.")
    else:
        df_raw = df_raw.sort_values(['shift_date','shift_time']).reset_index(drop=True)
        labels = {
            i: f"{pd.Timestamp(r['shift_date']).strftime('%d/%m')} · {r['shift_time']} · {r['doctor_name']}"
            for i, r in df_raw.iterrows()
        }
        tab1, tab2 = st.tabs(["Trocar dois plantões", "Substituir médico"])
        with tab1:
            a = st.selectbox("Plantão A", list(labels.keys()), format_func=lambda x: labels[x], key='swap_a')
            b_opts = [x for x in labels if x != a]
            b = st.selectbox("Plantão B", b_opts, format_func=lambda x: labels[x], key='swap_b') if b_opts else None
            if b is not None:
                ra, rb = df_raw.loc[a], df_raw.loc[b]
                st.info(f"{ra['doctor_name']} ↔ {rb['doctor_name']}")
                if st.button("🔄 Confirmar troca", type='primary'):
                    execute_transacional([
                        ("UPDATE shift_schedule SET doctor_id=%s, doctor_name=%s WHERE shift_date=%s AND shift_time=%s",
                         (int(rb['doctor_id']), rb['doctor_name'], ra['shift_date'], ra['shift_time'])),
                        ("UPDATE shift_schedule SET doctor_id=%s, doctor_name=%s WHERE shift_date=%s AND shift_time=%s",
                         (int(ra['doctor_id']), ra['doctor_name'], rb['shift_date'], rb['shift_time'])),
                    ])
                    st.success("Plantões trocados.")
                    st.rerun()
        with tab2:
            idx = st.selectbox("Plantão", list(labels.keys()), format_func=lambda x: labels[x], key='replace_shift')
            atual = df_raw.loc[idx]
            candidatos = [n for n in active_names if n != atual['doctor_name']]
            novo_nome = st.selectbox("Novo médico", candidatos, key='replace_doc') if candidatos else None
            if novo_nome and st.button("Substituir médico", type='primary'):
                execute_query("UPDATE shift_schedule SET doctor_id=%s, doctor_name=%s WHERE shift_date=%s AND shift_time=%s",
                              (id_by_name[novo_nome], novo_nome, atual['shift_date'], atual['shift_time']))
                st.success("Substituição realizada.")
                st.rerun()

# =================================================================
# 16. PÁGINA — BACKUP COMPLETO + RESTAURAÇÃO
# =================================================================
elif page == '💾 Backup':
    st.header("💾 Backup e restauração")
    st.caption("O backup completo inclui equipe, escala, padrão rotativo, valores dos turnos e data âncora do ciclo.")
    if st.button("📦 Preparar backup completo", type='primary'):
        st.session_state['full_backup'] = create_full_backup_zip()
    if st.session_state.get('full_backup'):
        st.download_button("⬇️ Baixar backup ZIP", data=st.session_state['full_backup'],
                           file_name=f"backup_escala_{hoje.strftime('%Y%m%d')}.zip", mime='application/zip')

    st.divider()
    st.subheader("Restaurar")
    st.warning("A restauração substitui os dados atuais. O arquivo é validado antes de qualquer alteração.")
    upload = st.file_uploader("Backup ZIP completo ou CSV antigo de escala", type=['zip','csv'])
    confirm = st.checkbox("Confirmo que quero substituir os dados abrangidos pelo arquivo restaurado.")
    if upload is not None and st.button("🚨 Restaurar backup", disabled=not confirm):
        # Gera cópia de segurança em memória antes do restore e mantém para download após o rerun atual.
        st.session_state['pre_restore_backup'] = create_full_backup_zip()
        try:
            if upload.name.lower().endswith('.zip'):
                restore_backup_zip(upload)
            else:
                restore_legacy_schedule_csv(upload)
        except Exception as e:
            st.error(f"Restauração cancelada/revertida: {e}")
        else:
            st.success("Backup restaurado com sucesso.")
            st.session_state.pop('full_backup', None)
            st.rerun()
    if st.session_state.get('pre_restore_backup'):
        st.download_button("🛟 Baixar backup automático anterior à última restauração",
                           data=st.session_state['pre_restore_backup'],
                           file_name=f"backup_pre_restore_{hoje.strftime('%Y%m%d')}.zip",
                           mime='application/zip')
