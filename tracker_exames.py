"""Tracker de TC/RX integrado ao banco e à escala do Hospital HELP."""
import datetime as dt
import io
import json
import re
import unicodedata
import zipfile
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

TZ = ZoneInfo('America/Fortaleza')
RULES = {
    'hosp trauma': ('🔴 Trauma', 2, 'plantao'),
    'hospital de trauma': ('🔴 Trauma', 2, 'plantao'),
    'pronto atendimento': ('🟠 Pronto Atendimento', 2, 'plantao'),
    'urgente': ('🟢 Urgente', 6, 'plantao'),
    'oncologia': ('🔵 Oncologia', 96, 'laudos'),
    'internado': ('🩷 Internado', 6, 'internado'),
    'internacao': ('🩷 Internado', 6, 'internado'),
    '': ('⚪ Rotina', 120, 'laudos'),
}


def normalize(value):
    text = '' if pd.isna(value) else str(value).strip().lower()
    return ''.join(c for c in unicodedata.normalize('NFKD', text)
                   if not unicodedata.combining(c))


def modality_group(value):
    # Combinações DICOM como CT\OT e OT\DX representam um exame,
    # e não exames adicionais por token. OT isolado não é classificado.
    tokens = set(re.split(r'[\\/;,\s]+', str(value).upper().strip()))
    ct = bool(tokens & {'CT', 'TC'})
    rx = bool(tokens & {'DX', 'CR', 'RX'})
    if ct and rx:
        return 'TC/RX — conferir'
    return 'TC' if ct else 'RX' if rx else None


def assignment_slot(performed, kind):
    day, clock = performed.date(), performed.time()
    if kind == 'laudos':
        return reporting_slot(performed)
    if clock < dt.time(6, 45):
        return day - dt.timedelta(days=1), 'Noite'
    if clock < dt.time(13):
        return day, 'Manhã'
    if kind == 'internado' or clock >= dt.time(18, 45):
        return day, 'Noite'
    return day, 'Tarde'


def reporting_slot(performed):
    day = performed.date()
    weekday = day.weekday()
    if weekday == 0:
        return day, 'Otávio'
    if weekday == 6:
        return day, 'Domingo — conferir'
    offset = 3 if weekday == 4 else 0 if weekday == 5 else 1
    # Exames excepcionais fora de 07–19h mantêm o responsável
    # do período adjacente, sem transferir para o plantão noturno.
    slot = 'Manhã' if performed.time() < dt.time(13) else 'Tarde'
    return day + dt.timedelta(days=offset), slot


def reporting_owner(performed, shifts, monday_doctor):
    day, slot = reporting_slot(performed)
    if slot == 'Otávio':
        return monday_doctor, f'{day:%d/%m/%Y} · Otávio (fixo)', '' if monday_doctor else 'Otávio não identificado no cadastro ativo'
    if slot == 'Domingo — conferir':
        return None, f'{day:%d/%m/%Y} · Domingo', 'Exame eletivo no domingo — conferir'
    owner = shifts.get((day, slot))
    if not owner:
        # Havendo somente um médico diurno, ele responde pelos dois períodos.
        doctors = {shifts.get((day, period)) for period in ('Manhã', 'Tarde')} - {None, ''}
        if len(doctors) == 1:
            owner = doctors.pop()
    return owner, f'{day:%d/%m/%Y} · {slot} (escala de referência)', '' if owner else 'Sem médico diurno na escala de referência'


def read_upload(filename, content):
    if filename.lower().endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = [i for i in archive.infolist() if not i.is_dir()
                     and not i.filename.startswith('__MACOSX/')
                     and i.filename.lower().endswith(('.xls', '.xlsx', '.csv'))]
            if len(files) != 1:
                raise ValueError('O ZIP precisa conter exatamente uma planilha XLS, XLSX ou CSV.')
            if files[0].file_size > 30_000_000:
                raise ValueError('A planilha excede 30 MB descompactada.')
            return read_upload(files[0].filename, archive.read(files[0]))
    if filename.lower().endswith('.csv'):
        try:
            return pd.read_csv(io.BytesIO(content), sep=None, engine='python', dtype=str)
        except UnicodeDecodeError:
            return pd.read_csv(io.BytesIO(content), sep=None, engine='python', dtype=str, encoding='latin1')
    return pd.read_excel(io.BytesIO(content), dtype=str,
                         engine='xlrd' if filename.lower().endswith('.xls') else 'openpyxl')


def prepare_records(frame):
    frame = frame.fillna('').copy()
    frame.columns = [str(c).strip() for c in frame.columns]
    required = {'Prioridade', 'Nome do paciente', 'Data do exame', 'Modalidade', 'Descrição do exame'}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError('Colunas ausentes: ' + ', '.join(sorted(missing)))
    records, excluded = [], 0
    for index, row in frame.iterrows():
        group = modality_group(row['Modalidade'])
        if group is None:
            excluded += 1
            continue
        # A prioridade é sempre a fonte da classificação; convênio não a altera.
        records.append({
            'linha': int(index) + 2, 'id': str(row.get('ID', '')),
            'acesso': str(row.get('Accession Nº', '')),
            'paciente': str(row['Nome do paciente']),
            'exame': str(row['Descrição do exame']), 'modalidade': group,
            'prioridade': str(row['Prioridade']),
            'realizacao': str(row['Data do exame']),
        })
    return records, excluded


def parse_performed(text):
    # Não inventa meia-noite quando o horário não veio na exportação.
    if not re.search(r'\d{1,2}:\d{2}', text):
        return None
    value = pd.to_datetime(text, dayfirst=True, errors='coerce')
    if pd.isna(value):
        return None
    value = value.to_pydatetime()
    return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)


def duration(seconds):
    minutes = max(0, int(abs(seconds) // 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f'{days}d {hours}h {minutes:02d}min'
    if hours:
        return f'{hours}h {minutes:02d}min'
    return f'{minutes}min' if minutes else 'menos de 1 min'


def build_tracker(records, shifts, monday_doctor=None, now=None):
    now = now or dt.datetime.now(TZ)
    rows = []
    for record in records:
        rule = RULES.get(normalize(record['prioridade']))
        performed = parse_performed(record['realizacao'])
        issues = []
        owner, slot_label, deadline, remaining = None, '', None, None
        if not rule:
            issues.append('Prioridade não reconhecida')
        if performed is None:
            issues.append('Data/hora inválida ou incompleta')
        elif performed > now:
            issues.append('Realização no futuro')
        if record['modalidade'] == 'TC/RX — conferir':
            issues.append('Modalidade ambígua')
        if rule and performed:
            if rule[2] == 'laudos':
                owner, slot_label, issue = reporting_owner(performed, shifts, monday_doctor)
                if issue:
                    issues.append(issue)
            else:
                day, slot = assignment_slot(performed, rule[2])
                slot_label = f'{day:%d/%m/%Y} · {slot}'
                owner = shifts.get((day, slot))
                if not owner:
                    issues.append('Sem médico na escala de plantão')
            deadline = performed + dt.timedelta(hours=rule[1])
            remaining = (deadline - now).total_seconds()
        if issues:
            state = 'Conferir'
        elif remaining < 0:
            state = 'Atrasado'
        else:
            state = 'No prazo'
        time_label = '' if remaining is None else (
            'Atrasado há ' + duration(remaining) if remaining < 0 else
            'Vence agora' if remaining == 0 else 'Faltam ' + duration(remaining))
        rows.append({
            'Médico': owner or 'Sem responsável', 'Paciente': record['paciente'],
            'Exame': record['exame'], 'Modalidade': record['modalidade'],
            'Prioridade': record['prioridade'] or 'Sem prioridade',
            'Categoria': rule[0] if rule else 'Não reconhecida',
            'Realização': record['realizacao'],
            'Vencimento': deadline.strftime('%d/%m/%Y %H:%M') if deadline else '',
            'Situação': state, 'Prazo': time_label, 'Escala de referência': slot_label,
            'Conferência': '; '.join(issues), 'ID': record['id'],
            'Accession Nº': record['acesso'], 'Linha de origem': record['linha'],
            '_segundos': remaining if remaining is not None else float('inf'),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values('_segundos', kind='stable').reset_index(drop=True)


def whatsapp_deadline(seconds):
    if seconds < 0:
        days = int(abs(seconds) // 86400)
        if days:
            return f"em atraso há {days} {'dia' if days == 1 else 'dias'}"
        return 'em atraso há ' + duration(seconds)
    return 'vence agora' if seconds == 0 else 'faltam ' + duration(seconds)


def build_whatsapp_message(recipient, exams, now):
    lines = [f'Olá, {recipient}.', '',
             f'Essas são suas pendências de laudo TC/RX do Hospital HELP — atualização de {now:%d/%m/%Y %H:%M}.']
    by_day = {}
    for _, exam in exams.iterrows():
        day = parse_performed(exam['Realização']).date()
        by_day.setdefault(day, {}).setdefault(exam['Modalidade'], []).append(exam)
    for day in sorted(by_day):
        lines.extend(['', day.strftime('%d/%m')])
        for modality in ('TC', 'RX'):
            entries = by_day[day].get(modality, [])
            if not entries:
                continue
            lines.extend(['', f'- {modality}:'])
            for exam in sorted(entries, key=lambda r: r['_segundos']):
                lines.append(f"  - {exam['Paciente']} // {exam['Exame']} // {whatsapp_deadline(exam['_segundos'])}")
    return '\n'.join(lines)


def ensure_tables(execute_query):
    execute_query('''CREATE TABLE IF NOT EXISTS exam_tracker_snapshot (
        id INTEGER PRIMARY KEY CHECK (id=1), payload TEXT NOT NULL,
        source_name TEXT NOT NULL, imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );''')


def render_tracker(fetch_data, execute_query, fetch_doctors):
    if not st.session_state.get('admin_auth'):
        st.error('Desbloqueie a administração para acessar o tracker.')
        return
    ensure_tables(execute_query)
    st.header('⏱️ Tracker de exames · TC e RX')
    st.caption('Prazos desde a realização · horário de Fortaleza · classificação pela coluna Prioridade.')
    with st.expander('Regras de prazo e responsabilidade'):
        st.table(pd.DataFrame([
            ['Hosp Trauma', '2h', 'Turno da realização'],
            ['Pronto Atendimento', '2h', 'Turno da realização'],
            ['Urgente', '6h', 'Turno da realização'],
            ['Internado', '6h', '06h45–13h: manhã; 13h–24h: noite do dia; madrugada: noite anterior'],
            ['Oncologia', '4 dias corridos', 'Escala diurna conforme regra semanal abaixo'],
            ['Sem prioridade', '5 dias corridos', 'Escala diurna conforme regra semanal abaixo'],
        ], columns=['Prioridade', 'Prazo', 'Responsável']))
    docs = fetch_doctors()
    otavios = {int(r['id']): str(r['name']) for _,r in docs.iterrows()
               if r['ativo'] and 'otavio' in re.findall(r'\w+', normalize(r['name']))}
    monday_doctor = next(iter(otavios.values())) if len(otavios) == 1 else None
    with st.expander('📅 Responsáveis por Oncologia e Rotina'):
        st.table(pd.DataFrame([
            ['Segunda', 'Otávio (fixo)'], ['Terça', 'Escala da quarta'],
            ['Quarta', 'Escala da quinta'], ['Quinta', 'Escala da sexta'],
            ['Sexta', 'Escala da segunda seguinte'], ['Sábado', 'Escala do próprio sábado'],
            ['Domingo', 'Sem exames eletivos; ocorrência exige conferência'],
        ], columns=['Dia do exame', 'Escala de referência']))
        st.caption('07h–13h: médico da manhã; 13h–19h: médico da tarde. Antes das 07h permanece com a manhã; após as 19h, com a tarde. Se houver só um médico diurno, ele assume ambos os períodos. A escala manual anterior não participa mais da atribuição.')
        if len(otavios) > 1:
            selected = st.selectbox('Qual Otávio é o responsável fixo da segunda?', [None]+list(otavios),
                                    format_func=lambda x: 'Selecione' if x is None else otavios[x], key='tracker_otavio')
            monday_doctor = otavios.get(selected)
        if monday_doctor:
            st.caption(f'Responsável fixo de segunda: {monday_doctor}.')
    if not monday_doctor:
        st.warning('Identifique Otávio no cadastro ativo da equipe. Os eletivos de segunda ficarão para conferir até essa identificação.')
    with st.expander('📥 Importar lista de exames pendentes', expanded=True):
        upload = st.file_uploader('Exportação completa dos pendentes', type=['zip','xls','xlsx','csv'], key='tracker_upload')
        st.caption('Cada importação substitui a lista anterior de TC/RX. A ausência de um exame na nova lista não comprova que foi laudado.')
        if upload:
            try:
                records, excluded = prepare_records(read_upload(upload.name, upload.getvalue()))
            except Exception as exc:
                st.error(f'Não foi possível ler a exportação: {exc}')
                return
            st.write(f'{len(records)} linhas de TC/RX · {excluded} linhas de outras modalidades excluídas.')
            consent = st.checkbox('Confirmo que esta é a lista completa de exames pendentes para substituir a anterior.', key='tracker_import_confirm')
            if st.button('Confirmar importação', disabled=not consent or not records, key='tracker_import'):
                execute_query('''INSERT INTO exam_tracker_snapshot(id,payload,source_name,imported_at)
                    VALUES(1,%s,%s,NOW()) ON CONFLICT(id) DO UPDATE
                    SET payload=EXCLUDED.payload,source_name=EXCLUDED.source_name,imported_at=NOW()''',
                              (json.dumps(records, ensure_ascii=False), upload.name))
                st.success('Lista importada e salva no banco.')
    snapshot = fetch_data('SELECT payload,source_name,imported_at FROM exam_tracker_snapshot WHERE id=1')
    if snapshot.empty:
        st.info('Importe a exportação de pendentes. Os responsáveis serão identificados pela escala de plantões e pela regra semanal.')
        return
    records = json.loads(snapshot.iloc[0]['payload'])
    imported = pd.Timestamp(snapshot.iloc[0]['imported_at']).tz_convert(TZ)
    st.caption(f"Lista: {snapshot.iloc[0]['source_name']} · importada em {imported:%d/%m/%Y %H:%M}.")
    st.button('🔄 Atualizar prazos e responsáveis', key='tracker_refresh')
    st.caption('Os prazos são recalculados a cada interação ou atualização. Novos laudos exigem uma nova exportação de pendentes.')
    performed = [parse_performed(r['realizacao']) for r in records]
    valid_days = [v.date() for v in performed if v]
    shifts = {}
    if valid_days:
        reference_days = [reporting_slot(v)[0] for v in performed if v]
        start = min(valid_days)-dt.timedelta(days=1)
        end = max(valid_days + reference_days)
        data = fetch_data('''SELECT s.shift_date,s.shift_time,COALESCE(d.name,s.doctor_name) AS name
            FROM shift_schedule s LEFT JOIN doctors d ON d.id=s.doctor_id
            WHERE s.shift_date BETWEEN %s AND %s''', (start,end))
        shifts = {(pd.Timestamp(r['shift_date']).date(),r['shift_time']): r['name'] for _,r in data.iterrows()}
    now = dt.datetime.now(TZ)
    result = build_tracker(records, shifts, monday_doctor, now)
    if result.empty:
        st.info('A lista não contém exames de TC/RX.')
        return
    st.caption(f'Prazos calculados em {now:%d/%m/%Y %H:%M:%S}. Contagens por linha da exportação; identificadores repetidos são preservados.')
    a,b,c,d = st.columns(4)
    a.metric('Linhas de TC/RX', len(result))
    b.metric('Atrasados atribuídos', int((result['Situação']=='Atrasado').sum()))
    c.metric('No prazo atribuídos', int((result['Situação']=='No prazo').sum()))
    d.metric('Para conferir', int((result['Situação']=='Conferir').sum()))
    choices = sorted(result['Médico'].unique())
    chosen = st.selectbox('Filtrar por médico', ['Todos']+choices, key='tracker_doctor_filter')
    view = result if chosen=='Todos' else result[result['Médico']==chosen]
    status = st.multiselect('Situação', ['Atrasado','No prazo','Conferir'], default=['Atrasado','No prazo','Conferir'])
    view = view[view['Situação'].isin(status)].drop(columns='_segundos')
    st.dataframe(view, hide_index=True, use_container_width=True)
    st.download_button('⬇️ Baixar relatório filtrado (CSV)', view.to_csv(index=False).encode('utf-8-sig'),
                       file_name='tracker_exames.csv', mime='text/csv')
    st.download_button('⬇️ Backup da lista e referências de escala (JSON)',
                       json.dumps({'records':records,'source_name':str(snapshot.iloc[0]['source_name']),
                                   'imported_at':str(imported),
                                   'reporting_rule':'Seg Otávio; ter–qui dia seguinte; sex segunda seguinte; sáb próprio dia',
                                   'monday_doctor':monday_doctor,
                                   'shift_references':[{'date':day.isoformat(),'period':period,'doctor':doctor}
                                                       for (day,period),doctor in shifts.items()]},
                                  ensure_ascii=False, default=str, indent=2),
                       file_name='backup_tracker_exames.json', mime='application/json')
    with st.expander('📲 Preparar cobrança no WhatsApp'):
        eligible = result[result['Situação']!='Conferir']
        if eligible.empty:
            st.info('Nenhum exame atribuído sem pendências de conferência.')
            return
        recipient = st.selectbox('Médico destinatário', sorted(eligible['Médico'].unique()), key='tracker_whatsapp_doctor')
        only_late = st.checkbox('Incluir somente atrasados', value=True)
        group = eligible[eligible['Médico']==recipient]
        if only_late:
            group = group[group['Situação']=='Atrasado']
        message = build_whatsapp_message(recipient, group, now)
        text = st.text_area('Mensagem para conferir antes de abrir o WhatsApp', value=message, height=220)
        st.download_button('Baixar mensagem (TXT)', text, file_name='mensagem_pendencias.txt', mime='text/plain')
        phone = st.text_input('WhatsApp do médico com código do país e DDD', placeholder='55…', key='tracker_whatsapp_phone')
        digits = re.sub(r'\D','',phone)
        if group.empty:
            st.info('Não há exames para os filtros desta mensagem.')
        elif 10 <= len(digits) <= 15:
            st.link_button('Abrir mensagem no WhatsApp', 'https://wa.me/'+digits+'?'+urlencode({'text':text}))
            st.caption('Confira o destinatário e envie no WhatsApp. Abrir a mensagem não registra entrega.')
        elif phone:
            st.warning('Informe um telefone válido com código do país e DDD.')
