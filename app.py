import streamlit as st
import pandas as pd
import psycopg2
import psycopg2.extras
import datetime
import calendar
import hashlib
import os
from fpdf import FPDF
from io import BytesIO

# ==========================================
# 0. CONFIGURAÇÃO E CSS CORPORATIVO (AZUL E BRANCO)
# ==========================================
st.set_page_config(page_title="Radiologia HELP - Gestão de Escala", layout="wide")

st.markdown(
    """
    <style>
    /* FUNDO PRINCIPAL E CONTRASTE BASE */
    .stApp { background-color: #FFFFFF; color: #002D62; }
    
    /* CORREÇÃO VISUAL PARA MODO ESCURO NATIVO (Força textos azuis no fundo branco) */
    h1, h2, h3, h4, h5, h6 { color: #002D62 !important; }
    label, .stSelectbox label, .stSelectbox p { color: #002D62 !important; }
    
    /* BARRA LATERAL (Blindagem para manter texto branco no fundo escuro) */
    [data-testid="stSidebar"] { background-color: #1E2633; border-right: 1px solid #D1D5DB; }
    [data-testid="stSidebar"] p, [data-testid="stSidebar"] span, [data-testid="stSidebar"] h3, [data-testid="stSidebar"] label { color: #FFFFFF !important; }
    
    .hospital-title { text-align: center; color: #FFFFFF !important; font-family: 'Segoe UI', sans-serif; font-weight: bold; font-size: 2.2em; line-height: 1.1; margin-bottom: 25px; margin-top: 10px; }
    
    /* PLANILHAS DE DADOS */
    [data-testid="stDataEditor"] button, [data-testid="stDataEditor"] svg, [data-testid="stDataEditor"] .glideDataGrid-header-menu-button { display: none !important; visibility: hidden !important; }
    [data-testid="stDataEditor"] { background-color: #FFFFFF; border: 1.5px solid #000000 !important; }
    [data-testid="stDataEditor"] [role="columnheader"], [data-testid="stDataEditor"] [role="rowheader"] { background-color: #002D62 !important; color: #FFFFFF !important; font-weight: bold !important; border: 1px solid #000000 !important; pointer-events: none !important; }
    [data-testid="stDataEditor"] div[role="gridcell"] { border: 1px solid #000000 !important; color: #000000 !important; background-color: #FFFFFF !important; }
    
    /* BOTÕES GLOBAIS E POPOVER (Garante fundo corporativo) */
    div.stButton > button, 
    [data-testid="stPopover"] > button,
    div.stDownloadButton > button { 
        background-color: #002D62 !important; 
        color: #FFFFFF !important; 
        border: 1px solid #00AEEF !important; 
        font-weight: bold !important; 
        height: 3em !important; 
    }
    
    /* SELETOR UNIVERSAL (*): Força cor branca em qualquer tag (p, span, div) dentro dos botões */
    div.stButton > button *, 
    [data-testid="stPopover"] > button *,
    div.stDownloadButton > button * { 
        color: #FFFFFF !important; 
    }
    
    .stTabs [data-baseweb="tab-list"] { gap: 24px; }
    .stTabs [data-baseweb="tab"] { font-weight: bold; color: #002D62; }
    </style>
    """,
    unsafe_allow_html=True
)

# ==========================================
# 1. ARQUITETURA DE BANCO DE DADOS (BLINDADA)
# ==========================================
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

    conn = psycopg2.connect(
        db_url,
        options="-c client_encoding=utf8",
        connect_timeout=10 
    )
    conn.autocommit = True
    return conn

def execute_query(query: str, params=None) -> None:
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
        st.cache_resource.clear()
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
        st.cache_resource.clear()
        return _fetch()

def init_db():
    query = """
    CREATE TABLE IF NOT EXISTS doctors (name TEXT PRIMARY KEY);
    
    /* Tabelas Escala Geral */
    CREATE TABLE IF NOT EXISTS shift_schedule (shift_date DATE, shift_time VARCHAR(10), doctor_name TEXT, PRIMARY KEY(shift_date, shift_time));
    CREATE TABLE IF NOT EXISTS fixed_schedule_4w (week_num INT, weekday INT, shift_time VARCHAR(10), doctor_name TEXT, PRIMARY KEY(week_num, weekday, shift_time));
    
    /* Tabelas Escala TC Eletiva */
    CREATE TABLE IF NOT EXISTS shift_schedule_tc (shift_date DATE, shift_time VARCHAR(10), doctor_name TEXT, PRIMARY KEY(shift_date, shift_time));
    CREATE TABLE IF NOT EXISTS fixed_schedule_tc_4w (week_num INT, weekday INT, shift_time VARCHAR(10), doctor_name TEXT, PRIMARY KEY(week_num, weekday, shift_time));
    """
    execute_query(query)

try:
    init_db()
except Exception as e:
    st.error("🚨 Falha Crítica: Banco de Dados Inacessível.")
    st.error("O Supabase pode estar pausado ou a variável DATABASE_URL está incorreta.")
    st.code(str(e))
    st.stop()

# ==========================================
# 2. LOGIN (Hash '1234')
# ==========================================
if 'auth' not in st.session_state: st.session_state['auth'] = False
if not st.session_state['auth']:
    st.title("🔐 Acesso - Hospital HELP")
    pw = st.text_input("Senha de Acesso", type="password")
    if st.button("Entrar", use_container_width=True):
        if hashlib.sha256(str.encode(pw)).hexdigest() == "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4":
            st.session_state['auth'] = True
            st.rerun()
    st.stop()

# ==========================================
# 3. SIDEBAR, ICAL E CONTINGÊNCIA
# ==========================================
with st.sidebar:
    st.markdown("<div class='hospital-title'>Hospital<br>HELP</div>", unsafe_allow_html=True)
    st.divider()
    
    df_docs = fetch_data("SELECT name FROM doctors ORDER BY name;")
    lista_medicos = [""] + df_docs['name'].tolist()
    medico_alvo = st.selectbox("👤 Destacar na escala:", lista_medicos)
    
    if medico_alvo != "":
        st.subheader("📅 Sincronizar Agenda")
        st.download_button(
            label=f"📥 Baixar Plantões - {medico_alvo}",
            data="BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR",
            file_name=f"Plantões_{medico_alvo}.ics",
            mime="text/calendar",
            use_container_width=True
        )

    st.divider()
    st.subheader("👨‍⚕️ Gestão de Equipe")
    novo = st.text_input("Novo Médico")
    if st.button("Adicionar"):
        if novo:
            execute_query("INSERT INTO doctors (name) VALUES (%s) ON CONFLICT DO NOTHING;", (novo.strip(),))
            st.cache_data.clear(); st.rerun()
    
    for m in df_docs['name']:
        c1, c2 = st.columns([5, 1])
        c1.write(f"• {m}")
        if c2.button("X", key=f"del_{m}"):
            execute_query("DELETE FROM doctors WHERE name = %s;", (m,))
            st.cache_data.clear(); st.rerun()

    st.divider()
    st.subheader("⚠️ Contingência")
    df_backup = fetch_data("SELECT * FROM shift_schedule;")
    if not df_backup.empty:
        st.download_button("Exportar Geral (CSV)", data=df_backup.to_csv(index=False).encode('utf-8'), file_name="backup_geral.csv", mime="text/csv", use_container_width=True)

# ==========================================
# 4. ABAS E INTERFACE
# ==========================================
st.title("🏥 Gestão de Escala de Radiologia")

# ADICIONADO: 4 Abas para controle total
tab_escala, tab_tc, tab_padrao, tab_padrao_tc = st.tabs([
    "📅 Escala Mensal", 
    "🖥️ Escala TC Eletiva", 
    "⚙️ Padrão Rotativo (Geral)", 
    "⚙️ Padrão Rotativo (TC)"
])

# ---------------------------------------------------------
# ABA 3: PADRÃO ROTATIVO (GERAL - 4 Semanas)
# ---------------------------------------------------------
with tab_padrao:
    c_info, c_vazio, c_btn_padrao = st.columns([4, 1, 2])
    with c_info:
        st.subheader("Configurar Escala Espelho - Geral")
        st.caption("Preencha o ciclo de 4 semanas.")
    with c_btn_padrao:
        st.write("")
        if st.button("💾 Salvar Padrão Fixo Geral", type="primary", use_container_width=True, key="btn_salvar_padrao"):
            batch_fix = []
            for w_num, ed_fix in st.session_state.get('edits_padrao', []):
                for shift in ['Manhã', 'Tarde', 'Noite']:
                    for wd in range(7):
                        doc = ed_fix.at[shift, str(wd)]
                        if doc: batch_fix.append((w_num, wd, shift, doc))
            execute_query("DELETE FROM fixed_schedule_4w;") 
            if batch_fix:
                execute_query("INSERT INTO fixed_schedule_4w (week_num, weekday, shift_time, doctor_name) VALUES %s;", batch_fix)
            st.cache_data.clear(); st.success("Padrão fixo atualizado!")
            
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
            
        w_conf_fix = {str(c): st.column_config.SelectboxColumn(week_headers_fix[c], options=lista_medicos, width="small") for c in range(7)}
        ed_fix = st.data_editor(df_fix_pivot, column_config=w_conf_fix, use_container_width=True, key=f"ed_fixa_w{w_num}")
        all_fix_edits.append((w_num, ed_fix))
    
    st.session_state['edits_padrao'] = all_fix_edits

# ---------------------------------------------------------
# ABA 4: PADRÃO ROTATIVO (TC ELETIVA - 4 Semanas)
# ---------------------------------------------------------
with tab_padrao_tc:
    c_info_tc, c_vazio_tc, c_btn_padrao_tc = st.columns([4, 1, 2])
    with c_info_tc:
        st.subheader("Configurar Escala Espelho - TC Eletiva")
        st.caption("Preencha o ciclo de 4 semanas (Apenas Manhã e Tarde).")
    with c_btn_padrao_tc:
        st.write("")
        if st.button("💾 Salvar Padrão Fixo TC", type="primary", use_container_width=True, key="btn_salvar_padrao_tc"):
            batch_fix_tc = []
            for w_num, ed_fix_tc in st.session_state.get('edits_padrao_tc', []):
                for shift in ['Manhã', 'Tarde']:
                    for wd in range(7):
                        doc = ed_fix_tc.at[shift, str(wd)]
                        if doc: batch_fix_tc.append((w_num, wd, shift, doc))
            execute_query("DELETE FROM fixed_schedule_tc_4w;") 
            if batch_fix_tc:
                execute_query("INSERT INTO fixed_schedule_tc_4w (week_num, weekday, shift_time, doctor_name) VALUES %s;", batch_fix_tc)
            st.cache_data.clear(); st.success("Padrão fixo TC atualizado!")
            
    df_fix_raw_tc = fetch_data("SELECT week_num, weekday, shift_time, doctor_name FROM fixed_schedule_tc_4w")
    week_headers_fix_tc = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    cols_str_tc = [str(i) for i in range(7)]
    all_fix_edits_tc = []
    
    for w_num in range(4):
        st.markdown(f"#### Semana {w_num + 1}")
        df_w_raw_tc = df_fix_raw_tc[df_fix_raw_tc['week_num'] == w_num] if not df_fix_raw_tc.empty else pd.DataFrame()
        if not df_w_raw_tc.empty:
            df_fix_pivot_tc = df_w_raw_tc.pivot(index='shift_time', columns='weekday', values='doctor_name').reindex(['Manhã', 'Tarde'])
            df_fix_pivot_tc.columns = [str(int(c)) for c in df_fix_pivot_tc.columns]
            df_fix_pivot_tc = df_fix_pivot_tc.reindex(columns=cols_str_tc).fillna("")
        else:
            df_fix_pivot_tc = pd.DataFrame("", index=['Manhã', 'Tarde'], columns=cols_str_tc)
            
        w_conf_fix_tc = {str(c): st.column_config.SelectboxColumn(week_headers_fix_tc[c], options=lista_medicos, width="small") for c in range(7)}
        ed_fix_tc = st.data_editor(df_fix_pivot_tc, column_config=w_conf_fix_tc, use_container_width=True, key=f"ed_fixa_tc_w{w_num}")
        all_fix_edits_tc.append((w_num, ed_fix_tc))
    
    st.session_state['edits_padrao_tc'] = all_fix_edits_tc


# ---------------------------------------------------------
# ABA 1: ESCALA DO MÊS GERAL
# ---------------------------------------------------------
with tab_escala:
    meses = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
    col_m, col_a, col_space, col_reset = st.columns([2, 1.5, 3, 2.5])
    
    with col_m: mes_nome = st.selectbox("Mês de Referência", meses, index=datetime.date.today().month - 1)
    with col_a: ano = st.selectbox("Ano", [2026])
    mes_num = meses.index(mes_nome) + 1
    
    with col_reset:
        st.write("") 
        with st.popover("✨ Resetar para Padrão Fixo", use_container_width=True):
            st.error("⚠️ Esta ação apagará permanentemente todas as edições manuais deste mês.")
            trava_seguranca = st.checkbox("Estou ciente. Substituir escala.")
            
            if st.button("Confirmar Execução", type="primary", use_container_width=True, disabled=not trava_seguranca):
                df_fix = fetch_data("SELECT week_num, weekday, shift_time, doctor_name FROM fixed_schedule_4w WHERE doctor_name != ''")
                if not df_fix.empty:
                    fix_map = {(r['week_num'], int(r['weekday']), r['shift_time']): r['doctor_name'] for _, r in df_fix.iterrows()}
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
                    
                    execute_query("DELETE FROM shift_schedule WHERE EXTRACT(YEAR FROM shift_date) = %s AND EXTRACT(MONTH FROM shift_date) = %s", (ano, mes_num))
                    execute_query("INSERT INTO shift_schedule (shift_date, shift_time, doctor_name) VALUES %s;", batch_a)
                    st.cache_data.clear(); st.rerun()

    df_raw = fetch_data("SELECT shift_date, shift_time, doctor_name FROM shift_schedule WHERE EXTRACT(YEAR FROM shift_date) = %s AND EXTRACT(MONTH FROM shift_date) = %s", (ano, mes_num))

    if not df_raw.empty:
        df_raw['dia'] = pd.to_datetime(df_raw['shift_date']).dt.day
        df_pivot = df_raw.pivot(index='shift_time', columns='dia', values='doctor_name').reindex(['Manhã', 'Tarde', 'Noite']).fillna("")
    else: 
        df_pivot = pd.DataFrame(index=['Manhã', 'Tarde', 'Noite'])

    if medico_alvo != "":
        st.subheader(f"📍 Módulo WhatsApp: Plantões de {medico_alvo}")
        def style_highlight(val):
            return 'background-color: #00AEEF; color: #FFFFFF; font-weight: bold; border: 1px solid black;' if val == medico_alvo else 'background-color: #FFFFFF; color: #000000;'
        st.dataframe(df_pivot.style.map(style_highlight), use_container_width=True, hide_index=False)

    st.divider()
    st.subheader("📝 Edição da Escala Mensal")
    calendar.setfirstweekday(calendar.MONDAY)
    weeks = calendar.monthcalendar(ano, mes_num)
    week_headers = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    all_edits = []

    for i, week in enumerate(weeks):
        st.markdown(f"#### Semana {i+1}")
        w_data = {f"w{i}_d{idx}": (["", "", ""] if day == 0 else (df_pivot[day].tolist() if day in df_pivot.columns else ["","",""])) for idx, day in enumerate(week)}
        for k in w_data: w_data[k] = ["" if x is None or x == "None" else x for x in w_data[k]]
        
        df_w_raw = pd.DataFrame(w_data)
        df_w_raw.index = ['Manhã', 'Tarde', 'Noite']
        
        w_conf = {f"w{i}_d{idx}": (st.column_config.TextColumn(week_headers[idx], disabled=True, width="small") if day == 0 else st.column_config.SelectboxColumn(f"{week_headers[idx]} {day:02d}", options=lista_medicos, width="small")) for idx, day in enumerate(week)}
        
        df_to_edit = df_w_raw.reset_index().rename(columns={'index': 'Turno'})
        headers_final = {'Turno': st.column_config.TextColumn("Turno", disabled=True, width="small")}
        headers_final.update(w_conf)
        
        ed = st.data_editor(df_to_edit, column_config=headers_final, use_container_width=True, key=f"ed_m_{i}", hide_index=True)
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

    c_save, c_pdf = st.columns(2)
    
    with c_save:
        if st.button("💾 SALVAR ESCALA GERAL", type="primary", use_container_width=True):
            batch = []
            shifts = ['Manhã', 'Tarde', 'Noite']
            for week_idx, (w_days, ed) in enumerate(all_edits):
                for idx, day in enumerate(w_days):
                    if day > 0:
                        dt = datetime.date(ano, mes_num, day)
                        for row_idx, shift in enumerate(shifts):
                            col_name = f"w{week_idx}_d{idx}"
                            doc_name = ed.at[row_idx, col_name]
                            batch.append((dt, shift, doc_name))
            
            execute_query("""INSERT INTO shift_schedule (shift_date, shift_time, doctor_name) VALUES %s ON CONFLICT (shift_date, shift_time) DO UPDATE SET doctor_name = EXCLUDED.doctor_name;""", batch)
            st.cache_data.clear(); st.success("Escala salva!"); st.rerun()

    with c_pdf:
        if not df_pivot.empty:
            pdf_bytes = generate_pdf_semanal(weeks, df_pivot, resumo_rh, mes_nome, ano)
            st.download_button(label="📄 BAIXAR RELATÓRIO PDF", data=pdf_bytes, file_name=f"Escala_{mes_nome}_{ano}.pdf", mime="application/pdf", use_container_width=True)

# ---------------------------------------------------------
# ABA 2: ESCALA TC ELETIVA (SEM FINANCEIRO E SEM NOITE)
# ---------------------------------------------------------
with tab_tc:
    col_m_tc, col_a_tc, col_space_tc, col_reset_tc = st.columns([2, 1.5, 3, 2.5])
    
    with col_m_tc: mes_nome_tc = st.selectbox("Mês de Referência", meses, index=datetime.date.today().month - 1, key="sel_mes_tc")
    with col_a_tc: ano_tc = st.selectbox("Ano", [2026], key="sel_ano_tc")
    mes_num_tc = meses.index(mes_nome_tc) + 1
    
    with col_reset_tc:
        st.write("") 
        with st.popover("✨ Resetar para Padrão TC", use_container_width=True):
            st.error("⚠️ Esta ação apagará permanentemente todas as edições manuais deste mês na TC.")
            trava_seguranca_tc = st.checkbox("Estou ciente. Substituir escala TC.", key="trava_tc")
            
            if st.button("Confirmar Execução", type="primary", use_container_width=True, disabled=not trava_seguranca_tc, key="btn_exec_tc"):
                df_fix_tc = fetch_data("SELECT week_num, weekday, shift_time, doctor_name FROM fixed_schedule_tc_4w WHERE doctor_name != ''")
                if not df_fix_tc.empty:
                    fix_map_tc = {(r['week_num'], int(r['weekday']), r['shift_time']): r['doctor_name'] for _, r in df_fix_tc.iterrows()}
                    batch_a_tc = []
                    cal_w_tc = calendar.monthcalendar(ano_tc, mes_num_tc)
                    for i, w in enumerate(cal_w_tc):
                        p_w = i % 4
                        for wd, day in enumerate(w):
                            if day > 0:
                                dt = datetime.date(ano_tc, mes_num_tc, day)
                                # Apenas Manhã e Tarde
                                for s in ['Manhã', 'Tarde']:
                                    doc = fix_map_tc.get((p_w, wd, s), "")
                                    if doc: batch_a_tc.append((dt, s, doc))
                    
                    execute_query("DELETE FROM shift_schedule_tc WHERE EXTRACT(YEAR FROM shift_date) = %s AND EXTRACT(MONTH FROM shift_date) = %s", (ano_tc, mes_num_tc))
                    execute_query("INSERT INTO shift_schedule_tc (shift_date, shift_time, doctor_name) VALUES %s;", batch_a_tc)
                    st.cache_data.clear(); st.rerun()

    df_raw_tc = fetch_data("SELECT shift_date, shift_time, doctor_name FROM shift_schedule_tc WHERE EXTRACT(YEAR FROM shift_date) = %s AND EXTRACT(MONTH FROM shift_date) = %s", (ano_tc, mes_num_tc))

    if not df_raw_tc.empty:
        df_raw_tc['dia'] = pd.to_datetime(df_raw_tc['shift_date']).dt.day
        # Ajustado para remover a Noite
        df_pivot_tc = df_raw_tc.pivot(index='shift_time', columns='dia', values='doctor_name').reindex(['Manhã', 'Tarde']).fillna("")
    else: 
        # Ajustado para remover a Noite
        df_pivot_tc = pd.DataFrame(index=['Manhã', 'Tarde'])

    if medico_alvo != "":
        st.subheader(f"📍 Módulo WhatsApp: Plantões de {medico_alvo} (TC Eletiva)")
        def style_highlight_tc(val):
            return 'background-color: #00AEEF; color: #FFFFFF; font-weight: bold; border: 1px solid black;' if val == medico_alvo else 'background-color: #FFFFFF; color: #000000;'
        st.dataframe(df_pivot_tc.style.map(style_highlight_tc), use_container_width=True, hide_index=False)

    st.divider()
    st.subheader("📝 Edição da Escala TC Eletiva")
    calendar.setfirstweekday(calendar.MONDAY)
    weeks_tc = calendar.monthcalendar(ano_tc, mes_num_tc)
    all_edits_tc = []

    for i, week in enumerate(weeks_tc):
        st.markdown(f"#### Semana {i+1}")
        # Ajustado para retornar listas de apenas 2 posições (Manhã e Tarde)
        w_data_tc = {f"wtc{i}_d{idx}": (["", ""] if day == 0 else (df_pivot_tc[day].tolist() if day in df_pivot_tc.columns else ["",""])) for idx, day in enumerate(week)}
        for k in w_data_tc: w_data_tc[k] = ["" if x is None or x == "None" else x for x in w_data_tc[k]]
        
        df_w_raw_tc = pd.DataFrame(w_data_tc)
        df_w_raw_tc.index = ['Manhã', 'Tarde']
        
        w_conf_tc = {f"wtc{i}_d{idx}": (st.column_config.TextColumn(week_headers[idx], disabled=True, width="small") if day == 0 else st.column_config.SelectboxColumn(f"{week_headers[idx]} {day:02d}", options=lista_medicos, width="small")) for idx, day in enumerate(week)}
        
        df_to_edit_tc = df_w_raw_tc.reset_index().rename(columns={'index': 'Turno'})
        headers_final_tc = {'Turno': st.column_config.TextColumn("Turno", disabled=True, width="small")}
        headers_final_tc.update(w_conf_tc)
        
        ed_tc = st.data_editor(df_to_edit_tc, column_config=headers_final_tc, use_container_width=True, key=f"ed_tc_w_{i}", hide_index=True)
        all_edits_tc.append((week, ed_tc))

    st.divider()
    
    # FUNÇÃO DE PDF EXCLUSIVA PARA TC ELETIVA (SEM FINANCEIRO E SEM NOITE)
    def generate_pdf_tc(weeks, pivot, mes, ano):
        pdf = FPDF(orientation='L', unit='mm', format='A4')
        pdf.add_page()
        total_semanas = len(weeks)
        
        if total_semanas <= 4: font_tit = 18; font_tab = 9; h_row = 7; margin_w = 5
        elif total_semanas == 5: font_tit = 16; font_tab = 8; h_row = 6; margin_w = 3
        else: font_tit = 14; font_tab = 7; h_row = 4.5; margin_w = 2

        pdf.set_font("Arial", 'B', font_tit); pdf.set_text_color(0, 45, 98)
        pdf.cell(0, 10, f"HOSPITAL HELP - ESCALA TC ELETIVA - {mes.upper()} / {ano}", ln=True, align='C'); pdf.ln(2)

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

            # Ajustado para Manhã e Tarde apenas
            for shift in ['Manhã', 'Tarde']:
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
        return bytes(pdf.output(dest='S'))

    c_save_tc, c_pdf_tc = st.columns(2)
    
    with c_save_tc:
        if st.button("💾 SALVAR ESCALA TC ELETIVA", type="primary", use_container_width=True):
            batch_tc = []
            # Ajustado para Manhã e Tarde apenas
            shifts = ['Manhã', 'Tarde']
            for week_idx, (w_days, ed) in enumerate(all_edits_tc):
                for idx, day in enumerate(w_days):
                    if day > 0:
                        dt = datetime.date(ano_tc, mes_num_tc, day)
                        for row_idx, shift in enumerate(shifts):
                            col_name = f"wtc{week_idx}_d{idx}"
                            doc_name = ed.at[row_idx, col_name]
                            batch_tc.append((dt, shift, doc_name))
            
            execute_query("""INSERT INTO shift_schedule_tc (shift_date, shift_time, doctor_name) VALUES %s ON CONFLICT (shift_date, shift_time) DO UPDATE SET doctor_name = EXCLUDED.doctor_name;""", batch_tc)
            st.cache_data.clear(); st.success("Escala TC Eletiva salva!"); st.rerun()

    with c_pdf_tc:
        if not df_pivot_tc.empty:
            pdf_bytes_tc = generate_pdf_tc(weeks_tc, df_pivot_tc, mes_nome_tc, ano_tc)
            st.download_button(label="📄 BAIXAR RELATÓRIO TC PDF", data=pdf_bytes_tc, file_name=f"Escala_TC_{mes_nome_tc}_{ano_tc}.pdf", mime="application/pdf", use_container_width=True)
