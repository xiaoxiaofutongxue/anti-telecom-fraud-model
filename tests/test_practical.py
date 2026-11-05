"""单机实证准备回归。全部为临时合成数据，不使用真实案件或用户核验库。"""
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock,patch
import csv,hashlib,json,shutil,tempfile,threading,unittest,zipfile
import numpy as np
from openpyxl import Workbook
from fourflow.core import load_dataset,build_case_graph,parse_time
from fourflow.advanced_data import create_advanced_dataset
from fourflow.intake import read_table,suggest_columns,import_tables
from fourflow.cleaning import clean_dataset
from fourflow.io_utils import atomic_json,point_model
from fourflow.validation_groups import FIELDS,check_group_splits,group_tokens
from fourflow.hd_eia import prepare
from fourflow.dual_model import train_joint,DualFlowModel
from fourflow.empirical import evaluate_external,criteria_template,assess_criteria,record_review,wilson,model_status
from fourflow.operations import backup_workspace,verify_backup,restore_backup
from fourflow.orchestrator import run_analysis,Cancelled
from fourflow.windows import plan_windows
from fourflow.store import Store
from fourflow.service import analysis_graph
from fourflow.dataset_config import configure_copy,group_template

ROOT=Path(__file__).resolve().parents[1]


class PracticalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='practical_',dir=ROOT/'runtime');cls.root=Path(cls.temp.name);cls.data=load_dataset(create_advanced_dataset(cls.root/'data',12,1551,'PRACT15'));cls.cid=cls.data.tables['cases'][0]['case_id'];cls.graph=build_case_graph(cls.data,cls.cid)
        path=cls.root/'structured';shutil.copytree(cls.data.root,path);(path/'multimodal_links.json').write_text('[]',encoding='utf-8');manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'));manifest['advanced']['sha256']['multimodal_links.json']=hashlib.sha256((path/'multimodal_links.json').read_bytes()).hexdigest();atomic_json(path/'manifest.json',manifest)
        cls.structured=load_dataset(path);cls.model=train_joint(cls.structured,cls.root/'joint_model',3,seed=1551)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def clone(self,name):
        path=self.root/name;shutil.copytree(self.data.root,path);return path
    def csv_source(self,name,text,encoding='utf-8-sig'):
        path=self.root/name;path.write_text(text,encoding=encoding);return path
    def entry(self,path,table='funds'):
        snapshot=read_table(path);return {'path':str(path),'table':table,'sha256':snapshot['sha256'],'columns':suggest_columns(table,snapshot['headers']),'defaults':{'chain':'合成银行域','from_kind':'account','to_kind':'account'} if table=='funds' else {}}
    def meta(self):return {'case_id':'IMP1','title':'合成接入测试','region':'合成区域','case_type':'合成案型','origin':'synthetic'}
    def add_groups(self,path,same=False):
        data=load_dataset(path)
        with (path/'evaluation_groups.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(FIELDS)
            for case in data.tables['cases']:writer.writerow([case['case_id'],'SAME' if same else 'GROUP_'+case['case_id'],'case_group','true','S001'])
        manifest=dict(data.manifest);manifest.update(evaluation_groups='evaluation_groups.csv',evaluation_groups_sha256=hashlib.sha256((path/'evaluation_groups.csv').read_bytes()).hexdigest());atomic_json(path/'manifest.json',manifest);return load_dataset(path)
    def test_csv_chinese_mapping_leading_zeros_and_intake_lineage(self):
        path=self.csv_source('leading.csv','流水号,交易时间,付款账号,收款账号,金额,币种\n0001,2026-01-01 08:00,000123,000456,100.01,CNY\n');entry=self.entry(path);result=import_tables([entry],self.root/'leading_out',self.meta());self.assertTrue(result['valid']);data=load_dataset(result['output']);row=data.tables['funds'][0]
        self.assertEqual(row['from_id'],'000123');self.assertEqual(row['tx_id'],'0001');self.assertEqual(row['_original_line'],2);self.assertEqual(row['_original_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
    def test_gb18030_csv_is_supported(self):
        path=self.csv_source('gb.csv','流水号,交易时间,付款账号,收款账号,金额,币种\n001,2026-01-01 08:00,A,B,1,CNY\n','gb18030');self.assertEqual(read_table(path)['encoding'],'gb18030')
    def test_queue_cannot_mix_real_and_synthetic(self):
        path=self.csv_source('mixed.csv','流水号,交易时间,付款账号,收款账号,金额,币种\n001,2026-01-01 08:00,A,B,1,CNY\n');entry={**self.entry(path),'origin':'real'}
        with self.assertRaisesRegex(ValueError,'混合'):import_tables([entry],self.root/'mixed_out',self.meta())
    def test_queue_preserves_each_case_metadata(self):
        path=self.csv_source('two_cases.csv','流水号,交易时间,付款账号,收款账号,金额,币种\n001,2026-01-01 08:00,A,B,1,CNY\n');one={**self.entry(path),'case_metadata':{**self.meta(),'case_id':'A','title':'合成甲','region':'合成甲区'}};two={**self.entry(path),'case_metadata':{**self.meta(),'case_id':'B','title':'合成乙','region':'合成乙区'}}
        result=import_tables([one,two],self.root/'two_cases_out',self.meta());self.assertTrue(result['valid']);cases={r['case_id']:r for r in load_dataset(result['output']).tables['cases']};self.assertEqual(cases['A']['region'],'合成甲区');self.assertEqual(cases['B']['region'],'合成乙区')
    def test_intake_numeric_amount_retains_python_float_representation(self):
        from fourflow.intake import _cell
        value=123456.12345678912;self.assertEqual(_cell(value)['value'],str(value))
    def test_xlsx_formulas_are_not_used_as_values(self):
        book=Workbook();sheet=book.active;sheet.append(['流水号','交易时间','付款账号','收款账号','金额','币种']);sheet.append(['001','2026-01-01 08:00','A','B','=1+1','CNY']);path=self.root/'formula.xlsx';book.save(path)
        result=import_tables([self.entry(path)],self.root/'formula_out',self.meta());self.assertFalse(result['valid']);self.assertIn('公式',result['errors'][0]['reason'])
    def test_xlsx_numeric_identifier_does_not_silently_lose_precision(self):
        book=Workbook();sheet=book.active;sheet.append(['流水号','交易时间','付款账号','收款账号','金额','币种']);sheet.append(['001','2026-01-01 08:00',12345678901234567,'B',1,'CNY']);path=self.root/'numeric.xlsx';book.save(path)
        result=import_tables([self.entry(path)],self.root/'numeric_out',self.meta());self.assertFalse(result['valid']);self.assertIn('精度',result['errors'][0]['reason'])
    def test_xlsx_explicit_zero_padding_is_preserved(self):
        book=Workbook();sheet=book.active;sheet.append(['流水号','交易时间','付款账号','收款账号','金额','币种']);sheet.append(['001','2026-01-01 08:00',123,'B',1,'CNY']);sheet['C2'].number_format='000000';path=self.root/'padded.xlsx';book.save(path)
        result=import_tables([self.entry(path)],self.root/'padded_out',self.meta());self.assertTrue(result['valid']);row=load_dataset(result['output']).tables['funds'][0];self.assertEqual(row['from_id'],'000123');self.assertEqual(row['_original_sheet'],'Sheet')
    def test_long_numeric_identifier_is_rejected_even_with_padding(self):
        book=Workbook();sheet=book.active;sheet.append(['流水号','交易时间','付款账号','收款账号','金额','币种']);sheet.append(['001','2026-01-01 08:00',12345678901234567,'B',1,'CNY']);sheet['C2'].number_format='00000000000000000000';path=self.root/'long_padded.xlsx';book.save(path)
        result=import_tables([self.entry(path)],self.root/'long_padded_out',self.meta());self.assertFalse(result['valid']);self.assertIn('精度',result['errors'][0]['reason'])
    def test_input_change_after_preview_is_rejected(self):
        path=self.csv_source('change.csv','流水号,金额\nA,1\n');entry=self.entry(path);path.write_text('流水号,金额\nA,2\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'预览后变化'):import_tables([entry],self.root/'changed_out',self.meta())
    def test_duplicate_headers_rejected(self):
        path=self.csv_source('duplicate.csv','A,A\n1,2\n')
        with self.assertRaisesRegex(ValueError,'表头'):read_table(path)
    def test_intake_does_not_invent_actor_identity_or_role(self):
        path=self.csv_source('people.csv','通信编号,发送主体,接收主体,时间,渠道,摘要\nM1,A1,A2,2026-01-01 08:00,合成,待核对\n');result=import_tables([self.entry(path,'communications')],self.root/'people_out',self.meta());self.assertTrue(result['valid']);data=load_dataset(result['output'])
        self.assertEqual(len(data.tables['actors']),2);self.assertTrue(all(r['actor_type']=='unspecified' and not r['role'] and r['role_verified']=='false' for r in data.tables['actors']))
    def test_cleaning_intake_preserves_original_hash_and_sheet(self):
        path=self.csv_source('clean.csv','流水号,交易时间,付款账号,收款账号,金额,币种\nA,2026-01-01 08:00,A,B,1,CNY\n');result=import_tables([self.entry(path)],self.root/'clean_intake',self.meta());report=clean_dataset(result['output'],self.root/'clean_second');self.assertTrue(report['valid']);self.assertEqual(load_dataset(self.root/'clean_second').tables['funds'][0]['_original_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
    def test_tampered_intake_table_is_not_silently_trusted(self):
        path=self.csv_source('tamper.csv','流水号,交易时间,付款账号,收款账号,金额,币种\nA,2026-01-01 08:00,A,B,1,CNY\n');result=import_tables([self.entry(path)],self.root/'tamper_out',self.meta());output=Path(result['output'])/'funds.csv';output.write_bytes(output.read_bytes().replace(b',1,CNY',b',2,CNY'))
        with self.assertRaisesRegex(ValueError,'规范表变化'):load_dataset(output.parent)
    def test_intake_original_material_change_blocks_verified_lineage(self):
        path=self.csv_source('original_tamper.csv','流水号,交易时间,付款账号,收款账号,金额,币种\nA,2026-01-01 08:00,A,B,1,CNY\n');result=import_tables([self.entry(path)],self.root/'original_tamper',self.meta());material=next((Path(result['output'])/'_ingest_parent').iterdir());material.write_bytes(material.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'原件'):load_dataset(result['output'])
    def test_cleaned_intake_original_change_is_also_rejected(self):
        path=self.csv_source('clean_original.csv','流水号,交易时间,付款账号,收款账号,金额,币种\nA,2026-01-01 08:00,A,B,1,CNY\n');result=import_tables([self.entry(path)],self.root/'clean_original_intake',self.meta());target=self.root/'clean_original_result';self.assertTrue(clean_dataset(result['output'],target)['valid']);material=next((target/'_ingest_parent').iterdir());material.write_bytes(material.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'原件'):load_dataset(target)
    def test_csv_multiline_cells_preserve_physical_source_line(self):
        path=self.csv_source('multiline.csv','通信编号,时间,发送主体,接收主体,渠道,摘要\nM1,2026-01-01 08:00,A,B,合成,"第一行\n第二行"\nM2,2026-01-01 08:01,A,B,合成,第三行\n');snapshot=read_table(path);self.assertEqual([r['line'] for r in snapshot['rows']],[2,4]);result=import_tables([self.entry(path,'communications')],self.root/'multiline_result',self.meta());self.assertTrue(result['valid']);self.assertEqual(load_dataset(result['output']).tables['communications'][1]['_original_line'],4)
    def test_repeated_cleaning_keeps_intake_raw_file_verification(self):
        path=self.csv_source('repeat.csv','流水号,交易时间,付款账号,收款账号,金额,币种\nA,2026-01-01 08:00,A,B,1,CNY\n');result=import_tables([self.entry(path)],self.root/'repeat_input',self.meta());first=self.root/'repeat_first';second=self.root/'repeat_second';self.assertTrue(clean_dataset(result['output'],first)['valid']);self.assertTrue(clean_dataset(first,second)['valid']);material=next((second/'_ingest_parent').iterdir());material.write_bytes(material.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'原件'):load_dataset(second)
    def test_reviewed_material_import_after_intake_keeps_provenance(self):
        from fourflow.multimodal import LocalMediaProcessor,draft_communication
        path=self.csv_source('review_intake.csv','通信编号,时间,发送主体,接收主体,渠道,摘要\nM1,2026-01-01 08:00,A,B,合成,原记录\n');result=import_tables([self.entry(path,'communications')],self.root/'review_intake',self.meta());data=load_dataset(result['output']);note=self.root/'review_text.txt';note.write_text('合成核对材料',encoding='utf-8');extraction=LocalMediaProcessor(ROOT).extract(note,'synthetic');output=draft_communication(data,extraction,self.root/'reviewed_after_intake','IMP1','A','B','2026-01-01 08:02','合成核验人','人工核对摘要');loaded=load_dataset(output)
        self.assertEqual(len(loaded.tables['communications']),2);self.assertEqual(loaded.tables['communications'][0]['_original_sha256'],data.tables['communications'][0]['_original_sha256']);material=next((output/'_ingest_parent').iterdir());material.write_bytes(material.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'原件'):load_dataset(output)
    def test_material_import_keeps_advanced_group_and_task_configuration(self):
        from fourflow.multimodal import LocalMediaProcessor,draft_communication
        from fourflow.hd_eia import auxiliary
        group=group_template(self.data);group='\n'.join(line.replace(',false,',',true,').replace(',,case_group,',',ADV'+str(i)+',case_group,') if i else line for i,line in enumerate(group.splitlines()));configured=configure_copy(self.data,self.root/'advanced_configured',{'label_definition':self.data.manifest['label_definition'],'role_task':self.data.manifest['role_task']},group,'合成核验人','测试留存');note=self.root/'advanced_note.txt';note.write_text('合成追加材料',encoding='utf-8');extraction=LocalMediaProcessor(ROOT).extract(note,'synthetic');people=[r['actor_id'] for r in configured.rows('actors',self.cid)];output=draft_communication(configured,extraction,self.root/'advanced_reviewed',self.cid,people[0],people[1],'2026-01-01 12:00','合成核验人','人工核对摘要');loaded=load_dataset(output)
        self.assertEqual(len(group_tokens(loaded)),12);self.assertTrue(auxiliary(loaded)['media']);self.assertTrue((output/'configuration_record.json').is_file())
    def test_real_training_requires_declared_groups(self):
        import copy
        data=copy.deepcopy(self.data);data.manifest['origin']='real'
        with self.assertRaisesRegex(ValueError,'分组'):check_group_splits(data,{'train':[self.cid]})
    def test_related_group_crossing_split_is_rejected(self):
        data=self.add_groups(self.clone('same_group'),same=True);ids=[r['case_id'] for r in data.tables['cases']]
        with self.assertRaisesRegex(ValueError,'跨训练'):check_group_splits(data,{'train':ids[:6],'test':ids[6:]})
    def test_group_file_hash_tamper_is_rejected(self):
        path=self.clone('group_tamper');self.add_groups(path);(path/'evaluation_groups.csv').write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'分组文件指纹'):load_dataset(path)
    def test_cleaning_copies_registered_group_file(self):
        data=self.add_groups(self.clone('group_clean'));result=clean_dataset(data.root,self.root/'groups_cleaned');self.assertTrue(result['valid']);self.assertEqual(len(group_tokens(load_dataset(self.root/'groups_cleaned'))),12)
    def test_task_configuration_changes_copy_not_original_labels(self):
        original=(self.data.root/'case_labels.csv').read_bytes();group=group_template(self.data)
        group='\n'.join(line.replace(',false,',',true,').replace(',,case_group,',',G'+str(i)+',case_group,') if i else line for i,line in enumerate(group.splitlines()))
        copied=configure_copy(self.data,self.root/'configured',{'label_definition':self.data.manifest['label_definition'],'role_task':self.data.manifest['role_task']},group,'合成测试','仅单元验证，非真实核验')
        self.assertEqual(original,(self.data.root/'case_labels.csv').read_bytes());self.assertEqual(original,(copied.root/'case_labels.csv').read_bytes());self.assertEqual(len(group_tokens(copied)),12)
    def test_event_age_uses_explicit_cutoff_clock(self):
        latest=max(parse_time(d['timestamp']) for _,_,_,d in self.graph.edges(keys=True,data=True));graph=build_case_graph(self.data,self.cid,latest+timedelta(hours=24));first=prepare(self.data,self.graph);second=prepare(self.data,graph)
        np.testing.assert_allclose(second['event_x'][:,5].numpy()-first['event_x'][:,5].numpy(),.5,atol=1e-6)
    def test_structured_joint_training_does_not_require_unavailable_media(self):
        self.assertEqual(self.model.metadata['trained_modalities'],[]);self.assertEqual(self.model.metadata['gradient_evidence']['raw_audio'],0.);loaded=DualFlowModel.load(self.root/'joint_model');result=loaded.predict(self.data,self.graph,explain=True)
        self.assertEqual(set(result['ignored_untrained_modalities']),{'image','audio','video'});self.assertLess(abs(result['explanation']['sum_residual']),1e-6)
    def test_windows_cover_events_keep_sources_and_respect_limits(self):
        result=plan_windows(self.graph,max_nodes=40,max_events=10,overlap=2,max_windows=128);self.assertEqual(result['coverage'],1.);self.assertFalse(result['truncated'])
        self.assertTrue(all(r['nodes']<=40 and r['events']<=10 for r in result['windows']));self.assertTrue(all(d['source_id'] for r in result['windows'] for _,_,_,d in r['graph'].edges(keys=True,data=True)));self.assertEqual(len({r['graph'].graph['as_of'] for r in result['windows']}),1)
    def test_window_limit_reports_incomplete_coverage(self):
        result=plan_windows(self.graph,max_events=5,overlap=0,max_windows=1);self.assertTrue(result['truncated']);self.assertLess(result['coverage'],1.)
    def test_orchestration_can_finish_statistics_without_models(self):
        store=Store(self.root/'orchestrate.sqlite3');graph=analysis_graph(self.data,self.cid,store);directory,result=run_analysis(self.data,graph,store,self.root/'analysis_runs');self.assertEqual(result['status'],'complete');self.assertTrue((directory/'index.html').exists());self.assertTrue(result['next_actions']);self.assertTrue(store.verify_audit()['valid'])
    def test_orchestration_joint_report_contains_explanation_and_priorities(self):
        store=Store(self.root/'joint_flow.sqlite3');cid=self.data.tables['cases'][1]['case_id'];graph=analysis_graph(self.data,cid,store);directory,state=run_analysis(self.data,graph,store,self.root/'joint_analysis_runs',{'joint':self.model});result=json.loads((directory/'windows.json').read_text(encoding='utf-8'))['windows'][0]['joint'];self.assertTrue(result['available']);self.assertIn('explanation',result);self.assertIn('multiobjective',result);self.assertLess(abs(result['explanation']['sum_residual']),1e-6)
    def test_orchestration_cancellation_does_not_publish_complete_run(self):
        event=threading.Event();event.set();store=Store(self.root/'cancel.sqlite3')
        with self.assertRaises(Cancelled):run_analysis(self.data,analysis_graph(self.data,self.cid,store),store,self.root/'cancel_runs',cancel=event)
        state=json.loads(next((self.root/'cancel_runs').glob('*/run.json')).read_text(encoding='utf-8'));self.assertEqual(state['status'],'cancelled')
    def test_audit_chain_detects_modification_and_blocks_later_review(self):
        store=Store(self.root/'integrity.sqlite3');store.audit('initial',{'value':1})
        with store.connect() as conn:conn.execute("UPDATE audit SET payload='{}' WHERE id=1")
        self.assertFalse(store.verify_audit()['valid'])
        with self.assertRaisesRegex(ValueError,'一致性'):store.review('d','c','f','待核验','合成测试','测试')
        self.assertEqual(store.reviews('d','c'),[])
    def test_backup_restore_preserves_dataset_and_review_database(self):
        workspace=self.root/'workspace';(workspace/'runtime').mkdir(parents=True);(workspace/'app.py').write_text('pass',encoding='utf-8');store=Store(workspace/'runtime/analysis.sqlite3');store.review(self.data.fingerprint,self.cid,'F1','待核验','合成测试依据','测试')
        path=backup_workspace(workspace,self.data,self.root/'backups');manifest=verify_backup(path);self.assertEqual(manifest['dataset_fingerprint'],self.data.fingerprint);restored=restore_backup(path,self.root/'restored');self.assertEqual(load_dataset(restored/'data/restored_dataset').fingerprint,self.data.fingerprint);self.assertTrue(Store(restored/'runtime/analysis.sqlite3').verify_audit()['valid'])
    def test_restore_never_overwrites_nonempty_destination(self):
        with self.assertRaises((ValueError,FileNotFoundError)):restore_backup(self.root/'missing.zip',self.data.root)
    def test_atomic_json_failure_keeps_previous_file(self):
        path=self.root/'atomic.json';atomic_json(path,{'old':True})
        with self.assertRaises(ValueError):atomic_json(path,{'invalid':float('nan')})
        self.assertEqual(json.loads(path.read_text()),{'old':True})
    def test_point_model_uses_portable_path_inside_workspace(self):
        workspace=self.root/'pointer';directory=workspace/'runtime/models/model';directory.mkdir(parents=True);(directory/'weights.npz').write_bytes(b'weights');(directory/'model_card.json').write_text('{}');point_model(workspace,'latest.json',directory);self.assertEqual(json.loads((workspace/'runtime/models/latest.json').read_text())['path'],'runtime/models/model')
    def test_single_workspace_lock_is_exclusive_and_releasable(self):
        from fourflow.workspace_lock import WorkspaceLock
        first=WorkspaceLock(self.root/'lock');second=WorkspaceLock(self.root/'lock')
        try:self.assertTrue(first.acquire());self.assertFalse(second.acquire());first.release();self.assertTrue(second.acquire())
        finally:first.release();second.release()
    def test_wilson_is_not_certainty_for_zero_observed_errors(self):
        low,high=wilson(10,10);self.assertLess(low,1);self.assertAlmostEqual(high,1.)
    def test_joint_acceptance_requires_all_heads_not_only_fund(self):
        profile=criteria_template('joint');summary={'labelled_cases':100,'positive_cases':50,'negative_cases':50,'independent_groups':100,'coverage':1.,'precision':1.,'recall':1.,'person_coverage':1.,'role_macro_f1':1.,'level_macro_f1':1.,'control_precision':.1,'control_recall':.1};self.assertFalse(assess_criteria(summary,profile,'joint')['passed'])
    def test_role_acceptance_does_not_use_fund_labels(self):
        summary={'labelled_cases':12,'labelled_persons':60,'independent_groups':12,'coverage':1.,'joint_accuracy':1.};self.assertTrue(assess_criteria(summary,criteria_template('roles'),'roles')['passed'])
    def test_synthetic_model_status_stays_demonstration(self):self.assertEqual(model_status(self.root,self.model.metadata),'合成演示模型')
    def test_external_report_keeps_errors_coverage_and_cannot_approve_synthetic(self):
        external=load_dataset(create_advanced_dataset(self.root/'external',12,1561,'EXT15'));profile={k:(1 if isinstance(v,int) else 0.) for k,v in criteria_template('joint').items()}
        path,result=evaluate_external(self.model,external,'joint',self.root/'empirical',profile)
        self.assertEqual(result['summary']['labelled_cases'],12);self.assertEqual(result['summary']['independent_groups'],12);self.assertIn('raw_roles',result);self.assertTrue((path/'control_pairs.csv').exists())
        with self.assertRaisesRegex(ValueError,'合成验收'):record_review(self.root,path,self.root/'joint_model','合成测试人','仅验证拒绝合成审批',True)
        record=record_review(self.root,path,self.root/'joint_model','合成测试人','测试退回流程',False);self.assertTrue(record.exists());self.assertEqual(model_status(self.root,self.model.metadata),'合成演示模型')
    def test_external_training_asset_overlap_is_rejected(self):
        from fourflow.empirical import validate_external
        with self.assertRaisesRegex(ValueError,'共享资产'):validate_external(self.data,self.model)
    def test_audit_chain_is_transactional_when_review_save_fails(self):
        store=Store(self.root/'atomic_review.sqlite3');store.audit('start',{})
        with patch.object(store,'_append_audit',side_effect=ValueError('test interruption')):
            with self.assertRaises(ValueError):store.review('d','c','f','待核验','测试','测试')
        self.assertEqual(store.reviews('d','c'),[]);self.assertTrue(store.verify_audit()['valid'])


if __name__=='__main__':unittest.main()
