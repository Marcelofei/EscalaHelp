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

st.set_page_config(
    page_title="Hospital HELP — Escala de Radiologia",
    layout="wide",
    page_icon="🩻",
    initial_sidebar_state="expanded",
)

TURNOS = ["Manhã", "Tarde", "Noite"]
MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
         "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
DIAS_SEMANA = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
DIAS_SEMANA_CURTO = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

# =================================================================
# BANCO
# =================================================================
def _database_url():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        try:
            db_url = st.secrets["DATABASE_URL"]
        except Exception:
            pass
    if not db_url:
        st.error("DATABASE_URL ausente nas configurações.")
        st.stop()
    return db_url


@st.cache_resource
def get_db_pool():
    return ThreadedConnectionPool(
        minconn=1, maxconn=8, dsn=_database_url(),
        options="-c client_encoding=utf8", connect_timeout=10,
    )


def _with_connection(callback, transactional=False):
    last_exc = None
    for attempt in range(2):
        pool = get_db_pool()
        conn = None
        returned = False
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
                    returned = True
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
            if conn is not None and not returned:
                try:
                    if not conn.closed:
                        conn.autocommit = True
                    pool.putconn(conn)
                except Exception:
                    pass
    if last_exc:
        raise last_exc


def execute_query(query, params=None):
    def _exec(conn):
        with conn.cursor() as cur:
            cur.execute(query, params)
    _with_connection(_exec)
    st.cache_data.clear()


def execute_transacional(operacoes):
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


def fetch_data(query, params=None):
    def _fetch(conn):
        with conn.cursor() as cur:
            cur.execute(query, params)
            if not cur.description:
                return pd.DataFrame()
            cols = [d[0] for d in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=cols)
    return _with_connection(_fetch)


def _add_constraint_if_missing(table, name, definition):
    execute_query(f"""
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='{name}') THEN
        ALTER TABLE {table} ADD CONSTRAINT {name} {definition};
      END IF;
    END $$;
    """)


@st.cache_resource(show_spinner=False)
def init_db():
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
    execute_query("ALTER TABLE shift_schedule ADD COLUMN IF NOT EXISTS doctor_id BIGINT;")
    execute_query("ALTER TABLE fixed_schedule_4w ADD COLUMN IF NOT EXISTS doctor_id BIGINT;")
    execute_query("""
    DO $$ DECLARE tipo_col TEXT; BEGIN
      SELECT data_type INTO tipo_col FROM information_schema.columns
      WHERE table_name='doctors' AND column_name='ativo' AND table_schema=current_schema();
      IF tipo_col IN ('integer','smallint','bigint') THEN
        ALTER TABLE doctors ALTER COLUMN ativo DROP DEFAULT;
        ALTER TABLE doctors ALTER COLUMN ativo TYPE BOOLEAN USING (ativo <> 0);
        ALTER TABLE doctors ALTER COLUMN ativo SET DEFAULT TRUE;
      END IF;
    END $$;
    """)
    execute_query("UPDATE doctors SET ativo=TRUE WHERE ativo IS NULL;")
    _add_constraint_if_missing("doctors", "uq_doctors_id", "UNIQUE (id)")

    execute_query("""
      INSERT INTO doctors (name, ativo)
      SELECT DISTINCT doctor_name, FALSE FROM shift_schedule
      WHERE doctor_name IS NOT NULL AND BTRIM(doctor_name)<>''
      ON CONFLICT (name) DO NOTHING;
    """)
    execute_query("""
      INSERT INTO doctors (name, ativo)
      SELECT DISTINCT doctor_name, FALSE FROM fixed_schedule_4w
      WHERE doctor_name IS NOT NULL AND BTRIM(doctor_name)<>''
      ON CONFLICT (name) DO NOTHING;
    """)
    execute_query("UPDATE shift_schedule s SET doctor_id=d.id FROM doctors d WHERE s.doctor_id IS NULL AND s.doctor_name=d.name;")
    execute_query("UPDATE fixed_schedule_4w s SET doctor_id=d.id FROM doctors d WHERE s.doctor_id IS NULL AND s.doctor_name=d.name;")

    _add_constraint_if_missing("shift_schedule", "ck_shift_schedule_turno", "CHECK (shift_time IN ('Manhã','Tarde','Noite'))")
    _add_constraint_if_missing("fixed_schedule_4w", "ck_fixed_turno", "CHECK (shift_time IN ('Manhã','Tarde','Noite'))")
    _add_constraint_if_missing("fixed_schedule_4w", "ck_fixed_week", "CHECK (week_num BETWEEN 0 AND 3)")
    _add_constraint_if_missing("fixed_schedule_4w", "ck_fixed_weekday", "CHECK (weekday BETWEEN 0 AND 6)")
    _add_constraint_if_missing("shift_schedule", "fk_shift_doctor", "FOREIGN KEY (doctor_id) REFERENCES doctors(id)")
    _add_constraint_if_missing("fixed_schedule_4w", "fk_fixed_doctor", "FOREIGN KEY (doctor_id) REFERENCES doctors(id)")
    execute_query("CREATE INDEX IF NOT EXISTS idx_shift_schedule_date ON shift_schedule(shift_date);")
    execute_query("CREATE INDEX IF NOT EXISTS idx_shift_schedule_doctor ON shift_schedule(doctor_id, shift_date);")

    execute_query("""
      CREATE TABLE IF NOT EXISTS shift_types (
        name VARCHAR(10) PRIMARY KEY,
        start_time TIME NOT NULL,
        end_time TIME NOT NULL,
        value NUMERIC(12,2) NOT NULL CHECK (value >= 0)
      );
    """)
    execute_query("""
      INSERT INTO shift_types(name,start_time,end_time,value) VALUES
      ('Manhã','07:00','13:00',750),('Tarde','13:00','19:00',750),('Noite','19:00','07:00',1500)
      ON CONFLICT(name) DO NOTHING;
    """)
    execute_query("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    execute_query("""
      INSERT INTO app_config(key,value) VALUES('rotation_anchor_date',%s)
      ON CONFLICT(key) DO NOTHING;
    """, (monday.isoformat(),))
    return True


try:
    init_db()
except Exception as e:
    st.error("🚨 Falha Crítica: Banco de Dados Inacessível.")
    st.code(str(e))
    st.stop()

# =================================================================
# DADOS / REGRAS
# =================================================================
def month_bounds(ano, mes):
    ini = datetime.date(ano, mes, 1)
    fim = datetime.date(ano + 1, 1, 1) if mes == 12 else datetime.date(ano, mes + 1, 1)
    return ini, fim


@st.cache_data(ttl=30, show_spinner=False)
def fetch_doctors():
    return fetch_data("SELECT id,name,ativo FROM doctors ORDER BY ativo DESC,name")


@st.cache_data(ttl=20, show_spinner=False)
def fetch_month_schedule(ano, mes):
    ini, fim = month_bounds(ano, mes)
    return fetch_data("""
      SELECT s.shift_date,s.shift_time,s.doctor_id,COALESCE(d.name,s.doctor_name) AS doctor_name
      FROM shift_schedule s LEFT JOIN doctors d ON d.id=s.doctor_id
      WHERE s.shift_date >= %s AND s.shift_date < %s
      ORDER BY s.shift_date,s.shift_time
    """, (ini, fim))


@st.cache_data(ttl=60, show_spinner=False)
def fetch_fixed_pattern():
    return fetch_data("""
      SELECT f.week_num,f.weekday,f.shift_time,f.doctor_id,COALESCE(d.name,f.doctor_name) AS doctor_name
      FROM fixed_schedule_4w f LEFT JOIN doctors d ON d.id=f.doctor_id
      WHERE f.doctor_id IS NOT NULL
      ORDER BY f.week_num,f.weekday,f.shift_time
    """)


@st.cache_data(ttl=300, show_spinner=False)
def get_shift_types():
    df = fetch_data("SELECT name,start_time,end_time,value FROM shift_types ORDER BY CASE name WHEN 'Manhã' THEN 1 WHEN 'Tarde' THEN 2 ELSE 3 END")
    if not df.empty:
        df['value'] = df['value'].astype(float)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def get_rotation_anchor():
    df = fetch_data("SELECT value FROM app_config WHERE key='rotation_anchor_date'")
    if df.empty:
        today = datetime.date.today()
        return today - datetime.timedelta(days=today.weekday())
    return datetime.date.fromisoformat(str(df.iloc[0]['value']))


def set_rotation_anchor(data):
    monday = data - datetime.timedelta(days=data.weekday())
    execute_query("""
      INSERT INTO app_config(key,value) VALUES('rotation_anchor_date',%s)
      ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
    """, (monday.isoformat(),))
    return monday


def cycle_week_for_date(data, anchor):
    monday = data - datetime.timedelta(days=data.weekday())
    return ((monday - anchor).days // 7) % 4


def build_pattern_assignments(ano, mes, df_fix, anchor):
    fix_map = {}
    if not df_fix.empty:
        for _, r in df_fix.iterrows():
            if pd.notna(r.get('doctor_id')):
                fix_map[(int(r['week_num']), int(r['weekday']), r['shift_time'])] = (int(r['doctor_id']), str(r['doctor_name']))
    regs = []
    for day in range(1, calendar.monthrange(ano, mes)[1] + 1):
        dt = datetime.date(ano, mes, day)
        week = cycle_week_for_date(dt, anchor)
        for turno in TURNOS:
            info = fix_map.get((week, dt.weekday(), turno))
            if info:
                regs.append((dt, turno, info[0], info[1]))
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


def current_state_from_edits(all_edits, ano, mes):
    rows = []
    for week_idx, (days, ed) in enumerate(all_edits):
        for idx, day in enumerate(days):
            if day <= 0:
                continue
            dt = datetime.date(ano, mes, day)
            for row_idx, turno in enumerate(TURNOS):
                col = f"w{week_idx}_d{idx}"
                nome = str(ed.at[row_idx, col]).strip() if pd.notna(ed.at[row_idx, col]) else ""
                if nome:
                    rows.append((dt, turno, nome))
    return rows


def financial_summary_from_rows(rows, shift_types_df):
    value_map = {r['name']: float(r['value']) for _, r in shift_types_df.iterrows()}
    if not rows:
        return pd.DataFrame(columns=['doctor_name','Manhã','Tarde','Noite','Total_Plantões','Total'])
    df = pd.DataFrame(rows, columns=['shift_date','shift_time','doctor_name'])
    df['valor'] = df['shift_time'].map(value_map).fillna(0.0)
    counts = df.pivot_table(index='doctor_name', columns='shift_time', values='shift_date', aggfunc='count', fill_value=0)
    for t in TURNOS:
        if t not in counts.columns:
            counts[t] = 0
    counts = counts[TURNOS]
    counts['Total_Plantões'] = counts.sum(axis=1)
    counts['Total'] = df.groupby('doctor_name')['valor'].sum()
    return counts.reset_index().sort_values('doctor_name')


def ics_escape(text):
    return str(text).replace('\\','\\\\').replace(';','\\;').replace(',','\\,').replace('\n','\\n')


def generate_ics(df_personal, doctor_name, shift_types_df):
    config = {r['name']: r for _, r in shift_types_df.iterrows()}
    now_utc = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    lines = ["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//Hospital HELP//Escala Radiologia//PT-BR","CALSCALE:GREGORIAN"]
    for _, r in df_personal.iterrows():
        if r['shift_time'] not in config:
            continue
        cfg = config[r['shift_time']]
        dt = pd.Timestamp(r['shift_date']).date()
        start_t = cfg['start_time'] if not isinstance(cfg['start_time'], str) else datetime.time.fromisoformat(cfg['start_time'])
        end_t = cfg['end_time'] if not isinstance(cfg['end_time'], str) else datetime.time.fromisoformat(cfg['end_time'])
        start_dt = datetime.datetime.combine(dt, start_t)
        end_date = dt + datetime.timedelta(days=1) if end_t <= start_t else dt
        end_dt = datetime.datetime.combine(end_date, end_t)
        uid = hashlib.sha1(f"{dt}-{r['shift_time']}-{doctor_name}".encode()).hexdigest() + "@hospital-help"
        lines.extend([
            "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{now_utc}",
            f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}", f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{ics_escape('Plantão Radiologia — ' + r['shift_time'])}",
            f"DESCRIPTION:{ics_escape('Hospital HELP — ' + doctor_name)}", "LOCATION:Hospital HELP", "END:VEVENT"
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines).encode('utf-8')

# =================================================================
# INTERAÇÕES RÁPIDAS DA ESCALA
# =================================================================
def claim_shift_atomic(shift_date, shift_time, doctor_id, doctor_name):
    def _claim(conn):
        with conn.cursor() as cur:
            cur.execute("""
              INSERT INTO shift_schedule(shift_date,shift_time,doctor_id,doctor_name)
              VALUES(%s,%s,%s,%s)
              ON CONFLICT(shift_date,shift_time) DO NOTHING
              RETURNING doctor_id
            """, (shift_date, shift_time, int(doctor_id), doctor_name))
            if cur.fetchone():
                return True, doctor_name
            cur.execute("""
              SELECT COALESCE(d.name,s.doctor_name) FROM shift_schedule s
              LEFT JOIN doctors d ON d.id=s.doctor_id
              WHERE s.shift_date=%s AND s.shift_time=%s
            """, (shift_date, shift_time))
            row = cur.fetchone()
            return False, row[0] if row else "outro médico"
    result = _with_connection(_claim, transactional=True)
    fetch_month_schedule.clear()
    return result


def replace_occupied_shift_atomic(shift_date, shift_time, expected_owner_id, expected_owner_name, doctor_id, doctor_name):
    def _replace(conn):
        with conn.cursor() as cur:
            if expected_owner_id is not None and not pd.isna(expected_owner_id):
                cur.execute("""
                  UPDATE shift_schedule SET doctor_id=%s,doctor_name=%s
                  WHERE shift_date=%s AND shift_time=%s AND doctor_id=%s RETURNING doctor_id
                """, (int(doctor_id), doctor_name, shift_date, shift_time, int(expected_owner_id)))
            else:
                cur.execute("""
                  UPDATE shift_schedule SET doctor_id=%s,doctor_name=%s
                  WHERE shift_date=%s AND shift_time=%s AND doctor_id IS NULL AND doctor_name=%s RETURNING doctor_id
                """, (int(doctor_id), doctor_name, shift_date, shift_time, expected_owner_name))
            if cur.fetchone():
                return True, doctor_name
            cur.execute("""
              SELECT COALESCE(d.name,s.doctor_name) FROM shift_schedule s
              LEFT JOIN doctors d ON d.id=s.doctor_id
              WHERE s.shift_date=%s AND s.shift_time=%s
            """, (shift_date, shift_time))
            row = cur.fetchone()
            return False, row[0] if row else "vaga já alterada"
    result = _with_connection(_replace, transactional=True)
    fetch_month_schedule.clear()
    return result


def swap_with_my_shift_atomic(target_date, target_time, target_owner_id, my_date, my_time, my_doctor_id):
    def _swap(conn):
        with conn.cursor() as cur:
            cur.execute("""
              SELECT shift_date,shift_time,doctor_id,doctor_name FROM shift_schedule
              WHERE (shift_date=%s AND shift_time=%s) OR (shift_date=%s AND shift_time=%s)
              ORDER BY shift_date, shift_time
              FOR UPDATE
            """, (target_date, target_time, my_date, my_time))
            rows = cur.fetchall()
            state = {(r[0], r[1]): (r[2], r[3]) for r in rows}
            target = state.get((target_date, target_time)); mine = state.get((my_date, my_time))
            if not target or not mine:
                return False, "Um dos plantões foi alterado antes da confirmação."
            if target_owner_id is not None and not pd.isna(target_owner_id) and target[0] != int(target_owner_id):
                return False, "O plantão escolhido mudou de médico."
            if mine[0] != int(my_doctor_id):
                return False, "Seu plantão escolhido mudou antes da troca."
            cur.execute("UPDATE shift_schedule SET doctor_id=%s,doctor_name=%s WHERE shift_date=%s AND shift_time=%s",
                        (mine[0], mine[1], target_date, target_time))
            cur.execute("UPDATE shift_schedule SET doctor_id=%s,doctor_name=%s WHERE shift_date=%s AND shift_time=%s",
                        (target[0], target[1], my_date, my_time))
            return True, "Troca realizada."
    result = _with_connection(_swap, transactional=True)
    fetch_month_schedule.clear()
    return result


def apply_admin_diff(changes, id_by_name):
    """Aplica em uma única transação só as células que o admin de fato alterou
    no editor em lote, cada uma condicionada a ainda estar no valor que o
    editor tinha quando foi aberto (concorrência otimista). Substitui o antigo
    DELETE do mês inteiro + INSERT: aquele padrão apagava qualquer turno
    assumido por outro usuário (calendário rápido) enquanto o editor estava
    aberto, porque reescrevia o mês inteiro a partir de um snapshot desatualizado.

    changes: lista de (data, turno, snap_doctor_id, snap_doctor_name, novo_nome)
    Retorna (aplicadas, conflitos).
    """
    aplicadas = []
    conflitos = []

    def _run(conn):
        with conn.cursor() as cur:
            for dt, turno, snap_id, snap_name, new_name in changes:
                ok = False
                if new_name == "":
                    if snap_id is not None:
                        cur.execute(
                            "DELETE FROM shift_schedule WHERE shift_date=%s AND shift_time=%s AND doctor_id=%s RETURNING doctor_id",
                            (dt, turno, snap_id),
                        )
                    else:
                        cur.execute(
                            "DELETE FROM shift_schedule WHERE shift_date=%s AND shift_time=%s AND doctor_id IS NULL AND doctor_name=%s RETURNING doctor_id",
                            (dt, turno, snap_name),
                        )
                    ok = cur.fetchone() is not None
                else:
                    new_id = id_by_name.get(new_name)
                    if snap_id is not None:
                        cur.execute(
                            "UPDATE shift_schedule SET doctor_id=%s, doctor_name=%s WHERE shift_date=%s AND shift_time=%s AND doctor_id=%s RETURNING doctor_id",
                            (new_id, new_name, dt, turno, snap_id),
                        )
                        ok = cur.fetchone() is not None
                    elif snap_name:
                        cur.execute(
                            "UPDATE shift_schedule SET doctor_id=%s, doctor_name=%s WHERE shift_date=%s AND shift_time=%s AND doctor_id IS NULL AND doctor_name=%s RETURNING doctor_id",
                            (new_id, new_name, dt, turno, snap_name),
                        )
                        ok = cur.fetchone() is not None
                    else:
                        cur.execute(
                            "INSERT INTO shift_schedule (shift_date, shift_time, doctor_id, doctor_name) VALUES (%s,%s,%s,%s) ON CONFLICT (shift_date, shift_time) DO NOTHING RETURNING doctor_id",
                            (dt, turno, new_id, new_name),
                        )
                        ok = cur.fetchone() is not None

                if ok:
                    aplicadas.append((dt, turno, new_name))
                else:
                    cur.execute(
                        """
                        SELECT COALESCE(d.name, s.doctor_name)
                        FROM shift_schedule s LEFT JOIN doctors d ON d.id = s.doctor_id
                        WHERE s.shift_date=%s AND s.shift_time=%s;
                        """,
                        (dt, turno),
                    )
                    row = cur.fetchone()
                    atual = row[0] if row else "vazio"
                    conflitos.append((dt, turno, snap_name or "vazio", new_name or "vazio", atual))

    _with_connection(_run, transactional=True)
    fetch_month_schedule.clear()
    return aplicadas, conflitos


def edited_grid_to_map(all_edits, ano, mes):
    """Mapa (data, turno) -> nome ('' quando vazio) para TODAS as células do
    editor, preenchidas ou não. Necessário pro diff em apply_admin_diff, que
    precisa saber tanto do que foi preenchido quanto do que foi apagado."""
    edited = {}
    for week_idx, (days, ed) in enumerate(all_edits):
        for idx, day in enumerate(days):
            if day <= 0:
                continue
            dt = datetime.date(ano, mes, day)
            for row_idx, turno in enumerate(TURNOS):
                col = f"w{week_idx}_d{idx}"
                val = ed.at[row_idx, col]
                nome = "" if pd.isna(val) else str(val).strip()
                edited[(dt, turno)] = nome
    return edited


BACKUP_TABLES = {
    'doctors.csv': "SELECT id,name,ativo FROM doctors ORDER BY id",
    'shift_schedule.csv': "SELECT shift_date,shift_time,doctor_id,doctor_name FROM shift_schedule ORDER BY shift_date,shift_time",
    'fixed_schedule_4w.csv': "SELECT week_num,weekday,shift_time,doctor_id,doctor_name FROM fixed_schedule_4w ORDER BY week_num,weekday,shift_time",
    'shift_types.csv': "SELECT name,start_time,end_time,value FROM shift_types ORDER BY name",
    'app_config.csv': "SELECT key,value FROM app_config ORDER BY key",
}


def create_full_backup_zip():
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('metadata.json', json.dumps({'schema_version':2,'created_at':datetime.datetime.now().isoformat()}, indent=2))
        for filename, query in BACKUP_TABLES.items():
            zf.writestr(filename, fetch_data(query).to_csv(index=False))
    return bio.getvalue()


def restore_backup_zip(uploaded):
    """Valida o ZIP inteiro antes de substituir qualquer dado."""
    with zipfile.ZipFile(io.BytesIO(uploaded.getvalue()), 'r') as zf:
        missing = set(BACKUP_TABLES) - set(zf.namelist())
        if missing:
            raise ValueError("Backup incompleto: " + ", ".join(sorted(missing)))
        data = {name: pd.read_csv(zf.open(name)) for name in BACKUP_TABLES}

    required = {
        'doctors.csv': {'id','name','ativo'},
        'shift_schedule.csv': {'shift_date','shift_time','doctor_id'},
        'fixed_schedule_4w.csv': {'week_num','weekday','shift_time','doctor_id'},
        'shift_types.csv': {'name','start_time','end_time','value'},
        'app_config.csv': {'key','value'},
    }
    for filename, cols in required.items():
        missing_cols = cols - set(data[filename].columns)
        if missing_cols:
            raise ValueError(f"{filename} sem coluna(s): {', '.join(sorted(missing_cols))}")

    valid_turnos = set(TURNOS)
    for filename in ('shift_schedule.csv','fixed_schedule_4w.csv'):
        turnos = set(data[filename]['shift_time'].dropna().astype(str).unique())
        invalid = turnos - valid_turnos
        if invalid:
            raise ValueError(f"{filename} contém turno(s) inválido(s): {', '.join(sorted(invalid))}")

    docs = [(int(r['id']), str(r['name']).strip(), str(r['ativo']).lower() in ('true','1','t','yes'))
            for _, r in data['doctors.csv'].iterrows()]
    if any(not name for _, name, _ in docs):
        raise ValueError("doctors.csv contém nome vazio.")
    doc_ids = {x[0] for x in docs}

    sched = []
    for _, r in data['shift_schedule.csv'].iterrows():
        did = int(r['doctor_id'])
        if did not in doc_ids:
            raise ValueError(f"shift_schedule.csv referencia doctor_id inexistente: {did}")
        sched.append((pd.to_datetime(r['shift_date']).date(), str(r['shift_time']), did,
                      None if pd.isna(r.get('doctor_name')) else str(r.get('doctor_name'))))

    fixed = []
    for _, r in data['fixed_schedule_4w.csv'].iterrows():
        if pd.isna(r.get('doctor_id')):
            continue
        did = int(r['doctor_id'])
        week_num, weekday = int(r['week_num']), int(r['weekday'])
        if did not in doc_ids:
            raise ValueError(f"fixed_schedule_4w.csv referencia doctor_id inexistente: {did}")
        if not 0 <= week_num <= 3 or not 0 <= weekday <= 6:
            raise ValueError("fixed_schedule_4w.csv contém semana/dia inválido.")
        fixed.append((week_num, weekday, str(r['shift_time']), did,
                      None if pd.isna(r.get('doctor_name')) else str(r.get('doctor_name'))))

    types = []
    for _, r in data['shift_types.csv'].iterrows():
        value = float(r['value'])
        if value < 0:
            raise ValueError("shift_types.csv contém valor negativo.")
        # Também valida o formato dos horários antes de iniciar a transação destrutiva.
        datetime.time.fromisoformat(str(r['start_time']))
        datetime.time.fromisoformat(str(r['end_time']))
        types.append((str(r['name']), str(r['start_time']), str(r['end_time']), value))
    cfg = [(str(r['key']), str(r['value'])) for _, r in data['app_config.csv'].iterrows()]

    execute_transacional([
        ("DELETE FROM shift_schedule", None), ("DELETE FROM fixed_schedule_4w", None), ("DELETE FROM doctors", None),
        ("DELETE FROM shift_types", None), ("DELETE FROM app_config", None),
        ("INSERT INTO doctors(id,name,ativo) VALUES %s", docs),
        ("INSERT INTO shift_schedule(shift_date,shift_time,doctor_id,doctor_name) VALUES %s", sched),
        ("INSERT INTO fixed_schedule_4w(week_num,weekday,shift_time,doctor_id,doctor_name) VALUES %s", fixed),
        ("INSERT INTO shift_types(name,start_time,end_time,value) VALUES %s", types),
        ("INSERT INTO app_config(key,value) VALUES %s", cfg),
        ("SELECT setval(pg_get_serial_sequence('doctors','id'),COALESCE((SELECT MAX(id) FROM doctors),1),true)", None),
    ])


def restore_legacy_schedule_csv(uploaded):
    """Compatibilidade com o antigo backup CSV contendo apenas a escala."""
    uploaded.seek(0)
    df = pd.read_csv(uploaded)
    required = {'shift_date','shift_time','doctor_name'}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("CSV antigo sem coluna(s): " + ", ".join(sorted(missing)))
    invalid = set(df['shift_time'].dropna().astype(str).unique()) - set(TURNOS)
    if invalid:
        raise ValueError("CSV contém turno(s) inválido(s): " + ", ".join(sorted(invalid)))

    df = df.copy()
    df['doctor_name'] = df['doctor_name'].fillna('').astype(str).str.strip()
    df = df[df['doctor_name'] != '']
    names = sorted(df['doctor_name'].unique().tolist())

    # Primeiro garante os médicos, sem apagar configurações, padrão ou valores.
    for name in names:
        execute_query(
            "INSERT INTO doctors(name,ativo) VALUES(%s,TRUE) ON CONFLICT(name) DO NOTHING",
            (name,)
        )
    docs = fetch_data("SELECT id,name FROM doctors")
    id_by = {str(r['name']): int(r['id']) for _, r in docs.iterrows()}

    rows = []
    seen = set()
    for _, r in df.iterrows():
        dt = pd.to_datetime(r['shift_date']).date()
        turno = str(r['shift_time'])
        key = (dt, turno)
        if key in seen:
            raise ValueError(f"CSV contém plantão duplicado em {dt:%d/%m/%Y} · {turno}.")
        seen.add(key)
        name = str(r['doctor_name'])
        rows.append((dt, turno, id_by[name], name))

    # CSV legado restaura somente a escala, como o backup antigo fazia.
    execute_transacional([
        ("DELETE FROM shift_schedule", None),
        ("INSERT INTO shift_schedule(shift_date,shift_time,doctor_id,doctor_name) VALUES %s", rows),
    ])

# =================================================================
# PDF
# =================================================================
def generate_pdf_semanal(weeks, pivot, resumo, mes, ano, shift_types_df):
    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.add_page(); pdf.set_font('Arial','B',16); pdf.set_text_color(0,45,98)
    pdf.cell(0,10,f"HOSPITAL HELP - ESCALA RADIOLOGIA - {mes.upper()} / {ano}",ln=True,align='C'); pdf.ln(2)
    headers = ['Seg','Ter','Qua','Qui','Sex','Sab','Dom']; label_w=25; day_w=(pdf.w-pdf.l_margin-pdf.r_margin-label_w)/7
    for i, week in enumerate(weeks):
        pdf.set_font('Arial','B',8); pdf.set_fill_color(0,45,98); pdf.set_text_color(255,255,255)
        pdf.cell(label_w,6,'Turno',1,0,'C',True)
        for idx, day in enumerate(week):
            pdf.cell(day_w,6,(f"{headers[idx]} {day:02d}" if day else headers[idx]),1,0,'C',True)
        pdf.ln()
        for turno in TURNOS:
            pdf.set_font('Arial','B',8); pdf.set_text_color(0,45,98); pdf.set_fill_color(240,240,240)
            pdf.cell(label_w,6,turno.replace('ã','a'),1,0,'C',True); pdf.set_font('Arial','',8); pdf.set_text_color(0,0,0)
            for day in week:
                nome = '-' if not day else str(pivot.at[turno, day] if day in pivot.columns else '')
                pdf.cell(day_w,6,nome[:20],1,0,'C')
            pdf.ln()
        pdf.ln(3)
    pdf.add_page(); pdf.set_font('Arial','B',14); pdf.set_text_color(0,45,98)
    pdf.cell(0,10,'FECHAMENTO FINANCEIRO - RH',ln=True); pdf.ln(3)
    widths=[90,25,25,25,30,40]; hdr=['Medico','Manha','Tarde','Noite','Plantoes','Total (R$)']
    pdf.set_font('Arial','B',9); pdf.set_fill_color(0,45,98); pdf.set_text_color(255,255,255)
    for w,h in zip(widths,hdr): pdf.cell(w,8,h,1,0,'C',True)
    pdf.ln(); pdf.set_font('Arial','',9); pdf.set_text_color(0,0,0); total_geral=0.0
    for _, r in resumo.iterrows():
        vals=[str(r['doctor_name'])[:36],str(int(r['Manhã'])),str(int(r['Tarde'])),str(int(r['Noite'])),str(int(r['Total_Plantões']))]
        for w,v in zip(widths[:-1],vals): pdf.cell(w,8,v,1,0,'L' if w==widths[0] else 'C')
        total=float(r['Total']); total_geral+=total; pdf.cell(widths[-1],8,f"{total:,.2f}".replace(',','X').replace('.',',').replace('X','.'),1,1,'R')
    pdf.set_font('Arial','B',9); pdf.cell(sum(widths[:-1]),8,'TOTAL GERAL',1,0,'R'); pdf.cell(widths[-1],8,f"{total_geral:,.2f}".replace(',','X').replace('.',',').replace('X','.'),1,1,'R')
    out=pdf.output(dest='S'); return out.encode('latin-1') if isinstance(out,str) else bytes(out)

# =================================================================
# VISUAL
# =================================================================
def aplicar_estilo_visual():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');
    :root,.stApp{--background-color:#0D1420!important;--secondary-background-color:#111A29!important;--text-color:#E6EAF2!important;--primary-color:#3B82F6!important}
    html,body,.stApp,[data-testid="stAppViewContainer"],[data-testid="stMain"],[data-testid="stHeader"],.main{background:#0D1420!important}
    [data-testid="stHeader"]{background:transparent!important} html,body,[class*="css"]{font-family:'Inter',sans-serif}
    h1,h2,h3,h4,h5,h6{font-family:'Inter',sans-serif!important;color:#F4F6FA!important;font-weight:700!important}
    .stApp label,.stApp .stMarkdown,.stApp .stMarkdown p,.stApp [data-testid="stWidgetLabel"] p{color:#E6EAF2!important}
    .stApp [data-testid="stCaptionContainer"]{color:#7B8AA3!important}
    section[data-testid="stSidebar"]{background:#111A29!important;border-right:1px solid #1E2A3D}
    .stApp [data-baseweb="select"]>div,.stApp [data-baseweb="input"]>div,.stApp input,.stApp textarea{background:#0D1420!important;border:1px solid #26324A!important;color:#E6EAF2!important;border-radius:8px!important}
    .stButton button{border-radius:8px!important;font-weight:600!important}
    div[data-testid="stMetric"]{background:#111A29!important;border:1px solid #1E2A3D;border-radius:12px;padding:.9rem 1.1rem}
    div[data-testid="stMetricValue"]{font-family:'JetBrains Mono',monospace!important;color:#F4F6FA!important}
    div[data-testid="stExpander"],div[data-testid="stPopover"]{border:1px solid #1E2A3D!important;border-radius:12px!important;background:#111A29!important}
    .nav-eyebrow{font-size:.68rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:#5C6A84!important;margin:1rem 0 .5rem .1rem}
    .sidebar-brand{display:flex;align-items:center;gap:12px;padding:6px 0 14px}.sidebar-brand .badge{background:linear-gradient(135deg,#2563EB,#1D4ED8);color:white;font-weight:800;width:44px;height:44px;border-radius:10px;display:flex;align-items:center;justify-content:center}.sidebar-brand .nome{font-weight:700}.sidebar-brand .depto{color:#7B8AA3;font-size:.78rem}
    .period-hero{text-align:center;padding:4px 0 6px}.period-hero .eyebrow{color:#64748B;font-size:.68rem;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.period-hero .title{color:#F8FAFC;font-size:1.7rem;font-weight:800;letter-spacing:-.03em}
    .turno-legend{display:flex;gap:18px;margin:4px 0 14px;flex-wrap:wrap}.turno-legend .item{display:flex;align-items:center;gap:7px;font-size:.82rem;color:#AAB4C8}.turno-legend .dot{width:8px;height:8px;border-radius:50%}.dot-manha{background:#F59E0B}.dot-tarde{background:#06B6D4}.dot-noite{background:#8B5CF6}

    /* Identificação principal */
    .identity-panel-anchor{display:none}.identity-panel-head{display:flex;align-items:center;gap:12px;padding:5px 2px 10px}.identity-icon{width:44px;height:44px;display:flex;align-items:center;justify-content:center;border-radius:12px;background:linear-gradient(135deg,#2563EB,#1D4ED8);font-size:1.25rem}.identity-eyebrow{color:#93C5FD;font-size:.64rem;font-weight:900;letter-spacing:.10em}.identity-title{color:#F8FAFC;font-size:1.25rem;font-weight:800}.identity-subtitle{color:#A8B6CC;font-size:.78rem}
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.identity-panel-anchor){border:1px solid #3B82F6!important;background:linear-gradient(135deg,rgba(37,99,235,.16),rgba(17,26,41,.98))!important;box-shadow:0 0 0 1px rgba(59,130,246,.10),0 8px 26px rgba(37,99,235,.12)!important;padding:.35rem .45rem .5rem!important;margin:.25rem 0 1rem!important}
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.identity-panel-anchor) [data-baseweb="select"]>div{min-height:52px!important;border:2px solid #3B82F6!important;background:#0B1526!important;font-size:1rem!important}

    /* GRID UNIFORME DO CALENDÁRIO RÁPIDO — tamanho estrutural, não cosmético */
    [class*="st-key-calday_"] [data-testid="stVerticalBlockBorderWrapper"],
    [class*="st-key-calself_"] [data-testid="stVerticalBlockBorderWrapper"],
    [class*="st-key-calempty_"] [data-testid="stVerticalBlockBorderWrapper"],
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.quick-day-title){
        height:230px!important;min-height:230px!important;max-height:230px!important;
        box-sizing:border-box!important;
        background:#111A29!important;
        border:1px solid #334155!important;
        border-radius:11px!important;
        overflow:hidden!important;
    }
    /* Fallback + regra direta: o DIA INTEIRO muda de tom quando há Você. */
    [class*="st-key-calself_"] [data-testid="stVerticalBlockBorderWrapper"],
    [class*="st-key-calself_"],
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.quick-day-title.day-has-self){
        background:linear-gradient(180deg,rgba(37,99,235,.24),rgba(15,23,38,.98))!important;
        border-color:#3B82F6!important;
        box-shadow:0 0 0 1px rgba(59,130,246,.18) inset,0 7px 18px rgba(37,99,235,.10)!important;
    }
    /* O bloco interno sempre começa no topo e usa o MESMO gap entre data/turnos. */
    [class*="st-key-calday_"] [data-testid="stVerticalBlock"],
    [class*="st-key-calself_"] [data-testid="stVerticalBlock"],
    [class*="st-key-calempty_"] [data-testid="stVerticalBlock"],
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.quick-day-title) [data-testid="stVerticalBlock"]{
        gap:8px!important;
        justify-content:flex-start!important;
        align-content:flex-start!important;
        padding:0!important;
    }
    /* Data: altura invariável e sempre ancorada no topo. */
    .quick-day-title{
        height:28px!important;min-height:28px!important;max-height:28px!important;
        display:flex!important;align-items:center!important;
        margin:0!important;padding:0!important;
        color:#F8FAFC;font-size:1rem;font-weight:800;line-height:1!important;
        overflow:hidden!important;white-space:nowrap!important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.quick-day-title) [data-testid="stMarkdownContainer"] p{margin:0!important;}
    /* Cada um dos 3 turnos ocupa exatamente 40px. O gap vem do pai (8px). */
    [class*="st-key-calday_"] .stButton,
    [class*="st-key-calday_"] .stPopover,
    [class*="st-key-calself_"] .stButton,
    [class*="st-key-calself_"] .stPopover,
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.quick-day-title) .stButton,
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.quick-day-title) .stPopover{
        height:40px!important;min-height:40px!important;max-height:40px!important;
        margin:0!important;padding:0!important;
    }
    [class*="st-key-calday_"] button,
    [class*="st-key-calself_"] button,
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.quick-day-title) button{
        width:100%!important;
        height:40px!important;min-height:40px!important;max-height:40px!important;
        margin:0!important;padding:.2rem .48rem!important;
        border-radius:10px!important;
    }
    [class*="st-key-calday_"] button p,
    [class*="st-key-calself_"] button p,
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.quick-day-title) button p{
        margin:0!important;line-height:1!important;
        white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;
        font-size:.79rem!important;
    }
    /* Você tem o mesmo 40px dos demais slots e nenhum conteúdo extra. */
    div[data-testid="stMarkdownContainer"]:has(.quick-self-slot){
        height:40px!important;min-height:40px!important;max-height:40px!important;
        margin:0!important;padding:0!important;
    }
    .quick-self-slot{
        display:flex;align-items:center;gap:7px;width:100%;box-sizing:border-box;
        height:40px!important;min-height:40px!important;max-height:40px!important;
        margin:0!important;padding:5px 10px;border-radius:10px;
        background:linear-gradient(135deg,rgba(37,99,235,.34),rgba(29,78,216,.20));
        border:1px solid #3B82F6;border-left:3px solid #60A5FA;
        box-shadow:0 0 0 1px rgba(59,130,246,.08) inset;
        overflow:hidden;
    }
    .quick-self-slot .self-emoji{flex:0 0 auto}
    .quick-self-slot .self-check{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;background:#3B82F6;color:#FFF;font-size:.72rem;font-weight:900;flex:0 0 18px}
    .quick-self-slot .self-label{color:#F8FBFF;font-weight:800;font-size:.88rem;white-space:nowrap}
    .quick-day-spacer{height:40px!important;min-height:40px!important;max-height:40px!important;margin:0!important;padding:0!important;}
    .month-summary{color:#7B8AA3;font-size:.82rem;margin:.15rem 0 .6rem}
    .block-container{padding-top:.8rem!important}
    @media(max-width:700px){.block-container{padding-left:.65rem!important;padding-right:.65rem!important}.period-hero .title{font-size:1.45rem}}
    </style>
    """, unsafe_allow_html=True)


aplicar_estilo_visual()

# =================================================================
# LOGIN — MANTIDO COMO ANTES
# =================================================================
if 'auth' not in st.session_state:
    st.session_state['auth'] = False
if not st.session_state['auth']:
    c = st.columns([1,1.2,1])[1]
    with c:
        st.markdown("<div style='text-align:center;margin-top:8vh'><h2>Hospital HELP</h2><p style='color:#7B8AA3'>Gestão de Escala — Radiologia</p></div>", unsafe_allow_html=True)
        pw = st.text_input("Senha de Acesso", type='password', label_visibility='collapsed', placeholder='Senha de acesso')
        if st.button("Entrar", use_container_width=True, type='primary'):
            if hashlib.sha256(str.encode(pw)).hexdigest() == "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4":
                st.session_state['auth'] = True; st.rerun()
            st.error("Senha incorreta.")
    st.stop()

# =================================================================
# ESTADO / NAVEGAÇÃO
# =================================================================
try:
    df_docs = fetch_doctors()
except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
    # Falha de conexão com o banco DEPOIS do login não tinha nenhum tratamento
    # aqui -- qualquer soluço (banco sob carga, pooler indisponível, rede)
    # derrubava a página inteira com stack trace cru pro usuário. Ver
    # conversa: erro real observado foi EAUTHQUERY / timeout por saturação
    # de Disk IO no Supabase (compute nano).
    get_db_pool.clear()
    st.error("🚨 Sem conexão com o banco de dados no momento.")
    st.caption("O banco pode estar sob carga ou há uma instabilidade de rede temporária. Tente novamente em instantes.")
    with st.expander("Detalhes técnicos"):
        st.code(str(e))
    if st.button("🔄 Tentar novamente", type="primary"):
        st.rerun()
    st.stop()

if not df_docs.empty:
    df_docs['ativo'] = df_docs['ativo'].fillna(False).astype(bool)
active_names = df_docs[df_docs['ativo']]['name'].tolist() if not df_docs.empty else []
all_names = df_docs['name'].tolist() if not df_docs.empty else []
id_by_name = {r['name']: int(r['id']) for _,r in df_docs.iterrows()} if not df_docs.empty else {}
name_by_id = {int(r['id']): r['name'] for _,r in df_docs.iterrows()} if not df_docs.empty else {}

hoje = datetime.date.today()
st.session_state.setdefault('period_month', hoje.month)
st.session_state.setdefault('period_year', hoje.year)
st.session_state.setdefault('page', '📅 Escala')
st.session_state.setdefault('scale_edit_mode', False)
st.session_state.setdefault('show_pattern_preview', False)


def _set_page(label): st.session_state['page'] = label

def _period_changed():
    st.session_state['show_pattern_preview'] = False
    st.session_state['scale_edit_mode'] = False
    st.session_state.pop('rh_pdf', None)
    st.session_state.pop('rh_pdf_key', None)

def _shift_period(delta):
    mes=int(st.session_state['period_month'])+delta; ano=int(st.session_state['period_year'])
    if mes<1: mes,ano=12,ano-1
    if mes>12: mes,ano=1,ano+1
    st.session_state['period_month']=mes; st.session_state['period_year']=ano; _period_changed()

def _ir_para_hoje():
    st.session_state['period_month']=hoje.month; st.session_state['period_year']=hoje.year; _period_changed()

def _toggle_scale_edit(ano_atual=None, mes_atual=None):
    novo = not st.session_state['scale_edit_mode']
    if not novo and ano_atual is not None and mes_atual is not None:
        st.session_state.pop(f"scale_edit_snapshot_{ano_atual}_{mes_atual}", None)
    st.session_state['scale_edit_mode'] = novo

def _toggle_pattern_preview(): st.session_state['show_pattern_preview']=not st.session_state['show_pattern_preview']


def render_period_selector():
    mes=int(st.session_state['period_month']); ano=int(st.session_state['period_year'])
    p1,p2,p3=st.columns([1.15,4.7,1.15])
    p1.button('‹',use_container_width=True,on_click=_shift_period,args=(-1,),key=f'prev_{st.session_state["page"]}')
    p2.markdown(f"<div class='period-hero'><div class='eyebrow'>Escala</div><div class='title'>{MESES[mes-1]} {ano}</div></div>",unsafe_allow_html=True)
    p3.button('›',use_container_width=True,on_click=_shift_period,args=(1,),key=f'next_{st.session_state["page"]}')
    a,b,c,d=st.columns([2.3,1.1,1.5,2.3])
    b.button('Hoje',use_container_width=True,on_click=_ir_para_hoje,key=f'today_{st.session_state["page"]}')
    with c.popover('📅 Outro mês',use_container_width=True):
        years=list(range(hoje.year-3,hoje.year+5));
        if ano not in years: years=sorted(set(years+[ano]))
        st.selectbox('Mês',range(1,13),format_func=lambda x:MESES[x-1],key='period_month',on_change=_period_changed)
        st.selectbox('Ano',years,key='period_year',on_change=_period_changed)


def nav_button(label,key):
    st.sidebar.button(label,key=key,type='primary' if st.session_state['page']==label else 'secondary',use_container_width=True,on_click=_set_page,args=(label,))

with st.sidebar:
    st.markdown("<div class='sidebar-brand'><div class='badge'>HH</div><div><div class='nome'>Hospital HELP</div><div class='depto'>Radiologia</div></div></div>",unsafe_allow_html=True)
    st.divider()
st.sidebar.markdown("<div class='nav-eyebrow'>Dia a dia</div>",unsafe_allow_html=True)
nav_button('📅 Escala','nav_escala'); nav_button('🔄 Trocas','nav_trocas')
st.sidebar.markdown("<div class='nav-eyebrow'>Planejamento</div>",unsafe_allow_html=True); nav_button('🔁 Padrão Rotativo','nav_padrao')
st.sidebar.markdown("<div class='nav-eyebrow'>Gestão</div>",unsafe_allow_html=True); nav_button('👥 Equipe','nav_equipe'); nav_button('💰 Fechamento RH','nav_rh')
st.sidebar.markdown("<div class='nav-eyebrow'>Configurações</div>",unsafe_allow_html=True); nav_button('⚙️ Turnos e Valores','nav_turnos'); nav_button('💾 Backup','nav_backup')

page=st.session_state['page']; mes_num=int(st.session_state['period_month']); ano=int(st.session_state['period_year']); mes_nome=MESES[mes_num-1]

# =================================================================
# CALENDÁRIO OPERACIONAL UNIFORME
# =================================================================
def fixed_day_container(key):
    """Altura fixa real; mantém fallback para versões antigas do Streamlit."""
    try:
        return st.container(height=230, border=True, key=key)
    except TypeError:
        try:
            return st.container(height=230, border=True)
        except TypeError:
            return st.container(border=True)


def render_quick_claim_calendar(df_raw, ano, mes, doctor_name, doctor_id):
    calendar.setfirstweekday(calendar.MONDAY)
    weeks=calendar.monthcalendar(ano,mes)
    occupied={}
    if not df_raw.empty:
        for _,r in df_raw.iterrows():
            dt=pd.Timestamp(r['shift_date']).date(); occupied[(dt,r['shift_time'])]={'name':str(r['doctor_name']),'id':None if pd.isna(r.get('doctor_id')) else int(r['doctor_id'])}
    meus=[]
    if not df_raw.empty:
        for _,r in df_raw[df_raw['doctor_name']==doctor_name].sort_values(['shift_date','shift_time']).iterrows():
            meus.append((pd.Timestamp(r['shift_date']).date(),str(r['shift_time'])))
    emoji={'Manhã':'🌅','Tarde':'☀️','Noite':'🌙'}
    st.caption("Toque em **+ Assumir** nas vagas. Toque no **nome de outro médico** para assumir ou trocar.")
    for week_idx,week in enumerate(weeks):
        cols=st.columns(7,gap='small')
        for wd,day in enumerate(week):
            with cols[wd]:
                if day==0:
                    day_box = fixed_day_container(f"calempty_{ano}_{mes}_{week_idx}_{wd}")
                    with day_box:
                        st.markdown("<div class='quick-day-title'>&nbsp;</div>",unsafe_allow_html=True)
                        st.markdown("<div class='quick-day-spacer'></div>",unsafe_allow_html=True)
                        st.markdown("<div class='quick-day-spacer'></div>",unsafe_allow_html=True)
                        st.markdown("<div class='quick-day-spacer'></div>",unsafe_allow_html=True)
                    continue

                dt=datetime.date(ano,mes,day)
                day_has_self=any(occupied.get((dt,t),{}).get('name')==doctor_name for t in TURNOS)
                day_key = f"calself_{ano}_{mes}_{day}" if day_has_self else f"calday_{ano}_{mes}_{day}"
                with fixed_day_container(day_key):
                    hoje_txt=' · Hoje' if dt==hoje else ''
                    self_cls=' day-has-self' if day_has_self else ''
                    st.markdown(f"<div class='quick-day-title{self_cls}'>{DIAS_SEMANA_CURTO[wd]} {day:02d}{hoje_txt}</div>",unsafe_allow_html=True)
                    for turno in TURNOS:
                        info=occupied.get((dt,turno)); em=emoji[turno]
                        if info:
                            atual=info['name']; atual_id=info['id']
                            if atual==doctor_name:
                                st.markdown(f"<div class='quick-self-slot'><span class='self-emoji'>{em}</span><span class='self-check'>✓</span><span class='self-label'>Você</span></div>",unsafe_allow_html=True)
                            else:
                                with st.popover(f"{em} {atual}",use_container_width=True):
                                    st.caption(f"{turno} · {day:02d}/{mes:02d} · atualmente com **{atual}**")
                                    if st.button('✋ Assumir este plantão',key=f'take_{ano}_{mes}_{day}_{turno}_{doctor_id}',type='primary',use_container_width=True):
                                        ok,owner=replace_occupied_shift_atomic(dt,turno,atual_id,atual,doctor_id,doctor_name)
                                        st.session_state['claim_flash']=('success' if ok else 'warning', f"Você assumiu {turno.lower()} de {day:02d}/{mes:02d}." if ok else f"Não foi possível: o plantão agora está com {owner}.")
                                        st.rerun()
                                    opcoes=[x for x in meus if x!=(dt,turno)]
                                    if opcoes:
                                        pick=st.selectbox('Trocar com um plantão meu',opcoes,format_func=lambda x:f"{x[0].strftime('%d/%m')} · {x[1]}",key=f'swap_pick_{ano}_{mes}_{day}_{turno}_{doctor_id}')
                                        if st.button('🔄 Confirmar troca',key=f'swap_inline_{ano}_{mes}_{day}_{turno}_{doctor_id}',use_container_width=True):
                                            ok,msg=swap_with_my_shift_atomic(dt,turno,atual_id,pick[0],pick[1],doctor_id)
                                            st.session_state['claim_flash']=('success' if ok else 'warning',msg); st.rerun()
                        else:
                            if st.button(f"＋ {turno}",key=f'claim_{ano}_{mes}_{day}_{turno}_{doctor_id}',use_container_width=True):
                                ok,owner=claim_shift_atomic(dt,turno,doctor_id,doctor_name)
                                st.session_state['claim_flash']=('success' if ok else 'warning',f"{turno} de {day:02d}/{mes:02d} assumido." if ok else f"Esse turno acabou de ser assumido por {owner}.")
                                st.rerun()


def render_readonly_calendar(pivot,ano,mes):
    calendar.setfirstweekday(calendar.MONDAY)
    weeks=calendar.monthcalendar(ano,mes)
    emoji={'Manhã':'🌅','Tarde':'☀️','Noite':'🌙'}
    for week_idx,week in enumerate(weeks):
        cols=st.columns(7,gap='small')
        for wd,day in enumerate(week):
            with cols[wd]:
                if day==0:
                    with fixed_day_container(f"calempty_read_{ano}_{mes}_{week_idx}_{wd}"):
                        st.markdown("<div class='quick-day-title'>&nbsp;</div>",unsafe_allow_html=True)
                        st.markdown("<div class='quick-day-spacer'></div>",unsafe_allow_html=True)
                        st.markdown("<div class='quick-day-spacer'></div>",unsafe_allow_html=True)
                        st.markdown("<div class='quick-day-spacer'></div>",unsafe_allow_html=True)
                    continue
                dt=datetime.date(ano,mes,day)
                with fixed_day_container(f"calday_read_{ano}_{mes}_{day}"):
                    hoje_txt=' · Hoje' if dt==hoje else ''
                    st.markdown(f"<div class='quick-day-title'>{DIAS_SEMANA_CURTO[wd]} {day:02d}{hoje_txt}</div>",unsafe_allow_html=True)
                    for turno in TURNOS:
                        nome=str(pivot.at[turno,day]).strip() if day in pivot.columns and pd.notna(pivot.at[turno,day]) else ''
                        label=html.escape(nome) if nome else '—'
                        st.markdown(f"<div class='quick-self-slot' style='background:#0D1420;border-color:#334155;border-left-color:#334155'><span class='self-emoji'>{emoji[turno]}</span><span class='self-label' style='font-weight:600'>{label}</span></div>",unsafe_allow_html=True)

# =================================================================
# PÁGINAS
# =================================================================
if page=='📅 Escala':
    render_period_selector(); mes_num=int(st.session_state['period_month']); ano=int(st.session_state['period_year']); mes_nome=MESES[mes_num-1]
    df_raw=fetch_month_schedule(ano,mes_num); pivot=schedule_to_pivot(df_raw,ano,mes_num); total=calendar.monthrange(ano,mes_num)[1]*3; filled=len(df_raw)
    if st.session_state.get('medico_alvo_escala') not in ['']+active_names: st.session_state.pop('medico_alvo_escala',None)
    with st.container(border=True):
        st.markdown("<span class='identity-panel-anchor'></span>",unsafe_allow_html=True)
        st.markdown("<div class='identity-panel-head'><div class='identity-icon'>👤</div><div><div class='identity-eyebrow'>IDENTIFIQUE-SE PARA USAR A ESCALA</div><div class='identity-title'>Eu sou...</div><div class='identity-subtitle'>Escolha seu nome uma vez. Depois é só tocar no plantão que deseja assumir.</div></div></div>",unsafe_allow_html=True)
        medico=st.selectbox('Selecione seu nome',['']+active_names,key='medico_alvo_escala',label_visibility='collapsed',placeholder='Toque aqui e escolha seu nome')
    flash=st.session_state.pop('claim_flash',None)
    if flash: getattr(st,flash[0])(flash[1])
    st.markdown("<div class='turno-legend'><div class='item'><span class='dot dot-manha'></span>Manhã</div><div class='item'><span class='dot dot-tarde'></span>Tarde</div><div class='item'><span class='dot dot-noite'></span>Noite</div></div>",unsafe_allow_html=True)
    if medico:
        render_quick_claim_calendar(df_raw,ano,mes_num,medico,id_by_name[medico])
        pessoal=df_raw[df_raw['doctor_name']==medico].copy().sort_values('shift_date')
        with st.expander(f"📅 Meus plantões · {len(pessoal)} neste mês"):
            if not pessoal.empty:
                view=pessoal.copy(); view['Data']=pd.to_datetime(view['shift_date']).dt.strftime('%d/%m/%Y'); view=view.rename(columns={'shift_time':'Turno'}); st.dataframe(view[['Data','Turno']],hide_index=True,use_container_width=True)
                st.download_button('📅 Adicionar ao meu calendário (.ics)',data=generate_ics(pessoal,medico,get_shift_types()),file_name=f'Plantoes_{medico}_{mes_nome}_{ano}.ics',mime='text/calendar',use_container_width=True)
    else:
        st.info("Escolha seu nome em **Eu sou** para assumir um turno vazio com um toque."); render_readonly_calendar(pivot,ano,mes_num)
    cobertura=filled/total*100 if total else 0; st.markdown(f"<div class='month-summary'>{filled}/{total} turnos cobertos · {max(total-filled,0)} sem médico · {cobertura:.0f}% de cobertura</div>",unsafe_allow_html=True)
    with st.expander('⚙️ Administração e ferramentas'):
        a,b=st.columns(2); a.button('✨ Aplicar Padrão Rotativo',use_container_width=True,on_click=_toggle_pattern_preview); b.button('✏️ Editar escala completa',use_container_width=True,on_click=_toggle_scale_edit,args=(ano,mes_num))
    if st.session_state['show_pattern_preview']:
        anchor=get_rotation_anchor(); fix=fetch_fixed_pattern(); desired=build_pattern_assignments(ano,mes_num,fix,anchor)
        st.subheader('Prévia do padrão rotativo'); st.caption(f"Ciclo ancorado em {anchor.strftime('%d/%m/%Y')}.")
        current_map={(pd.Timestamp(r['shift_date']).date(),r['shift_time']):r['doctor_name'] for _,r in df_raw.iterrows()}
        desired_map={(r[0],r[1]):r[3] for r in desired}
        keys=set(current_map)|set(desired_map)
        mantidos=sum(1 for k in keys if current_map.get(k) and current_map.get(k)==desired_map.get(k))
        alterados=sum(1 for k in keys if current_map.get(k) and desired_map.get(k) and current_map.get(k)!=desired_map.get(k))
        novos=sum(1 for k in keys if not current_map.get(k) and desired_map.get(k))
        vazios=sum(1 for k in keys if current_map.get(k) and not desired_map.get(k))
        p1,p2,p3,p4=st.columns(4); p1.metric('Mantidos',mantidos); p2.metric('Alterados',alterados); p3.metric('Novos',novos); p4.metric('Ficarão vazios',vazios)
        if alterados or vazios: st.warning('Edições manuais divergentes do padrão serão substituídas.')
        if st.checkbox('Estou ciente. Substituir a escala deste mês pelo padrão.') and st.button('Aplicar padrão ao mês',type='primary'):
            ini,fim=month_bounds(ano,mes_num); execute_transacional([('DELETE FROM shift_schedule WHERE shift_date >= %s AND shift_date < %s',(ini,fim)),('INSERT INTO shift_schedule(shift_date,shift_time,doctor_id,doctor_name) VALUES %s',desired)]); st.session_state['show_pattern_preview']=False; st.rerun()
    if st.session_state['scale_edit_mode']:
        st.subheader('✏️ Edição administrativa da escala')
        st.caption(
            "Use este editor apenas para alterações em lote. Ao salvar, só as células que você de fato "
            "alterar aqui são gravadas — turnos assumidos por outros usuários enquanto este editor estava "
            "aberto não são apagados."
        )
        # Snapshot capturado no instante em que o editor é aberto: é contra ele
        # que o "Salvar" compara o que foi editado, e contra ele que cada
        # gravação é condicionada (concorrência otimista) — ver apply_admin_diff.
        snapshot_key = f"scale_edit_snapshot_{ano}_{mes_num}"
        if snapshot_key not in st.session_state:
            snap = {}
            for _, r in df_raw.iterrows():
                dt_snap = pd.Timestamp(r['shift_date']).date()
                did = None if pd.isna(r.get('doctor_id')) else int(r['doctor_id'])
                snap[(dt_snap, r['shift_time'])] = (did, str(r['doctor_name']))
            st.session_state[snapshot_key] = snap
        snapshot_map = st.session_state[snapshot_key]

        calendar.setfirstweekday(calendar.MONDAY); weeks=calendar.monthcalendar(ano,mes_num); existing=df_raw['doctor_name'].dropna().tolist() if not df_raw.empty else []; opts=['']+sorted(set(active_names+existing)); edits=[]
        for i,week in enumerate(weeks):
            data={f'w{i}_d{idx}':(['','',''] if day==0 else [pivot.at[t,day] for t in TURNOS]) for idx,day in enumerate(week)}; dfw=pd.DataFrame(data,index=TURNOS).reset_index().rename(columns={'index':'Turno'}); conf={'Turno':st.column_config.TextColumn('Turno',disabled=True)}
            for idx,day in enumerate(week): conf[f'w{i}_d{idx}']=st.column_config.TextColumn(DIAS_SEMANA_CURTO[idx],disabled=True) if day==0 else st.column_config.SelectboxColumn(f'{DIAS_SEMANA_CURTO[idx]} {day:02d}',options=opts)
            ed=st.data_editor(dfw,column_config=conf,hide_index=True,use_container_width=True,key=f'edit_{i}_{ano}_{mes_num}'); edits.append((week,ed))
        if st.button('💾 Salvar escala deste mês',type='primary'):
            edited_map = edited_grid_to_map(edits, ano, mes_num)
            changes = []
            nomes_invalidos = set()
            for key in set(snapshot_map) | set(edited_map):
                snap_id, snap_name = snapshot_map.get(key, (None, ""))
                new_name = edited_map.get(key, snap_name)
                if new_name == snap_name:
                    continue
                if new_name and new_name not in id_by_name:
                    nomes_invalidos.add(new_name)
                    continue
                changes.append((key[0], key[1], snap_id, snap_name, new_name))

            if nomes_invalidos:
                st.error(f"Médico(s) não encontrado(s): {', '.join(sorted(nomes_invalidos))}. Nada foi salvo.")
            elif not changes:
                st.info("Nenhuma alteração para salvar.")
            else:
                aplicadas, conflitos = apply_admin_diff(changes, id_by_name)
                if conflitos:
                    st.warning(f"{len(conflitos)} célula(s) não foram salvas porque mudaram entre a abertura do editor e agora:")
                    for dt_c, turno_c, esperado, tentativa, atual_c in conflitos:
                        st.caption(f"- {dt_c.strftime('%d/%m')} {turno_c}: você viu **{esperado}**, tentou **{tentativa}**, mas está com **{atual_c}**. Reabra o editor para revisar.")
                if aplicadas:
                    st.success(f"{len(aplicadas)} turno(s) atualizado(s).")
                st.session_state['scale_edit_mode']=False
                st.session_state.pop(snapshot_key, None)
                st.rerun()

elif page=='🔄 Trocas':
    render_period_selector(); mes_num=int(st.session_state['period_month']); ano=int(st.session_state['period_year']); st.header('🔄 Trocas de plantão')
    st.caption("Usa as mesmas travas do calendário rápido: cada troca/substituição só é aplicada se o plantão ainda estiver como você viu na tela.")
    df=fetch_month_schedule(ano,mes_num)
    if df.empty: st.info('Não há plantões neste mês.')
    else:
        df=df.reset_index(drop=True); labels={i:f"{pd.Timestamp(r['shift_date']).strftime('%d/%m')} · {r['shift_time']} · {r['doctor_name']}" for i,r in df.iterrows()}; t1,t2=st.tabs(['Trocar dois plantões','Substituir médico'])
        with t1:
            a=st.selectbox('Plantão A',list(labels),format_func=lambda x:labels[x]); opts=[x for x in labels if x!=a]; b=st.selectbox('Plantão B',opts,format_func=lambda x:labels[x]) if opts else None
            if b is not None and st.button('🔄 Confirmar troca',type='primary'):
                ra,rb=df.loc[a],df.loc[b]
                dt_a = pd.Timestamp(ra['shift_date']).date(); dt_b = pd.Timestamp(rb['shift_date']).date()
                id_a = None if pd.isna(ra.get('doctor_id')) else int(ra['doctor_id'])
                id_b = None if pd.isna(rb.get('doctor_id')) else int(rb['doctor_id'])
                if id_a is None or id_b is None:
                    st.error("Um dos plantões não tem médico com ID resolvido (registro legado). Corrija em Equipe antes de trocar.")
                else:
                    ok, msg = swap_with_my_shift_atomic(dt_b, rb['shift_time'], id_b, dt_a, ra['shift_time'], id_a)
                    if ok:
                        st.success(msg); st.rerun()
                    else:
                        st.warning(f"Troca não realizada: {msg}")
        with t2:
            idx=st.selectbox('Plantão',list(labels),format_func=lambda x:labels[x]); atual=df.loc[idx]; candidatos=[n for n in active_names if n!=atual['doctor_name']]; novo=st.selectbox('Novo médico',candidatos) if candidatos else None
            if novo and st.button('Substituir médico',type='primary'):
                dt_atual = pd.Timestamp(atual['shift_date']).date()
                atual_id = None if pd.isna(atual.get('doctor_id')) else int(atual['doctor_id'])
                ok, resultado = replace_occupied_shift_atomic(dt_atual, atual['shift_time'], atual_id, atual['doctor_name'], id_by_name[novo], novo)
                if ok:
                    st.success("Substituição realizada."); st.rerun()
                else:
                    st.warning(f"Não foi possível substituir: o plantão já está com {resultado}.")

elif page=='🔁 Padrão Rotativo':
    st.header('🔁 Padrão Rotativo'); anchor=get_rotation_anchor(); nova=st.date_input('Segunda-feira de início da Semana 1',value=anchor)
    anchor_preview=nova-datetime.timedelta(days=nova.weekday())
    st.caption(f"Semana 1 começa em {anchor_preview.strftime('%d/%m/%Y')}; o ciclo segue continuamente 1 → 2 → 3 → 4.")
    if st.button('Salvar data âncora'): set_rotation_anchor(nova); st.rerun()
    raw=fetch_fixed_pattern(); pattern_opts=['']+sorted(set(active_names+(raw['doctor_name'].dropna().tolist() if not raw.empty else []))); edits=[]
    for w in range(4):
        week_start=anchor_preview+datetime.timedelta(days=7*w); week_end=week_start+datetime.timedelta(days=6)
        st.markdown(f"#### Semana {w+1} · {week_start.strftime('%d/%m')}–{week_end.strftime('%d/%m')}")
        part=raw[raw['week_num']==w] if not raw.empty else pd.DataFrame(); piv=part.pivot(index='shift_time',columns='weekday',values='doctor_name').reindex(TURNOS).reindex(columns=range(7)).fillna('') if not part.empty else pd.DataFrame('',index=TURNOS,columns=range(7)); piv.columns=[str(c) for c in range(7)]; conf={str(c):st.column_config.SelectboxColumn(DIAS_SEMANA[c],options=pattern_opts) for c in range(7)}; ed=st.data_editor(piv,column_config=conf,use_container_width=True,key=f'pat_{w}'); edits.append((w,ed))
    if st.button('💾 Salvar padrão rotativo',type='primary'):
        rows=[]
        for w,ed in edits:
            for t in TURNOS:
                for wd in range(7):
                    n=str(ed.at[t,str(wd)]).strip() if pd.notna(ed.at[t,str(wd)]) else ''
                    if n: rows.append((w,wd,t,id_by_name[n],n))
        execute_transacional([('DELETE FROM fixed_schedule_4w',None),('INSERT INTO fixed_schedule_4w(week_num,weekday,shift_time,doctor_id,doctor_name) VALUES %s',rows)]); st.rerun()

elif page=='👥 Equipe':
    st.header('👥 Equipe médica')
    with st.form('add_doc',clear_on_submit=True):
        n=st.text_input('Nome do médico'); ok=st.form_submit_button('➕ Adicionar')
        if ok and n.strip(): execute_query('INSERT INTO doctors(name,ativo) VALUES(%s,TRUE) ON CONFLICT(name) DO UPDATE SET ativo=TRUE',(n.strip(),)); st.rerun()
    for _,r in df_docs.iterrows():
        a,b=st.columns([4,1]); a.write(f"{'🟢' if r['ativo'] else '⚪'} {r['name']}");
        if b.button('Inativar' if r['ativo'] else 'Reativar',key=f'doc_{r["id"]}'): execute_query('UPDATE doctors SET ativo=%s WHERE id=%s',(not bool(r['ativo']),int(r['id']))); st.rerun()

    if not df_docs.empty:
        with st.expander('✏️ Corrigir nome de médico'):
            rename_id=st.selectbox('Médico',df_docs['id'].astype(int).tolist(),format_func=lambda x:name_by_id.get(int(x),str(x)),key='rename_doctor_id')
            atual_nome=name_by_id.get(int(rename_id),''); novo_nome=st.text_input('Novo nome',value=atual_nome,key='rename_doctor_name')
            if st.button('Salvar novo nome',key='rename_doctor_save'):
                novo_nome=novo_nome.strip()
                if not novo_nome: st.error('O nome não pode ficar vazio.')
                elif novo_nome!=atual_nome and novo_nome in all_names: st.error('Já existe outro médico com esse nome.')
                else:
                    execute_transacional([
                        ('UPDATE doctors SET name=%s WHERE id=%s',(novo_nome,int(rename_id))),
                        ('UPDATE shift_schedule SET doctor_name=%s WHERE doctor_id=%s',(novo_nome,int(rename_id))),
                        ('UPDATE fixed_schedule_4w SET doctor_name=%s WHERE doctor_id=%s',(novo_nome,int(rename_id))),
                    ]); st.success('Nome atualizado sem perder o histórico.'); st.rerun()

elif page=='⚙️ Turnos e Valores':
    st.header('⚙️ Turnos e valores'); df=get_shift_types(); edit=df.copy(); edit['start_time']=edit['start_time'].astype(str).str[:5]; edit['end_time']=edit['end_time'].astype(str).str[:5]; ed=st.data_editor(edit,hide_index=True,use_container_width=True,disabled=['name'])
    if st.button('💾 Salvar turnos e valores',type='primary'):
        ops=[]
        for _,r in ed.iterrows(): ops.append(('UPDATE shift_types SET start_time=%s::time,end_time=%s::time,value=%s WHERE name=%s',(r['start_time'],r['end_time'],float(r['value']),r['name'])))
        execute_transacional(ops); st.rerun()

elif page=='💰 Fechamento RH':
    render_period_selector(); mes_num=int(st.session_state['period_month']); ano=int(st.session_state['period_year']); mes_nome=MESES[mes_num-1]; st.header(f'💰 Fechamento RH · {mes_nome} {ano}'); df=fetch_month_schedule(ano,mes_num); types=get_shift_types(); rows=[(pd.Timestamp(r['shift_date']).date(),r['shift_time'],r['doctor_name']) for _,r in df.iterrows()]; resumo=financial_summary_from_rows(rows,types); total=float(resumo['Total'].sum()) if not resumo.empty else 0; a,b,c=st.columns(3); a.metric('Custo da escala',f'R$ {total:,.2f}'); b.metric('Plantões',len(rows)); c.metric('Médicos',resumo['doctor_name'].nunique() if not resumo.empty else 0)
    if not resumo.empty:
        st.dataframe(resumo.rename(columns={'doctor_name':'Médico','Total_Plantões':'Plantões','Total':'Total (R$)'}),hide_index=True,use_container_width=True)
        schedule_sig=df[['shift_date','shift_time','doctor_name']].astype(str).to_csv(index=False) if not df.empty else ''
        types_sig=types[['name','start_time','end_time','value']].astype(str).to_csv(index=False) if not types.empty else ''
        pdf_key=hashlib.sha1(f'{ano}-{mes_num}|{schedule_sig}|{types_sig}'.encode('utf-8')).hexdigest()
        if st.session_state.get('rh_pdf_key')!=pdf_key:
            st.session_state.pop('rh_pdf',None); st.session_state['rh_pdf_key']=pdf_key
        if st.button('📄 Preparar PDF do Fechamento RH',type='primary'):
            calendar.setfirstweekday(calendar.MONDAY); st.session_state['rh_pdf']=generate_pdf_semanal(calendar.monthcalendar(ano,mes_num),schedule_to_pivot(df,ano,mes_num),resumo,mes_nome,ano,types)
        if st.session_state.get('rh_pdf'): st.download_button('⬇️ Baixar PDF oficial',data=st.session_state['rh_pdf'],file_name=f'Fechamento_RH_Escala_{mes_nome}_{ano}.pdf',mime='application/pdf')

elif page=='💾 Backup':
    st.header('💾 Backup e restauração')
    st.caption('O ZIP completo inclui equipe, escala, padrão rotativo, valores dos turnos e configurações.')
    if st.button('📦 Preparar backup completo',type='primary'): st.session_state['backup']=create_full_backup_zip()
    if st.session_state.get('backup'): st.download_button('⬇️ Baixar backup ZIP',data=st.session_state['backup'],file_name=f'backup_escala_{hoje:%Y%m%d}.zip',mime='application/zip')
    st.divider(); st.subheader('Restaurar')
    st.warning('A restauração substitui dados. Um backup automático do estado atual é preparado antes da operação.')
    up=st.file_uploader('Backup ZIP completo ou CSV antigo da escala',type=['zip','csv']); conf=st.checkbox('Confirmo que quero substituir os dados abrangidos pelo arquivo.')
    if up and st.button('🚨 Restaurar backup',disabled=not conf):
        try:
            st.session_state['pre_restore_backup']=create_full_backup_zip()
            if up.name.lower().endswith('.csv'): restore_legacy_schedule_csv(up)
            else: restore_backup_zip(up)
        except Exception as e: st.error(f'Restauração cancelada/revertida: {e}')
        else: st.success('Backup restaurado.'); st.session_state.pop('backup',None); st.rerun()
    if st.session_state.get('pre_restore_backup'):
        st.download_button('🛟 Baixar backup automático anterior à última restauração',data=st.session_state['pre_restore_backup'],file_name=f'backup_pre_restore_{hoje:%Y%m%d}.zip',mime='application/zip')
