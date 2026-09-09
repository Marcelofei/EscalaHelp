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
from fpdf import FPDF

# =================================================================
# 1. CONFIGURAÇÃO DA PÁGINA
# =================================================================
st.set_page_config(page_title="Hospital HELP — Escala de Radiologia", layout="wide", page_icon="🩻")

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


def execute_values_query(query: str, rows: list) -> None:
    if not rows:
        return
    def _exec(conn):
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, query, rows)
    _with_connection(_exec, transactional=False)


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


def get_shift_types():
    df = fetch_data("SELECT name, start_time, end_time, value FROM shift_types ORDER BY CASE name WHEN 'Manhã' THEN 1 WHEN 'Tarde' THEN 2 ELSE 3 END")
    if df.empty:
        return pd.DataFrame(columns=['name', 'start_time', 'end_time', 'value'])
    df['value'] = df['value'].astype(float)
    return df


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
    @media (max-width: 700px) { [data-testid="stHorizontalBlock"] { flex-direction:column!important; } [data-testid="stHorizontalBlock"] > div { width:100%!important; min-width:100%!important; } .block-container { padding-left:.8rem!important; padding-right:.8rem!important; } }
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
df_docs = fetch_data("SELECT id, name, ativo FROM doctors ORDER BY ativo DESC, name")
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

# =================================================================
# 8. SIDEBAR / NAVEGAÇÃO
# =================================================================
def nav_button(label, key):
    ativo = st.session_state['page'] == label
    if st.sidebar.button(label, key=key, type='primary' if ativo else 'secondary', use_container_width=True):
        st.session_state['page'] = label
        st.rerun()

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

st.sidebar.divider()
st.sidebar.markdown("<div class='nav-eyebrow'>Período ativo</div>", unsafe_allow_html=True)
years = list(range(hoje.year - 2, hoje.year + 4))
if st.session_state['period_year'] not in years:
    st.session_state['period_year'] = hoje.year
cpm, cpy = st.sidebar.columns([1.3, 1])
with cpm:
    st.selectbox("Mês", range(1, 13), format_func=lambda x: MESES[x-1], key='period_month')
with cpy:
    st.selectbox("Ano", years, key='period_year')

def _ir_para_hoje():
    st.session_state['period_month'] = hoje.month
    st.session_state['period_year'] = hoje.year

st.sidebar.button("Hoje", use_container_width=True, on_click=_ir_para_hoje)

mes_num = int(st.session_state['period_month'])
ano = int(st.session_state['period_year'])
mes_nome = MESES[mes_num - 1]
page = st.session_state['page']

st.markdown("<div class='help-header'><div class='badge'>HH</div><div class='titles'><h1>Gestão de Escala — Radiologia</h1><span>Hospital HELP</span></div></div>", unsafe_allow_html=True)

# =================================================================
# 9. PDF — USA O ESTADO ATUAL DA TELA, NÃO APENAS O ÚLTIMO SALVO
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
    st.header(f"📅 Escala · {mes_nome} {ano}")
    df_raw = fetch_month_schedule(ano, mes_num)
    df_pivot = schedule_to_pivot(df_raw, ano, mes_num)
    shift_types_df = get_shift_types()

    dias_mes = calendar.monthrange(ano, mes_num)[1]
    total_slots = dias_mes * len(TURNOS)
    filled = len(df_raw)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Turnos cobertos", f"{filled}/{total_slots}")
    c2.metric("Sem médico", max(total_slots - filled, 0))
    c3.metric("Médicos escalados", df_raw['doctor_id'].nunique() if not df_raw.empty else 0)
    c4.metric("Cobertura", f"{(filled / total_slots * 100):.0f}%" if total_slots else "0%")

    c_reset, c_spacer = st.columns([2.6, 5])
    with c_reset:
        with st.popover("✨ Aplicar Padrão Rotativo", use_container_width=True):
            anchor = get_rotation_anchor()
            df_fix = fetch_data("""
                SELECT f.week_num, f.weekday, f.shift_time, f.doctor_id, COALESCE(d.name, f.doctor_name) AS doctor_name
                FROM fixed_schedule_4w f LEFT JOIN doctors d ON d.id=f.doctor_id
                WHERE f.doctor_id IS NOT NULL
            """)
            desired = build_pattern_assignments(ano, mes_num, df_fix, anchor)
            current_map = {(pd.Timestamp(r['shift_date']).date(), r['shift_time']): r['doctor_name'] for _, r in df_raw.iterrows()}
            desired_map = {(r[0], r[1]): r[3] for r in desired}
            all_keys = set(current_map) | set(desired_map)
            iguais = sum(1 for k in all_keys if current_map.get(k) == desired_map.get(k) and current_map.get(k) is not None)
            alterados = sum(1 for k in all_keys if current_map.get(k) and desired_map.get(k) and current_map.get(k) != desired_map.get(k))
            novos = sum(1 for k in all_keys if not current_map.get(k) and desired_map.get(k))
            apagados = sum(1 for k in all_keys if current_map.get(k) and not desired_map.get(k))
            st.caption(f"Ciclo ancorado em {anchor.strftime('%d/%m/%Y')} (Semana 1).")
            p1, p2 = st.columns(2); p1.metric("Mantidos", iguais); p2.metric("Alterados", alterados)
            p3, p4 = st.columns(2); p3.metric("Novos", novos); p4.metric("Ficarão vazios", apagados)
            if alterados or apagados:
                st.warning("Edições manuais divergentes do padrão serão substituídas.")
            trava = st.checkbox("Estou ciente. Substituir a escala deste mês pelo padrão.")
            if st.button("Aplicar padrão ao mês", type="primary", use_container_width=True, disabled=not trava):
                rows = [(dt, turno, did, nome) for dt, turno, did, nome in desired]
                execute_transacional([
                    ("DELETE FROM shift_schedule WHERE shift_date >= %s AND shift_date < %s", month_bounds(ano, mes_num)),
                    ("INSERT INTO shift_schedule (shift_date, shift_time, doctor_id, doctor_name) VALUES %s", rows),
                ])
                st.rerun()

    st.divider()
    medico_alvo = st.selectbox("👤 Ver escala individual", [""] + all_names, key='medico_alvo_escala')
    if medico_alvo:
        df_pessoal = df_raw[df_raw['doctor_name'] == medico_alvo].copy()
        if df_pessoal.empty:
            st.info(f"Nenhum plantão encontrado para {medico_alvo} neste mês.")
        else:
            df_pessoal = df_pessoal.sort_values('shift_date')
            df_view = df_pessoal.copy()
            df_view['Data'] = df_view['shift_date'].apply(lambda d: f"{DIAS_SEMANA_CURTO[pd.Timestamp(d).weekday()]} {pd.Timestamp(d).strftime('%d/%m')}")
            df_view['Turno'] = df_view['shift_time'].map({'Manhã':'🌅 Manhã','Tarde':'☀️ Tarde','Noite':'🌙 Noite'})
            st.dataframe(df_view[['Data', 'Turno']], use_container_width=True, hide_index=True)
            ics_bytes = generate_ics(df_pessoal, medico_alvo, shift_types_df)
            st.download_button("📅 Adicionar meus plantões ao calendário (.ics)", data=ics_bytes,
                               file_name=f"Plantões_{medico_alvo}_{mes_nome}_{ano}.ics", mime="text/calendar", use_container_width=True)
        st.divider()

    modo = st.radio("Modo", ["👁️ Visualizar", "✏️ Editar"], horizontal=True, label_visibility='collapsed')
    calendar.setfirstweekday(calendar.MONDAY)
    weeks = calendar.monthcalendar(ano, mes_num)

    st.markdown("<div class='turno-legend'><div class='item'><span class='dot dot-manha'></span>Manhã</div><div class='item'><span class='dot dot-tarde'></span>Tarde</div><div class='item'><span class='dot dot-noite'></span>Noite</div></div>", unsafe_allow_html=True)

    current_rows_for_export = []
    current_pivot_for_export = df_pivot.copy()

    if modo == "👁️ Visualizar":
        for i, week in enumerate(weeks):
            st.markdown(f"#### Semana {i+1}")
            data = {'Turno': ['🌅 Manhã', '☀️ Tarde', '🌙 Noite']}
            for idx, day in enumerate(week):
                col = DIAS_SEMANA_CURTO[idx] if day == 0 else f"{DIAS_SEMANA_CURTO[idx]} {day:02d}"
                data[col] = ['—','—','—'] if day == 0 else [df_pivot.at[t, day] or '—' for t in TURNOS]
            st.dataframe(pd.DataFrame(data), use_container_width=True, hide_index=True)
        for _, r in df_raw.iterrows():
            current_rows_for_export.append((pd.Timestamp(r['shift_date']).date(), r['shift_time'], r['doctor_name']))
    else:
        # Opções incluem ativos + qualquer médico já presente no mês (mesmo inativo), preservando histórico editável.
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
                config[key] = (st.column_config.TextColumn(DIAS_SEMANA_CURTO[idx], disabled=True, width='small') if day == 0
                               else st.column_config.SelectboxColumn(f"{DIAS_SEMANA_CURTO[idx]} {day:02d}", options=editor_options, width='small'))
            ed = st.data_editor(df_w, column_config=config, hide_index=True, use_container_width=True, key=f"edit_month_w{i}")
            all_edits.append((week, ed))

        current_rows_for_export = current_state_from_edits(all_edits, ano, mes_num)
        current_pivot_for_export = pd.DataFrame("", index=TURNOS, columns=range(1, dias_mes + 1))
        for dt, turno, nome in current_rows_for_export:
            current_pivot_for_export.at[turno, dt.day] = nome

        if st.button("💾 Salvar escala deste mês", type="primary", use_container_width=True):
            rows = []
            for dt, turno, nome in current_rows_for_export:
                did = id_by_name.get(nome)
                if did is None:
                    st.error(f"Médico não encontrado: {nome}")
                    st.stop()
                rows.append((dt, turno, did, nome))
            ini, fim = month_bounds(ano, mes_num)
            # O estado COMPLETO da grade substitui o mês. Células apagadas viram DELETE de verdade.
            execute_transacional([
                ("DELETE FROM shift_schedule WHERE shift_date >= %s AND shift_date < %s", (ini, fim)),
                ("INSERT INTO shift_schedule (shift_date, shift_time, doctor_id, doctor_name) VALUES %s", rows),
            ])
            st.success("Escala salva!")
            st.rerun()

    st.divider()
    resumo_atual = financial_summary_from_rows(current_rows_for_export, shift_types_df)
    cpdf1, cpdf2 = st.columns(2)
    with cpdf1:
        if current_rows_for_export:
            pdf_bytes = generate_pdf_semanal(weeks, current_pivot_for_export, resumo_atual, mes_nome, ano, shift_types_df)
            st.download_button("📄 Relatório completo da escala atual (PDF)", data=pdf_bytes,
                               file_name=f"Escala_{mes_nome}_{ano}.pdf", mime="application/pdf", use_container_width=True)
        else:
            st.caption("Sem escala para gerar PDF.")
    with cpdf2:
        if not df_raw.empty:
            export_csv = df_raw[['shift_date','shift_time','doctor_name']].to_csv(index=False).encode('utf-8')
            st.download_button("📥 Exportar escala do mês (CSV)", data=export_csv,
                               file_name=f"Escala_{mes_nome}_{ano}.csv", mime='text/csv', use_container_width=True)

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
    df_fix_raw = fetch_data("""
        SELECT f.week_num, f.weekday, f.shift_time, f.doctor_id, COALESCE(d.name, f.doctor_name) AS doctor_name
        FROM fixed_schedule_4w f LEFT JOIN doctors d ON d.id=f.doctor_id
    """)
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
        st.dataframe(display, hide_index=True, use_container_width=True,
                     column_config={'Total (R$)': st.column_config.NumberColumn(format='R$ %.2f')})
        st.download_button("📥 Exportar fechamento (CSV)", data=display.to_csv(index=False).encode('utf-8'),
                           file_name=f"Fechamento_RH_{mes_nome}_{ano}.csv", mime='text/csv')

# =================================================================
# 15. PÁGINA — TROCAS DE PLANTÃO (SEM WORKFLOW DE APROVAÇÃO/AUDITORIA)
# =================================================================
elif page == '🔄 Trocas':
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
