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
            ('02:00','laudos',(day,'Laudos')),
        ]:
            with self.subTest(time=time,kind=kind):
                self.assertEqual(t.assignment_slot(self.moment(time),kind),expected)
        self.assertEqual(t.assignment_slot(dt.datetime(2026,12,1,2,tzinfo=t.TZ),'plantao'),(dt.date(2026,11,30),'Noite'))
    def record(self,priority='',time='25/11/2026 10:00',**extra):
        return dict(linha=2,id='1',acesso='A',paciente='Paciente teste',exame='TC teste',modalidade='TC',prioridade=priority,realizacao=time,**extra)
    def test_deadlines(self):
        day=dt.date(2026,11,25)
        shifts={(day,'Manhã'):'Médico manhã',(day,'Tarde'):'Médico tarde',(day,'Noite'):'Médico noite'}
        daily={day:'Médico laudos'}
        for priority,hours in [('Hosp Trauma',2),('Pronto Atendimento',2),('Urgente',6),('Internado',6),('Oncologia',96),('',120)]:
            with self.subTest(priority=priority):
                out=t.build_tracker([self.record(priority)],shifts,daily,self.moment('11:00')).iloc[0]
                expected=self.moment('10:00')+dt.timedelta(hours=hours)
                self.assertEqual(out['Vencimento'],expected.strftime('%d/%m/%Y %H:%M'))
                self.assertEqual(out['Médico'],'Médico laudos' if priority in ('','Oncologia') else 'Médico manhã')
                self.assertEqual(out['Situação'],'No prazo')
        out=t.build_tracker([self.record('Internado','25/11/2026 13:00')],shifts,daily,self.moment('14:00')).iloc[0]
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
if __name__=='__main__':
    unittest.main()
