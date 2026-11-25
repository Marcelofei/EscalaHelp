import datetime as dt
import sys
import types
import unittest
import io
import zipfile
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
        for value in ['MG','MG\\OT','OT\\MG','MMG','mamografia']:
            self.assertEqual(t.modality_group(value),'MMG')
        self.assertIn('conferir',t.modality_group('MG\\DX'))
        for value in ['OT\\MR','MR','TE','OT','US\\SR','MAPA','XA\\OT']:
            self.assertIsNone(t.modality_group(value))
    def test_priority_only(self):
        raw=pd.DataFrame([{'Prioridade':'','Nome do paciente':'teste','Data do exame':'25/11/2026 10:00','Modalidade':'CT','Descrição do exame':'TC','Convênio':'Oncologia'}]*2)
        records,excluded=t.prepare_records(raw)
        self.assertEqual(len(records),2)
        self.assertEqual(excluded,0)
        self.assertEqual(records[0]['prioridade'],'')
        self.assertEqual(t.RULES[t.normalize(records[0]['prioridade'])][1],120)
    def test_whatsapp_counts_by_priority_day_and_modality_without_patient_names(self):
        exams = pd.DataFrame([
            {'Paciente':'Paciente B','Exame':'RX tórax','Modalidade':'RX','Realização':'26/10/2026 10:00','Prioridade':'Pronto Atendimento','Accession Nº':'ACESSO-SECRETO'},
            {'Paciente':'Paciente A','Exame':'TC abdome total','Modalidade':'TC','Realização':'25/10/2026 10:00','Prioridade':'Oncologia'},
            {'Paciente':'Paciente C','Exame':'TC crânio','Modalidade':'TC','Realização':'25/10/2026 11:00','Prioridade':'Sem prioridade'},
            {'Paciente':'Paciente D','Exame':'RX coluna','Modalidade':'RX','Realização':'25/10/2026 11:00','Prioridade':''},
        ])
        message=t.build_whatsapp_message('Iago',exams,self.moment('13:58'))
        self.assertEqual(message, 'Bom dia Dr(a). Iago,\n\n'
            'Essas são suas pendências de laudo TC/RX/MMG do Hospital HELP — atualização de 25/11/2026 13:58.\n'
            'Segue a relação e detalhamento dos exames para liberação:\n\n'
            'PRONTO ATENDIMENTO (Prioridade)\n26/10 - RX - 01\n\n'
            'URGENTE (Prioridade)\nSem pendências.\n\n'
            'INTERNOS (Prioridade)\nSem pendências.\n\n'
            'HOSP TRAUMA (Prioridade)\nSem pendências.\n\n'
            'ELETIVOS\n25/10 - TC - 02\n25/10 - RX - 01')
        self.assertNotIn('ACESSO',message)
        self.assertNotIn('Paciente',message)
        self.assertNotIn('abdome',message)
        self.assertNotIn('Oncologia',message)

    def test_whatsapp_priority_sections_and_date_order(self):
        exams=pd.DataFrame([
            {'Prioridade':priority,'Realização':day+'/11/2026 10:00','Modalidade':'TC'}
            for priority,day in [('Urgente','25'),('Internado','24'),('Internação','24'),
                                 ('Hosp Trauma','23'),('Hospital de Trauma','23'),
                                 ('Pronto Atendimento','22'),('Pronto Atendimento','21')]])
        message=t.build_whatsapp_message('Iago',exams,self.moment('13:58'))
        self.assertIn('URGENTE (Prioridade)\n25/11 - TC - 01',message)
        self.assertIn('INTERNOS (Prioridade)\n24/11 - TC - 02',message)
        self.assertIn('HOSP TRAUMA (Prioridade)\n23/11 - TC - 02',message)
        self.assertLess(message.index('21/11'),message.index('22/11'))
        self.assertTrue(message.endswith('ELETIVOS\nSem pendências.'))

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

    def csv_file(self, records):
        return pd.DataFrame(records).to_csv(index=False).encode('utf-8')

    def source_record(self, **changes):
        record={'ID':'1','Accession Nº':'A1','Prioridade':'','Nome do paciente':'Paciente teste',
                'Data do exame':'23/11/2026 10:00','Modalidade':'MG\\OT','Descrição do exame':'Mamografia'}
        record.update(changes)
        return record

    def test_multi_file_union_preserves_within_file_rows_and_merges_cross_file_copies(self):
        first=self.source_record()
        second=self.source_record(**{'ID':'2','Accession Nº':'A2','Modalidade':'CT'})
        records,excluded,duplicates,sheets=t.merge_uploads([
            ('a.csv',self.csv_file([first,first])),
            ('b.csv',self.csv_file([first,second])),
        ])
        self.assertEqual((len(records),excluded,duplicates,sheets),(3,0,1,2))
        self.assertEqual([r['modalidade'] for r in records],['MMG','MMG','TC'])
        self.assertEqual([o['arquivo'] for o in records[0]['origens']],['a.csv','b.csv'])
        out=t.build_tracker(records,{},'Otávio',self.moment('13:58'))
        self.assertTrue((out['Médico']=='Otávio').all())
        message=t.build_whatsapp_message('Otávio',out,self.moment('13:58'))
        self.assertIn('23/11 - MMG - 02',message)
        self.assertIn('23/11 - TC - 01',message)
        self.assertNotIn('Paciente teste',message)

    def test_multiple_spreadsheets_inside_zip_and_standalone_file(self):
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('a.csv',self.csv_file([self.source_record()]))
            z.writestr('b.csv',self.csv_file([self.source_record(**{'ID':'2'})]))
            z.writestr('__MACOSX/._ignored.csv','ignored')
        records,excluded,duplicates,sheets=t.merge_uploads([
            ('exames.zip',archive.getvalue()),
            ('extra.csv',self.csv_file([self.source_record(**{'ID':'3','Modalidade':'DX'}),
                                       self.source_record(**{'ID':'4','Modalidade':'MR'})])),
        ])
        self.assertEqual((len(records),excluded,duplicates,sheets),(3,1,0,3))
        self.assertEqual(records[0]['origens'][0]['arquivo'],'exames.zip / a.csv')

    def test_missing_identifiers_do_not_merge_and_invalid_source_rejects_batch(self):
        row=self.source_record(**{'ID':'','Accession Nº':''})
        records,_,duplicates,_=t.merge_uploads([('a.csv',self.csv_file([row])),('b.csv',self.csv_file([row]))])
        self.assertEqual((len(records),duplicates),(2,0))
        with self.assertRaisesRegex(ValueError,'invalido.csv'):
            t.merge_uploads([('a.csv',self.csv_file([row])),('invalido.csv',b'coluna,outra\n1,2\n')])

    def test_same_identifier_different_exam_is_preserved(self):
        row=self.source_record()
        records,_,duplicates,_=t.merge_uploads([
            ('a.csv',self.csv_file([row])),
            ('b.csv',self.csv_file([self.source_record(**{'Descrição do exame':'Outro exame'})])),
        ])
        self.assertEqual((len(records),duplicates),(2,0))

if __name__=='__main__':
    unittest.main()
