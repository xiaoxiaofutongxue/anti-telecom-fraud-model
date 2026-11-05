import csv
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import networkx as nx
import numpy as np

from fourflow.core import load_dataset,build_case_graph,parse_time,entity_id,ip_scope,money,TZ
from fourflow.synthetic import create_dataset
from fourflow.store import Store
from fourflow.service import analysis_graph
from fourflow.reporting import build_report
from fourflow.investigation import industry_traces,network_analysis
from fourflow.cleaning import clean_dataset
from fourflow.model import TemporalHeteroGNN,encode_graph,train_model,graph_fingerprint
from fourflow.analytics import amount_summary,exact_sum,principal_scenario,detect_patterns

ROOT=Path(__file__).resolve().parents[1]


class DetailAuditTests(unittest.TestCase):
    def setUp(self):
        parent=ROOT/'runtime/test_workspace';parent.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent);self.base=Path(self.temp.name)
        self.raw=create_dataset(self.base/'raw',3);self.data=load_dataset(self.raw)
        self.store=Store(self.base/'audit.sqlite3')

    def tearDown(self):self.temp.cleanup()

    def review_at(self,time,link,status):
        with patch('fourflow.store.datetime') as clock:
            clock.now.return_value=parse_time(time)
            self.store.review(self.data.fingerprint,'C001',link,status,'S001：合成回归验证','测试人员')

    def test_identifier_components_cannot_collide(self):
        self.assertNotEqual(entity_id('wallet','c','a:b'),entity_id('wallet','b:c','a'))
        self.assertEqual(entity_id('wallet','W001','TRON'),'wallet:TRON:W001')
        self.assertNotEqual(entity_id('wallet','a%3Ab','TRON'),entity_id('wallet','a:b','TRON'))

    def test_confirm_then_exclude_preserves_historical_state_and_event_time(self):
        self.review_at('2026-06-01T10:00:00+08:00','identity:I4','已核实线索')
        self.review_at('2026-07-01T10:00:00+08:00','identity:I4','已排除')
        past=analysis_graph(self.data,'C001',self.store,'2026-06-15T00:00:00+08:00')
        edges=[d for u,v,k,d in past.edges(keys=True,data=True) if d['relation']=='identity' and d['record_id']=='I4']
        self.assertEqual(len(edges),1)
        self.assertEqual(edges[0]['timestamp'],self.data.rows('identity_links','C001')[-1]['_time'].isoformat())
        self.assertEqual(edges[0]['effective_from'],'2026-06-01T10:00:00+08:00')
        present=analysis_graph(self.data,'C001',self.store)
        self.assertFalse(any(d['record_id']=='I4' for u,v,k,d in present.edges(keys=True,data=True)))
        report=build_report(self.data,past,self.store)
        identity=next(r for r in report['identity_links'] if r['link_id']=='I4')
        self.assertTrue(identity['effective_verified']);self.assertEqual(identity['manual_review']['status'],'已核实线索')
        self.assertFalse(any('候选关联' in r['question'] and 'C001-W06' in r['question'] for r in report['leads']))

    def test_excluded_source_identity_is_consistent_in_graph_report_and_leads(self):
        self.review_at('2026-06-01T00:00:00+08:00','identity:I1','已排除')
        historical=analysis_graph(self.data,'C001',self.store,'2026-03-01T00:00:00+08:00')
        self.assertTrue(any(d['relation']=='identity' and d['record_id']=='I1' for u,v,k,d in historical.edges(keys=True,data=True)))
        current=analysis_graph(self.data,'C001',self.store);report=build_report(self.data,current,self.store)
        row=next(r for r in report['identity_links'] if r['link_id']=='I1')
        self.assertEqual(row['verified'],'true');self.assertFalse(row['effective_verified'])
        self.assertFalse(any(r['related_records']==['I1'] for r in report['leads']))

    def test_future_finding_review_does_not_enter_historical_report(self):
        graph=analysis_graph(self.data,'C001',self.store)
        finding=detect_patterns(graph)[0]
        self.review_at('2026-06-01T00:00:00+08:00',finding['id'],'已排除')
        graph=analysis_graph(self.data,'C001',self.store,'2026-03-01T00:00:00+08:00')
        report=build_report(self.data,graph,self.store)
        self.assertEqual(report['reviews'],[])
        self.assertTrue(all(r['status']=='待核验' for r in report['findings']))

    def test_report_rejects_stale_reviews_and_scores(self):
        graph=analysis_graph(self.data,'C001',self.store)
        self.review_at('2026-06-01T00:00:00+08:00','identity:I4','已核实线索')
        with self.assertRaisesRegex(ValueError,'核验状态'):build_report(self.data,graph,self.store)
        graph=analysis_graph(self.data,'C001',self.store)
        model=TemporalHeteroGNN();model.metadata={'origin':'synthetic','model_id':'test','trained':True,'label_definition':self.data.manifest['label_definition']}
        assessment=model.assess(self.data,graph)
        graph.graph['as_of']='2026-08-01T00:00:00+08:00'
        with self.assertRaisesRegex(ValueError,'评分与图谱'):build_report(self.data,graph,self.store,assessment=assessment)

    def test_shared_ip_detects_unknown_identity_accounts(self):
        for row in self.data.rows('network_events','C001'):row['actor_id']=''
        graph=build_case_graph(self.data,'C001')
        shared=[r for r in network_analysis(graph)['findings'] if r['kind']=='shared_ip']
        self.assertTrue(shared);self.assertIn('0个主体、3个账户',shared[0]['detail'])

    def test_multicast_is_not_globally_scoped_and_private_global_is_rejected(self):
        with self.assertRaises(ValueError):ip_scope('224.0.0.1','')
        self.assertEqual(ip_scope('224.0.0.1','LAN-A'),'LAN-A')
        with self.assertRaisesRegex(ValueError,'保留网络域'):ip_scope('192.168.1.1','global')

    def test_ablations_keep_full_graph_reference_clock(self):
        graph=build_case_graph(self.data,'C001')
        original=encode_graph(graph)[1];without=encode_graph(graph,'funds')[1]
        np.testing.assert_allclose(original['communicate'],without['communicate'],rtol=0,atol=0)
        later=graph.copy();later.graph['as_of']='2026-01-04T00:00:00+08:00'
        self.assertNotEqual(graph_fingerprint(graph),graph_fingerprint(later))
        self.assertLess(encode_graph(later)[1]['communicate'].sum(),original['communicate'].sum())

    def test_empty_assessment_and_interleaved_time_split_rejected(self):
        model=TemporalHeteroGNN();model.metadata={'origin':'synthetic','model_id':'test'}
        self.assertFalse(model.assess(self.data,build_case_graph(self.data,'C001',as_of='2020-01-01'))['available'])
        training=load_dataset(create_dataset(self.base/'training',24))
        training.tables['funds'][0]['_time']=parse_time('2027-01-01T00:00:00+08:00')
        with self.assertRaisesRegex(ValueError,'时间重叠'):train_model(training,self.base/'model',1)

    def test_duplicate_source_files_do_not_inflate_priority(self):
        source=dict(self.data.rows('sources','C001')[0]);source['source_id']='S002'
        self.data.tables['sources'].append(source);self.data.rows('funds','C001')[0]['source_id']='S002'
        trace=industry_traces(self.data,build_case_graph(self.data,'C001'))[0]
        self.assertEqual(len(trace['source_ids']),2);self.assertEqual(trace['source_group_count'],1)
        self.assertAlmostEqual(trace['objectives']['source_diversity'],1/3)
        self.assertFalse(trace['source_independence_verified'])

    def test_industry_identity_expansion_does_not_depend_on_edge_order(self):
        graph=build_case_graph(self.data,'C001')
        graph.add_node('actor:A5',kind='actor',entity_id='A5',label='新增主体',chain='',role='',role_verified=False)
        graph.add_node('wallet:TRON:ZZ',kind='wallet',entity_id='ZZ',label='其他账户',chain='TRON',role='',role_verified=False)
        attrs=next(dict(d) for u,v,k,d in graph.edges(keys=True,data=True) if d['relation']=='identity')
        graph.add_edge('actor:A5','wallet:TRON:C001-W00',key='payer',**{**attrs,'record_id':'PAYER'})
        graph.add_edge('actor:A5','wallet:TRON:ZZ',key='other',**{**attrs,'record_id':'UNRELATED'})
        reverse=nx.MultiDiGraph(**graph.graph);reverse.add_nodes_from(reversed(list(graph.nodes(data=True))))
        for u,v,k,d in reversed(list(graph.edges(keys=True,data=True))):reverse.add_edge(u,v,key=k,**d)
        a=industry_traces(self.data,graph)[0];b=industry_traces(self.data,reverse)[0]
        self.assertEqual({r['record_id'] for r in a['steps']},{r['record_id'] for r in b['steps']})
        self.assertNotIn('UNRELATED',{r['record_id'] for r in a['steps']})

    def test_long_order_does_not_include_unbounded_gap_context(self):
        self.data.rows('shipments','C001')[-1]['_time']=parse_time('2026-02-01T08:00:00+08:00')
        self.data.rows('network_events','C001')[0]['_time']=parse_time('2026-01-15T08:00:00+08:00')
        trace=industry_traces(self.data,build_case_graph(self.data,'C001'),window_hours=168)[0]
        self.assertNotIn('NET001',{r['record_id'] for r in trace['steps']})

    def test_reclean_preserves_root_lineage_and_detects_post_clean_edits(self):
        first=self.base/'first';second=self.base/'second'
        clean_dataset(self.raw,first);one=json.loads((first/'cleaning_report.json').read_text(encoding='utf-8'))
        clean_dataset(first,second);two=json.loads((second/'cleaning_report.json').read_text(encoding='utf-8'))
        key='T001|C001'
        self.assertEqual(one['origins']['funds'][key]['sha256'],two['origins']['funds'][key]['sha256'])
        self.assertEqual(len(two['origins']['funds'][key]['stages']),2)
        path=second/'funds.csv';path.write_text(path.read_text(encoding='utf-8-sig').replace('USDT','USDC'),encoding='utf-8-sig')
        altered=load_dataset(second)
        self.assertIsNone(altered.rows('funds','C001')[0]['_original_line'])
        self.assertTrue(any('funds.csv' in w and '不匹配' in w for w in altered.warnings))

    def test_declared_missing_cleaning_report_and_absolute_reference_rejected(self):
        manifest=json.loads((self.raw/'manifest.json').read_text(encoding='utf-8'));manifest['cleaning']='cleaning_report.json'
        (self.raw/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'溯源文件'):load_dataset(self.raw)
        manifest.pop('cleaning');(self.raw/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        path=self.raw/'sources.csv'
        path.write_text(path.read_text(encoding='utf-8-sig').replace('evidence/C001.txt',str(self.raw/'evidence/C001.txt')),encoding='utf-8-sig')
        with self.assertRaisesRegex(ValueError,'相对路径'):load_dataset(self.raw)

    def test_snapshot_rows_and_exact_high_precision_money(self):
        self.assertEqual(self.data.snapshot_rows('funds','C001','2020-01-01'),[])
        self.assertEqual(len(self.data.snapshot_rows('actors','C001','2020-01-01')),4)
        graph=nx.MultiDiGraph()
        for i,value in enumerate(['9999999999999999999999999999.99','0.01']):
            graph.add_edge('a','b',key=str(i),relation='transfer',currency='CNY',amount=Decimal(value))
        summary=amount_summary(graph)
        self.assertEqual(summary['gross_by_currency']['CNY'],Decimal('10000000000000000000000000000.00'))
        self.assertEqual(exact_sum(r['net'] for r in summary['addresses']),0)
        scenario=principal_scenario('9999999999999999999999999999.99','.5','1','CNY',['S001'])
        self.assertEqual(scenario['lower'],'9999999999999999999999999999.99')
        with self.assertRaisesRegex(ValueError,'精度'):money('1e10000000')

    def test_dense_repeated_single_peer_is_not_fanout(self):
        graph=nx.MultiDiGraph()
        for i in range(1200):graph.add_edge('a','b',key=str(i),table='funds',record_id=str(i),source_id='S001',relation='transfer',flow='funds',timestamp='2026-01-01T08:00:00+08:00',currency='USDT',amount=Decimal(1))
        self.assertFalse(any(r['kind'] in {'fan_in','fan_out'} for r in detect_patterns(graph)))

    def test_untrained_weights_do_not_produce_case_scores(self):
        model=TemporalHeteroGNN();model.metadata={'origin':'synthetic','model_id':'untrained'}
        self.assertFalse(model.assess(self.data,build_case_graph(self.data,'C001'))['available'])

    def test_partial_training_case_still_warns_shared_entities(self):
        full=build_case_graph(self.data,'C001');model=TemporalHeteroGNN()
        model.metadata={'origin':'synthetic','model_id':'test','trained':True,'label_definition':self.data.manifest['label_definition'],'split_entities':{'train':[n for n,d in full.nodes(data=True) if d['kind']=='wallet']}}
        partial=build_case_graph(self.data,'C001',as_of='2026-01-01T08:05:00+08:00')
        result=model.assess(self.data,partial)
        self.assertEqual(result['known_split'],'train');self.assertEqual(result['overlap_kind'],'shared_entity')

    def test_legacy_review_history_migration_preserves_audit_versions(self):
        path=self.base/'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript('CREATE TABLE audit(id INTEGER PRIMARY KEY,created TEXT,action TEXT,payload TEXT);CREATE TABLE reviews(dataset TEXT,case_id TEXT,finding_id TEXT,status TEXT,note TEXT,reviewer TEXT,updated TEXT);')
            for time,status in [('2026-06-01T00:00:00+08:00','已核实线索'),('2026-07-01T00:00:00+08:00','已排除')]:
                payload={'dataset':'d','case_id':'c','finding_id':'f','status':status,'note':'历史依据','reviewer':'测试'}
                connection.execute('INSERT INTO audit(created,action,payload) VALUES (?,?,?)',(time,'review_saved',json.dumps(payload,ensure_ascii=False)))
            connection.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?,?)',('d','c','f','已排除','历史依据','测试','2026-07-01T00:00:00+08:00'))
        store=Store(path)
        self.assertEqual(store.reviews('d','c','2026-06-15')[0]['status'],'已核实线索')
        self.assertEqual(store.reviews('d','c')[0]['status'],'已排除')

    def test_failed_async_callback_does_not_stop_future_jobs(self):
        from fourflow.ui import Application
        from queue import Queue
        from types import SimpleNamespace
        queue=Queue();queue.put(('done',None,lambda value:(_ for _ in ()).throw(ValueError('测试异常'))))
        captured=[]
        fake=SimpleNamespace(jobs=queue,busy=True,status=SimpleNamespace(set=lambda message:captured.append(message)),after=lambda *args:captured.append(args),poll_jobs=lambda:None)
        with patch('fourflow.ui.messagebox.showerror'),patch('fourflow.ui.logging.exception'):
            Application.poll_jobs(fake)
        self.assertFalse(fake.busy);self.assertTrue(any(isinstance(r,tuple) and r[0]==80 for r in captured))

    def test_target_label_semantics_cannot_be_silently_mixed(self):
        model=TemporalHeteroGNN();model.metadata={'origin':'synthetic','model_id':'test','trained':True,'label_definition':dict(self.data.manifest['label_definition'])}
        graph=build_case_graph(self.data,'C001')
        self.data.manifest['label_definition']={**self.data.manifest['label_definition'],'positive':'完全不同的任务'}
        self.assertFalse(model.assess(self.data,graph)['available'])
        training=load_dataset(create_dataset(self.base/'training',24));training.manifest.pop('label_definition')
        with self.assertRaisesRegex(ValueError,'标签含义'):train_model(training,self.base/'model',1)


if __name__=='__main__':unittest.main()
