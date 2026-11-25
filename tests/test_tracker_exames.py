import datetime as dt
import sys
import types
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules['streamlit'] = types.ModuleType('streamlit')
import tracker_exames as t
import pandas as pd

class RulesTests(unittest.TestCase):
    def moment(self, time):
        return dt.datetime.fromisoformat('2026-11-25T'+time).replace(tzinfo=t.TZ)
    def test_boundaries(self):
        day=dt.date(2026,11,25)
        for time,kind,expected in [
            ('00:00','plantao',(day-dt.timedelta(days=1),'Noite')),
            ('06:44','plantao',(day-dt.timedelta(days=1),'Noite')),
            ('06:45','plantao',(day,'Manhã')),
            ('12:59','plantao',(day,'Manhã')),
            ('13:00','plantao',(day,'Tarde')),
            ('18:44','plantao',(day,'Tarde')),
            ('18:45','plantao',(day,'Noite')),
            ('23:59','plantao',(day,'Noite')),
            ('02:00','internado',(day-dt.timedelta(days=1),'Noite')),
            ('06:45','internado',(day,'Manhã')),
            ('12:59','internado',(day,'Manhã')),
            ('13:00','internado',(day,'Noite')),
            ('18:44','internado',(day,'Noite')),
            ('23:59','internado',(day,'Noite')),
            ('02:00','laudos',(day+dt.timedelta(days=1),'Manhã')),
        ]:
            with self.subTest(time=time,kind=kind):
                self.assertEqual(t.assignment_slot(self.moment(time),kind),expected)
        self.assertEqual(t.assignment_slot(dt.datetime(2026,12,1,2,tzinfo=t.TZ),'plantao'),(dt.date(2026,11,30),'Noite'))
    def record(self,priority='',time='25/11/2026 10:00',**extra):
        return dict(linha=2,id='1',acesso='A',paciente='Paciente teste',exame='TC teste',modalidade='TC',prioridade=priority,realizacao=time,**extra)
    def test_deadlines(self):
        day=dt.date(2026,11,25)
        shifts={(day,'Manhã'):'Médico manhã',(day,'Tarde'):'Médico tarde',(day,'Noite'):'Médico noite'}
        shifts[(day+dt.timedelta(days=1),'Manhã')]='Médico laudos'
        for priority,hours in [('Hosp Trauma',2),('Pronto Atendimento',2),('Urgente',6),('Internado',6),('Oncologia',96),('',120)]:
            with self.subTest(priority=priority):
                out=t.build_tracker([self.record(priority)],shifts,'Otávio',self.moment('11:00')).iloc[0]
                expected=self.moment('10:00')+dt.timedelta(hours=hours)
                self.assertEqual(out['Vencimento'],expected.strftime('%d/%m/%Y %H:%M'))
                self.assertEqual(out['Médico'],'Médico laudos' if priority in ('','Oncologia') else 'Médico manhã')
                self.assertEqual(out['Situação'],'No prazo')
        out=t.build_tracker([self.record('Internado','25/11/2026 13:00')],shifts,'Otávio',self.moment('14:00')).iloc[0]
        self.assertEqual(out['Médico'],'Médico noite')
    def test_late_missing_and_unknown(self):
        day=dt.date(2026,11,25)
        shifts={(day,'Manhã'):'Médico'}
        out=t.build_tracker([self.record('Hosp Trauma')],shifts,{},self.moment('13:00')).iloc[0]
        self.assertEqual(out['Prazo'],'Atrasado há 1h 00min')
        self.assertEqual(out['Situação'],'Atrasado')
        for record in [self.record('Desconhecida'),self.record('Hosp Trauma', '25/11/2026'),self.record('Hosp Trauma','25/11/2026 23:00')]:
            self.assertEqual(t.build_tracker([record],shifts,{},self.moment('13:00')).iloc[0]['Situação'],'Conferir')
        self.assertEqual(t.build_tracker([self.record('Hosp Trauma')],{},{},self.moment('13:00')).iloc[0]['Situação'],'Conferir')
    def test_modalities(self):
        for value in ['CT','CT\\OT','TC']:
            self.assertEqual(t.modality_group(value),'TC')
        for value in ['DX','OT\\CR','RX','DO\\OT\\CR']:
            self.assertEqual(t.modality_group(value),'RX')
        for value in ['OT\\MR','MR','TE','OT','US\\SR','MG\\OT','MAPA','XA\\OT']:
            self.assertIsNone(t.modality_group(value))
    def test_priority_only(self):
        raw=pd.DataFrame([{'Prioridade':'','Nome do paciente':'teste','Data do exame':'25/11/2026 10:00','Modalidade':'CT','Descrição do exame':'TC','Convênio':'Oncologia'}]*2)
        records,excluded=t.prepare_records(raw)
        self.assertEqual(len(records),2)
        self.assertEqual(excluded,0)
        self.assertEqual(records[0]['prioridade'],'')
        self.assertEqual(t.RULES[t.normalize(records[0]['prioridade'])][1],120)
    def test_whatsapp_groups_dates_modalities_and_patient_names(self):
        exams = pd.DataFrame([
            {'Paciente':'Paciente B','Exame':'RX tórax','Modalidade':'RX','Realização':'26/10/2026 10:00','_segundos':-7200,'Accession Nº':'ACESSO-SECRETO'},
            {'Paciente':'Paciente A','Exame':'TC abdome total','Modalidade':'TC','Realização':'25/10/2026 10:00','_segundos':-(5*86400+7*3600),'Accession Nº':'OUTRO-ACESSO'},
            {'Paciente':'Paciente C','Exame':'RX coluna','Modalidade':'RX','Realização':'25/10/2026 11:00','_segundos':-86400},
        ])
        message=t.build_whatsapp_message('Iago',exams,self.moment('13:58'))
        self.assertEqual(message, 'Olá, Iago.\n\n'
            'Essas são suas pendências de laudo TC/RX do Hospital HELP — atualização de 25/11/2026 13:58.\n\n'
            '25/10\n\n- TC:\n  - Paciente A // TC abdome total // em atraso há 5 dias\n\n'
            '- RX:\n  - Paciente C // RX coluna // em atraso há 1 dia\n\n'
            '26/10\n\n- RX:\n  - Paciente B // RX tórax // em atraso há 2h 00min')
        self.assertNotIn('ACESSO',message)
        self.assertNotIn('7h',message)

    def test_whatsapp_deadline_boundaries(self):
        self.assertEqual(t.whatsapp_deadline(-86399),'em atraso há 23h 59min')
        self.assertEqual(t.whatsapp_deadline(-86400),'em atraso há 1 dia')
        self.assertEqual(t.whatsapp_deadline(-172800),'em atraso há 2 dias')
        self.assertEqual(t.whatsapp_deadline(3600),'faltam 1h 00min')
        self.assertEqual(t.whatsapp_deadline(0),'vence agora')

    def test_reporting_weekday_reference_and_month_year_boundaries(self):
        for source,reference,slot in [
            ('2026-11-23','2026-11-23','Otávio'),
            ('2026-11-24','2026-11-25','Manhã'),
            ('2026-11-25','2026-11-26','Manhã'),
            ('2026-11-26','2026-11-27','Manhã'),
            ('2026-11-27','2026-11-30','Manhã'),
            ('2026-11-28','2026-11-28','Manhã'),
            ('2026-11-29','2026-11-29','Domingo — conferir'),
            ('2026-10-30','2026-11-02','Manhã'),
            ('2027-12-31','2028-01-03','Manhã'),
        ]:
            with self.subTest(source=source):
                moment=dt.datetime.fromisoformat(source+'T10:00').replace(tzinfo=t.TZ)
                self.assertEqual(t.reporting_slot(moment),(dt.date.fromisoformat(reference),slot))

    def test_reporting_split_morning_afternoon_and_exceptional_hours(self):
        reference=dt.date(2026,11,26)
        shifts={(reference,'Manhã'):'Médico A',(reference,'Tarde'):'Médico B',(reference,'Noite'):'Noturno'}
        for clock,expected in [('02:00','Médico A'),('06:59','Médico A'),('07:00','Médico A'),('12:59','Médico A'),('13:00','Médico B'),('18:59','Médico B'),('19:00','Médico B'),('23:59','Médico B')]:
            with self.subTest(clock=clock):
                self.assertEqual(t.reporting_owner(self.moment(clock),shifts,'Otávio')[0],expected)
        single={(reference,'Manhã'):'Único'}
        self.assertEqual(t.reporting_owner(self.moment('14:00'),single,'Otávio')[0],'Único')
        self.assertIsNone(t.reporting_owner(self.moment('14:00'),{},'Otávio')[0])

    def test_reporting_monday_fixed_sunday_flag_and_deadline_unchanged(self):
        now=dt.datetime(2026,12,1,10,tzinfo=t.TZ)
        for priority,hours in [('Oncologia',96),('',120)]:
            record=self.record(priority,'23/11/2026 14:00')
            out=t.build_tracker([record],{},'Otávio',now).iloc[0]
            self.assertEqual(out['Médico'],'Otávio')
            self.assertEqual(out['Vencimento'],(dt.datetime(2026,11,23,14)+dt.timedelta(hours=hours)).strftime('%d/%m/%Y %H:%M'))
            self.assertEqual(t.build_tracker([record],{},None,now).iloc[0]['Situação'],'Conferir')
            record=self.record(priority,'29/11/2026 10:00')
            out=t.build_tracker([record],{},'Otávio',now).iloc[0]
            self.assertEqual(out['Situação'],'Conferir')
            self.assertIn('domingo',out['Conferência'])

if __name__=='__main__':
    unittest.main()
