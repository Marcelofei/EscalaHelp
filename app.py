import streamlit as st
import pandas as pd
import psycopg2
import psycopg2.extras
import datetime
import calendar
import hashlib
import os
from fpdf import FPDF

# =================================================================
# 1. CONFIGURAÇÃO DA PÁGINA
# =================================================================
st.set_page_config(page_title="Hospital HELP — Escala de Radiologia", layout="wide", page_icon="🩻")

# =================================================================
# 2. INFRAESTRUTURA DE BANCO (COM TRANSAÇÃO ATÔMICA)
# =================================================================

@st.cache_resource(ttl=3600)
def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        try:
            db_url = st.secrets["DATABASE_URL"]
        except Exception:
            pass

    if not db_url:
        st.error("DATABASE_URL ausente nas configurações (Secrets/Environment).")
        st.stop()

    conn = psycopg2.connect(db_url, options="-c client_encoding=utf8", connect_timeout=10)
    conn.autocommit = True
    return conn

def execute_query(query: str, params=None) -> None:
    """Executa UMA operação isolada (mantém autocommit)."""
    def _exec():
        conn = get_db_connection()
        with conn.cursor() as cur:
            if params and isinstance(params, list) and "VALUES" in query:
                psycopg2.extras.execute_values(cur, query, params)
            else:
                cur.execute(query, params)
    try:
        _exec()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        get_db_connection.clear()
        _exec()

def execute_transacional(operacoes: list) -> None:
    """
    Executa uma LISTA de (query, params) na MESMA transação: tudo comita junto
    ou tudo desfaz junto. Usado nos pontos onde um DELETE é seguido de um INSERT
    (ex: "Resetar para Padrão Fixo") -- sem isso, se o INSERT falhar ou não tiver
    o que inserir, o DELETE já efetivado sozinho apagaria dados sem repor nada.
    """
    def _exec():
        conn = get_db_connection()
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                for query, params in operacoes:
                    if params and isinstance(params, list) and "VALUES" in query:
                        psycopg2.extras.execute_values(cur, query, params)
                    else:
                        cur.execute(query, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    try:
        _exec()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        get_db_connection.clear()
        _exec()

def fetch_data(query: str, params=None) -> pd.DataFrame:
    def _fetch():
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(query, params)
            if cur.description:
                columns = [desc[0] for desc in cur.description]
                return pd.DataFrame(cur.fetchall(), columns=columns)
            return pd.DataFrame()
    try:
        return _fetch()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        get_db_connection.clear()
        return _fetch()

def init_db():
    query = """
    CREATE TABLE IF NOT EXISTS doctors (name TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS shift_schedule (shift_date DATE, shift_time VARCHAR(10), doctor_name TEXT, PRIMARY KEY(shift_date, shift_time));
    CREATE TABLE IF NOT EXISTS fixed_schedule_4w (week_num INT, weekday INT, shift_time VARCHAR(10), doctor_name TEXT, PRIMARY KEY(week_num, weekday, shift_time));
    """
    execute_query(query)
    # Médico "inativo" em vez de excluído -- preserva o histórico de plantões antigos
    # mesmo depois que alguém sai da equipe. DEFAULT 1 já marca os médicos existentes
    # como ativos automaticamente.
    execute_query("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS ativo INTEGER DEFAULT 1;")

try:
    init_db()
except Exception as e:
    st.error("🚨 Falha Crítica: Banco de Dados Inacessível.")
    st.error("O Supabase pode estar pausado ou a variável DATABASE_URL está incorreta.")
    st.code(str(e))
    st.stop()

# =================================================================
# 3. IDENTIDADE VISUAL
# =================================================================
# Conceito: "painel de controle" de escala -- precisão e legibilidade em
# primeiro lugar, mantendo o azul institucional do Hospital HELP como
# acento de marca, só que calibrado pra funcionar de verdade em tema escuro
# (em vez de forçar fundo branco por cima do escuro nativo, como estava antes).
#
# Paleta:
#   Base #0B1220        -> fundo da página
#   Surface #131B2E      -> cartões, tabelas, formulários
#   Surface-sidebar #0E1526
#   Border #243049
#   Texto #E7ECF5 / muted #8B97AE
#   Marca (HELP blue, calibrado pro escuro) #2F8FE0
#   Crítico (ações destrutivas) #E0695C
#   Turnos: Manhã #E0A847 (ambar) / Tarde #2F8FE0 (azul) / Noite #7C6FE0 (índigo)
# Tipografia: Inter (texto/títulos) + JetBrains Mono (datas e valores financeiros).

def aplicar_estilo_visual():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

    :root, .stApp {
        --background-color: #0B1220 !important;
        --secondary-background-color: #131B2E !important;
        --text-color: #E7ECF5 !important;
        --primary-color: #2F8FE0 !important;
    }
    html, body, .stApp,
    [data-testid="stAppViewContainer"], [data-testid="stMain"], [data-testid="stHeader"], .main {
        background-color: #0B1220 !important;
    }
    [data-testid="stHeader"] { background-color: rgba(0,0,0,0) !important; }
    .stApp { color: #E7ECF5; }

    html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
    h1, h2, h3, h4, h5, h6 {
        font-family: 'Inter', sans-serif !important;
        font-weight: 700 !important;
        color: #E7ECF5 !important;
        letter-spacing: -0.01em;
    }

    .stApp label, .stApp .stMarkdown, .stApp .stMarkdown p,
    .stApp [data-testid="stWidgetLabel"] p, .stApp [data-testid="stWidgetLabel"] {
        color: #E7ECF5 !important;
    }
    .stApp [data-testid="stCaptionContainer"] { color: #8B97AE !important; }

    div[data-testid="stMetric"], div[data-testid="metric-container"] {
        background: #131B2E !important;
        border: 1px solid #243049;
        border-radius: 12px;
        padding: 0.9rem 1.1rem 0.8rem 1.1rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.35);
    }
    div[data-testid="stMetricValue"] {
        font-family: 'JetBrains Mono', monospace !important;
        font-weight: 600 !important;
        color: #E7ECF5 !important;
    }
    div[data-testid="stMetricLabel"] {
        font-weight: 600 !important; color: #8B97AE !important;
        font-size: 0.78rem !important; text-transform: uppercase; letter-spacing: 0.04em;
    }

    section[data-testid="stSidebar"] {
        background: #0E1526 !important;
        border-right: 1px solid #243049;
    }
    section[data-testid="stSidebar"] * { color: #E7ECF5 !important; }

    .stButton button, .stButton button[kind="secondary"], .stButton button:not([kind="primary"]) {
        background-color: #131B2E !important;
        border: 1px solid #243049 !important;
        color: #E7ECF5 !important;
        font-weight: 500;
    }
    .stButton button *, .stButton button[kind="secondary"] *, .stButton button:not([kind="primary"]) * {
        color: #E7ECF5 !important;
    }
    .stButton button:hover, .stButton button:not([kind="primary"]):hover {
        background-color: #1A2438 !important; border-color: #2F8FE0 !important;
    }
    .stButton button[kind="primary"] {
        background-color: #2F8FE0 !important; border: 1px solid #2F8FE0 !important; color: #061018 !important;
    }
    .stButton button[kind="primary"] * { color: #061018 !important; }
    .stButton button[kind="primary"]:hover { background-color: #2679BD !important; border-color: #2679BD !important; }

    div.stDownloadButton > button {
        background-color: #131B2E !important; border: 1px solid #2F8FE0 !important; color: #2F8FE0 !important;
    }
    div.stDownloadButton > button * { color: #2F8FE0 !important; }

    .nav-eyebrow {
        font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
        color: #8B97AE !important; margin: 1rem 0 0.4rem 0.1rem;
    }

    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] { font-weight: 600; color: #8B97AE !important; }
    .stTabs [data-baseweb="tab"] p { color: #8B97AE !important; }
    .stTabs [aria-selected="true"] { color: #2F8FE0 !important; }
    .stTabs [aria-selected="true"] p { color: #2F8FE0 !important; }

    div[data-testid="stDataFrame"], div[data-testid="stDataEditor"] {
        border-radius: 10px; overflow: hidden; border: 1px solid #243049;
    }
    div[data-testid="stExpander"], div[data-testid="stPopover"] {
        border: 1px solid #243049 !important; border-radius: 12px !important; background: #131B2E !important;
    }

    .help-header {
        display: flex; align-items: center; gap: 14px; padding: 4px 0 18px 0;
        border-bottom: 1px solid #243049; margin-bottom: 18px;
    }
    .help-header .badge {
        background: #2F8FE0; color: #061018; font-weight: 800; font-size: 1.3rem;
        width: 44px; height: 44px; border-radius: 10px; display: flex; align-items: center; justify-content: center;
    }
    .help-header .titles h1 { margin: 0; font-size: 1.3rem; line-height: 1.2; }
    .help-header .titles span { color: #8B97AE; font-size: 0.85rem; }

    .sidebar-brand { text-align: center; padding: 6px 0 14px 0; }
    .sidebar-brand .badge {
        background: #2F8FE0; color: #061018; font-weight: 800; font-size: 1.6rem;
        width: 56px; height: 56px; border-radius: 12px; display: flex; align-items: center; justify-content: center;
        margin: 0 auto 8px auto;
    }
    .sidebar-brand .nome { font-weight: 700; font-size: 1.05rem; color: #E7ECF5; }
    .sidebar-brand .depto { color: #8B97AE; font-size: 0.8rem; }
    </style>
    """, unsafe_allow_html=True)

aplicar_estilo_visual()

# =================================================================
# 4. LOGIN
# =================================================================
if 'auth' not in st.session_state: st.session_state['auth'] = False
if not st.session_state['auth']:
    c_login = st.columns([1, 1.2, 1])[1]
    with c_login:
        st.markdown(
            "<div style='text-align:center; margin-top:8vh;'>"
            "<div style='background:#2F8FE0; color:#061018; font-weight:800; font-size:1.8rem; width:64px; height:64px; "
            "border-radius:14px; display:flex; align-items:center; justify-content:center; margin:0 auto 16px auto;'>HH</div>"
            "<h2 style='margin-bottom:2px;'>Hospital HELP</h2>"
            "<p style='color:#8B97AE; margin-top:0;'>Gestão de Escala — Radiologia</p>"
            "</div>", unsafe_allow_html=True
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
# 5. SIDEBAR
# =================================================================
with st.sidebar:
    st.markdown(
        "<div class='sidebar-brand'><div class='badge'>HH</div>"
        "<div class='nome'>Hospital HELP</div>"
        "<div class='depto'>Radiologia</div></div>",
        unsafe_allow_html=True
    )
    st.divider()

    df_docs = fetch_data("SELECT name, ativo FROM doctors ORDER BY ativo DESC, name;")
    lista_medicos_ativos = [""] + df_docs[df_docs['ativo'] == 1]['name'].tolist()  # pra atribuir turnos
    lista_medicos_todos = [""] + df_docs['name'].tolist()  # pra consultar histórico (inclui inativos)

    st.markdown("<div class='nav-eyebrow'>Destacar na Escala</div>", unsafe_allow_html=True)
    medico_alvo = st.selectbox("Médico", lista_medicos_todos, label_visibility="collapsed")

    st.markdown("<div class='nav-eyebrow'>Equipe</div>", unsafe_allow_html=True)
    novo = st.text_input("Adicionar médico", label_visibility="collapsed", placeholder="Nome do médico")
    if st.button("➕ Adicionar à Equipe", use_container_width=True):
        if novo.strip():
            execute_query("INSERT INTO doctors (name, ativo) VALUES (%s, 1) ON CONFLICT DO NOTHING;", (novo.strip(),))
            st.rerun()

    if not df_docs.empty:
        with st.expander(f"Gerenciar equipe ({len(df_docs)} médicos)"):
            st.caption("Médicos inativos saem das opções de atribuição de turno, mas o histórico de plantões antigos deles continua intacto.")
            for _, row in df_docs.iterrows():
                nome, ativo = row['name'], int(row['ativo'])
                c1, c2 = st.columns([3.2, 1.8])
                c1.write(f"{'🟢' if ativo else '⚪'} {nome}")
                if c2.button("Inativar" if ativo else "Reativar", key=f"toggle_ativo_{nome}", use_container_width=True):
                    execute_query("UPDATE doctors SET ativo = %s WHERE name = %s;", (0 if ativo else 1, nome))
                    st.rerun()

    st.divider()
    st.markdown("<div class='nav-eyebrow'>Contingência</div>", unsafe_allow_html=True)
    df_backup = fetch_data("SELECT * FROM shift_schedule;")
    if not df_backup.empty:
        st.download_button("📥 Exportar Backup (CSV)", data=df_backup.to_csv(index=False).encode('utf-8'),
                           file_name="backup_escala.csv", mime="text/csv", use_container_width=True)
    else:
        st.caption("Sem dados de escala pra exportar ainda.")

# =================================================================
# 6. CABEÇALHO E ABAS
# =================================================================
st.markdown(
    "<div class='help-header'><div class='badge'>HH</div>"
    "<div class='titles'><h1>Gestão de Escala — Radiologia</h1>"
    "<span>Hospital HELP</span></div></div>",
    unsafe_allow_html=True
)

tab_escala, tab_padrao = st.tabs(["📅 Escala Mensal", "⚙️ Padrão Rotativo"])

meses = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
ano_atual = datetime.date.today().year
anos_disponiveis = list(range(ano_atual - 1, ano_atual + 3))

# ---------------------------------------------------------
# ABA: PADRÃO ROTATIVO (4 semanas)
# ---------------------------------------------------------
with tab_padrao:
    c_info, c_vazio, c_btn_padrao = st.columns([4, 1, 2])
    with c_info:
        st.subheader("Configurar Escala Espelho")
        st.caption("Preencha o ciclo de 4 semanas. Esse padrão é o que entra quando você usa 'Resetar para Padrão Fixo' num mês específico.")
    with c_btn_padrao:
        st.write("")
        if st.button("💾 Salvar Padrão Fixo", type="primary", use_container_width=True, key="btn_salvar_padrao"):
            batch_fix = []
            for w_num, ed_fix in st.session_state.get('edits_padrao', []):
                for shift in ['Manhã', 'Tarde', 'Noite']:
                    for wd in range(7):
                        doc = ed_fix.at[shift, str(wd)]
                        if doc: batch_fix.append((w_num, wd, shift, doc))

            operacoes = [("DELETE FROM fixed_schedule_4w;", None)]
            if batch_fix:
                operacoes.append(("INSERT INTO fixed_schedule_4w (week_num, weekday, shift_time, doctor_name) VALUES %s;", batch_fix))
            execute_transacional(operacoes)
            st.success("Padrão fixo atualizado!")

    df_fix_raw = fetch_data("SELECT week_num, weekday, shift_time, doctor_name FROM fixed_schedule_4w")
    week_headers_fix = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    cols_str = [str(i) for i in range(7)]
    all_fix_edits = []

    for w_num in range(4):
        st.markdown(f"#### Semana {w_num + 1}")
        df_w_raw = df_fix_raw[df_fix_raw['week_num'] == w_num] if not df_fix_raw.empty else pd.DataFrame()
        if not df_w_raw.empty:
            df_fix_pivot = df_w_raw.pivot(index='shift_time', columns='weekday', values='doctor_name').reindex(['Manhã', 'Tarde', 'Noite'])
            df_fix_pivot.columns = [str(int(c)) for c in df_fix_pivot.columns]
            df_fix_pivot = df_fix_pivot.reindex(columns=cols_str).fillna("")
        else:
            df_fix_pivot = pd.DataFrame("", index=['Manhã', 'Tarde', 'Noite'], columns=cols_str)

        w_conf_fix = {str(c): st.column_config.SelectboxColumn(week_headers_fix[c], options=lista_medicos_ativos, width="small") for c in range(7)}
        ed_fix = st.data_editor(df_fix_pivot, column_config=w_conf_fix, use_container_width=True, key=f"ed_fixa_w{w_num}")
        all_fix_edits.append((w_num, ed_fix))

    st.session_state['edits_padrao'] = all_fix_edits

# ---------------------------------------------------------
# ABA: ESCALA MENSAL
# ---------------------------------------------------------
with tab_escala:
    col_m, col_a, col_space, col_reset = st.columns([2, 1.5, 3, 2.5])

    with col_m: mes_nome = st.selectbox("Mês de Referência", meses, index=datetime.date.today().month - 1)
    with col_a: ano = st.selectbox("Ano", anos_disponiveis, index=anos_disponiveis.index(ano_atual))
    mes_num = meses.index(mes_nome) + 1

    with col_reset:
        st.write("")
        with st.popover("✨ Resetar para Padrão Fixo", use_container_width=True):
            st.error("⚠️ Esta ação apaga permanentemente todas as edições manuais deste mês e substitui pelo padrão fixo.")
            trava_seguranca = st.checkbox("Estou ciente. Substituir escala.")

            if st.button("Confirmar Execução", type="primary", use_container_width=True, disabled=not trava_seguranca):
                df_fix = fetch_data("SELECT week_num, weekday, shift_time, doctor_name FROM fixed_schedule_4w WHERE doctor_name != ''")
                fix_map = {(r['week_num'], int(r['weekday']), r['shift_time']): r['doctor_name'] for _, r in df_fix.iterrows()} if not df_fix.empty else {}

                batch_a = []
                cal_w = calendar.monthcalendar(ano, mes_num)
                for i, w in enumerate(cal_w):
                    p_w = i % 4
                    for wd, day in enumerate(w):
                        if day > 0:
                            dt = datetime.date(ano, mes_num, day)
                            for s in ['Manhã', 'Tarde', 'Noite']:
                                doc = fix_map.get((p_w, wd, s), "")
                                if doc: batch_a.append((dt, s, doc))

                operacoes = [("DELETE FROM shift_schedule WHERE EXTRACT(YEAR FROM shift_date) = %s AND EXTRACT(MONTH FROM shift_date) = %s", (ano, mes_num))]
                if batch_a:
                    operacoes.append(("INSERT INTO shift_schedule (shift_date, shift_time, doctor_name) VALUES %s;", batch_a))
                execute_transacional(operacoes)

                if not batch_a:
                    st.warning("Padrão fixo não tinha nenhuma atribuição pra esse mês — a escala foi limpa, mas nada foi reposto.")
                st.rerun()

    df_raw = fetch_data("SELECT shift_date, shift_time, doctor_name FROM shift_schedule WHERE EXTRACT(YEAR FROM shift_date) = %s AND EXTRACT(MONTH FROM shift_date) = %s", (ano, mes_num))

    if not df_raw.empty:
        df_raw['dia'] = pd.to_datetime(df_raw['shift_date']).dt.day
        df_pivot = df_raw.pivot(index='shift_time', columns='dia', values='doctor_name').reindex(['Manhã', 'Tarde', 'Noite']).fillna("")
    else:
        df_pivot = pd.DataFrame(index=['Manhã', 'Tarde', 'Noite'])

    if medico_alvo != "":
        st.subheader(f"🔎 Visão Individual — {medico_alvo}")
        modo_visao = st.radio("Modo de visualização", ["🗂️ Grade do Mês", "📱 Lista (mobile)"], horizontal=True, key="modo_visao_individual", label_visibility="collapsed")

        if modo_visao == "🗂️ Grade do Mês":
            def style_highlight(val):
                return 'background-color: #2F8FE0; color: #061018; font-weight: bold; border: 1px solid #243049;' if val == medico_alvo else 'background-color: #131B2E; color: #E7ECF5; border: 1px solid #243049;'
            st.dataframe(df_pivot.style.map(style_highlight), use_container_width=True, hide_index=False)
        else:
            # Lista simples (data -> turno), pensada pra ler bem numa tela de celular --
            # a grade de 7 colunas funciona em desktop, mas fica ilegível no telefone.
            df_pessoal = df_raw[df_raw['doctor_name'] == medico_alvo].copy()
            if df_pessoal.empty:
                st.info(f"Nenhum plantão encontrado para {medico_alvo} em {mes_nome}/{ano}.")
            else:
                dias_semana_pt = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
                rotulo_turno_lista = {"Manhã": "🌅 Manhã", "Tarde": "☀️ Tarde", "Noite": "🌙 Noite"}
                df_pessoal = df_pessoal.sort_values('shift_date').copy()
                df_pessoal['Data'] = df_pessoal['shift_date'].apply(
                    lambda d: f"{dias_semana_pt[pd.Timestamp(d).weekday()]} {pd.Timestamp(d).strftime('%d/%m')}"
                )
                df_pessoal['Turno'] = df_pessoal['shift_time'].map(rotulo_turno_lista)
                st.dataframe(df_pessoal[['Data', 'Turno']], use_container_width=True, hide_index=True)
        st.divider()

    st.subheader("📝 Edição da Escala Mensal")
    st.caption("🌅 Manhã · ☀️ Tarde · 🌙 Noite")
    calendar.setfirstweekday(calendar.MONDAY)
    weeks = calendar.monthcalendar(ano, mes_num)
    week_headers = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    rotulo_turno = {"Manhã": "🌅 Manhã", "Tarde": "☀️ Tarde", "Noite": "🌙 Noite"}
    all_edits = []

    for i, week in enumerate(weeks):
        st.markdown(f"#### Semana {i+1}")
        w_data = {f"w{i}_d{idx}": (["", "", ""] if day == 0 else (df_pivot[day].tolist() if day in df_pivot.columns else ["", "", ""])) for idx, day in enumerate(week)}
        for k in w_data: w_data[k] = ["" if x is None or x == "None" else x for x in w_data[k]]

        df_w_raw = pd.DataFrame(w_data)
        df_w_raw.index = ['Manhã', 'Tarde', 'Noite']

        w_conf = {f"w{i}_d{idx}": (st.column_config.TextColumn(week_headers[idx], disabled=True, width="small") if day == 0 else st.column_config.SelectboxColumn(f"{week_headers[idx]} {day:02d}", options=lista_medicos_ativos, width="small")) for idx, day in enumerate(week)}

        df_to_edit = df_w_raw.reset_index().rename(columns={'index': 'Turno'})
        df_to_edit['Turno'] = df_to_edit['Turno'].map(rotulo_turno)
        headers_final = {'Turno': st.column_config.TextColumn("Turno", disabled=True, width="small")}
        headers_final.update(w_conf)

        ed = st.data_editor(df_to_edit, column_config=headers_final, use_container_width=True, key=f"ed_m_{i}", hide_index=True)
        ed['Turno'] = ['Manhã', 'Tarde', 'Noite']  # desfaz o rótulo decorativo antes de salvar
        all_edits.append((week, ed))

    st.divider()
    df_fin = df_raw[df_raw['doctor_name'].isin(df_docs['name'])].copy()
    if not df_fin.empty:
        df_fin['valor'] = df_fin['shift_time'].map({'Manhã': 750, 'Tarde': 750, 'Noite': 1500})
        resumo_rh = df_fin.groupby('doctor_name').agg(Total=('valor', 'sum')).reset_index()
    else:
        resumo_rh = pd.DataFrame()

    def generate_pdf_semanal(weeks, pivot, resumo, mes, ano):
        pdf = FPDF(orientation='L', unit='mm', format='A4')
        pdf.add_page()
        total_semanas = len(weeks)
        if total_semanas <= 4: font_tit = 18; font_tab = 9; h_row = 7; margin_w = 5
        elif total_semanas == 5: font_tit = 16; font_tab = 8; h_row = 6; margin_w = 3
        else: font_tit = 14; font_tab = 7; h_row = 4.5; margin_w = 2

        # PDF mantém fundo branco/texto escuro de propósito -- é documento pra impressão,
        # não a tela do app, e isso poupa tinta/toner e segue a convenção de documentos impressos.
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

            for shift in ['Manhã', 'Tarde', 'Noite']:
                pdf.set_font("Arial", 'B', font_tab); pdf.set_fill_color(240, 240, 240); pdf.set_text_color(0, 45, 98)
                shift_label = shift.replace('ã', 'a'); pdf.cell(col_w_label, h_row, shift_label, 1, 0, 'C', True)
                pdf.set_font("Arial", '', font_tab); pdf.set_text_color(0, 0, 0)
                char_limit = 18 if font_tab >= 9 else (22 if font_tab == 8 else 25)

                for day in week:
                    if day == 0: pdf.cell(col_w_day, h_row, "-", 1, 0, 'C')
                    else:
                        nome = str(pivot.at[shift, day]) if day in pivot.columns else ""
                        pdf.cell(col_w_day, h_row, nome[:char_limit], 1, 0, 'C')
                pdf.ln()
            pdf.ln(margin_w)

        pdf.add_page(); pdf.set_font("Arial", 'B', 14); pdf.set_text_color(0, 45, 98)
        pdf.cell(0, 10, "FECHAMENTO FINANCEIRO - RH", ln=True, align='L'); pdf.ln(5)
        pdf.set_font("Arial", 'B', 10); pdf.set_fill_color(0, 45, 98); pdf.set_text_color(255, 255, 255)
        pdf.cell(140, 8, "Medico", 1, 0, 'C', True); pdf.cell(50, 8, "Total (R$)", 1, 1, 'C', True)
        pdf.set_font("Arial", '', 10); pdf.set_text_color(0, 0, 0); total_geral = 0
        for _, r in resumo.iterrows():
            pdf.cell(140, 8, str(r['doctor_name']), 1); valor = float(r['Total']); total_geral += valor
            valor_fmt = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."); pdf.cell(50, 8, valor_fmt, 1, 1, 'R')

        pdf.set_font("Arial", 'B', 10); pdf.set_fill_color(240, 240, 240); pdf.set_text_color(0, 45, 98)
        pdf.cell(140, 8, "TOTAL GERAL", 1, 0, 'R', True)
        total_fmt = f"{total_geral:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        pdf.cell(50, 8, total_fmt, 1, 1, 'R', True)

        return bytes(pdf.output(dest='S'))

    st.divider()
    c_save, c_pdf, c_csv = st.columns(3)

    with c_save:
        if st.button("💾 SALVAR ESCALA MENSAL", type="primary", use_container_width=True):
            batch = []
            shifts = ['Manhã', 'Tarde', 'Noite']
            for week_idx, (w_days, ed) in enumerate(all_edits):
                for idx, day in enumerate(w_days):
                    if day > 0:
                        dt = datetime.date(ano, mes_num, day)
                        for row_idx, shift in enumerate(shifts):
                            col_name = f"w{week_idx}_d{idx}"
                            doc_name = ed.at[row_idx, col_name]
                            if doc_name: batch.append((dt, shift, doc_name))

            if batch:
                execute_query("""INSERT INTO shift_schedule (shift_date, shift_time, doctor_name) VALUES %s ON CONFLICT (shift_date, shift_time) DO UPDATE SET doctor_name = EXCLUDED.doctor_name;""", batch)
            st.success("Escala salva!"); st.rerun()

    with c_pdf:
        if not df_pivot.empty:
            pdf_bytes = generate_pdf_semanal(weeks, df_pivot, resumo_rh, mes_nome, ano)
            st.download_button(label="📄 Relatório Completo (PDF)", data=pdf_bytes, file_name=f"Escala_{mes_nome}_{ano}.pdf", mime="application/pdf", use_container_width=True)
        else:
            st.caption("Sem escala pra gerar PDF ainda.")

    with c_csv:
        if not resumo_rh.empty:
            csv_financeiro = resumo_rh.rename(columns={'doctor_name': 'Medico', 'Total': 'Total_Reais'}).to_csv(index=False).encode('utf-8')
            st.download_button(label="📊 Fechamento (CSV)", data=csv_financeiro, file_name=f"Fechamento_{mes_nome}_{ano}.csv", mime="text/csv", use_container_width=True)
        else:
            st.caption("Sem fechamento financeiro pra exportar ainda.")
