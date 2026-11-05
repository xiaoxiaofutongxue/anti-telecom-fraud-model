import csv
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np

from fourflow.core import load_dataset,build_case_graph,normalize_ip,ip_scope,entity_id
from fourflow.cleaning import clean_dataset
from fourflow.synthetic import create_dataset
from fourflow.investigation import network_analysis,industry_traces,pca_projection,behavior_timeline
from fourflow.model import encode_graph,FEATURE_NAMES,train_model,TemporalHeteroGNN,graph_fingerprint
from fourflow.reporting import build_report
from fourflow.store import Store

ROOT=Path(__file__).resolve().parents[1]


class FraudTests(unittest.TestCase):
    def setUp(self):
        parent=ROOT/'runtime/test_workspace';parent.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent)
        self.base=Path(self.temp.name);self.source=create_dataset(self.base/'raw',3)
        self.dataset=load_dataset(self.source);self.graph=build_case_graph(self.dataset,'C001')

    def tearDown(self):self.temp.cleanup()

    def edit(self,table,callback):
        path=self.source/(table+'.csv')
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream);fields=reader.fieldnames;rows=list(reader)
        callback(rows)
        with path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)

    def test_ip_canonicalization_and_scope(self):
        self.assertEqual(normalize_ip('2001:0DB8:0000::1'),'2001:db8::1')
        self.assertEqual(normalize_ip('::ffff:192.0.2.1'),'192.0.2.1')
        with self.assertRaises(ValueError):normalize_ip('999.1.1.1')
        with self.assertRaises(ValueError):ip_scope('192.168.1.1','')
        self.assertNotEqual(entity_id('ip','192.168.1.1','LAN-A'),entity_id('ip','192.168.1.1','LAN-B'))
        self.assertEqual(ip_scope('8.8.8.8','anything'),'global')

    def test_network_hints_are_temporal_and_do_not_merge_actors(self):
        result=network_analysis(self.graph)
        self.assertEqual(len(result['observations']),9)
        self.assertTrue(any(r['kind']=='shared_ip' for r in result['findings']))
        self.assertTrue(any(r['kind']=='multi_ip' for r in result['findings']))
        self.assertEqual(sum(d['kind']=='actor' for n,d in self.graph.nodes(data=True)),4)
        past=build_case_graph(self.dataset,'C001',as_of='2026-01-01T07:59:00+08:00')
        self.assertEqual(network_analysis(past)['observations'],[])

    def test_shared_ip_outside_window_does_not_match(self):
        self.edit('network_events',lambda rows:[r.update(timestamp='2026-01-01T'+('08:00:00' if r['actor_id']=='A1' else '12:00:00' if r['actor_id']=='A2' else '16:00:00')+'+08:00') for r in rows if r['case_id']=='C001'])
        graph=build_case_graph(load_dataset(self.source),'C001')
        self.assertFalse(any(r['kind']=='shared_ip' for r in network_analysis(graph)['findings']))

    def test_industry_trace_has_sources_and_no_future_context(self):
        trace=industry_traces(self.dataset,self.graph)[0]
        self.assertEqual(trace['order'],'O001');self.assertEqual(len(trace['flows']),4)
        self.assertEqual(trace['service_types'],['通信设备'])
        self.assertTrue(all('通信设备' in s for s in trace['goods_labels']))
        self.assertTrue(all(s['source_id']=='S001' for s in trace['steps']))
        self.assertTrue(any('单一来源' in s for s in trace['missing']))
        self.assertTrue(all(s['record_id'] not in {'FX001','FX002'} for s in trace['steps']))
        early=build_case_graph(self.dataset,'C001',as_of='2026-01-01T08:29:00+08:00')
        self.assertEqual(industry_traces(self.dataset,early),[])

    def test_order_requires_explicit_payment_and_valid_time(self):
        self.edit('funds',lambda rows:[r.update(order_id='OTHER') for r in rows if r['case_id']=='C001'])
        dataset=load_dataset(self.source);trace=industry_traces(dataset,build_case_graph(dataset,'C001'))[0]
        self.assertEqual(trace['account_nodes'],[])
        self.assertTrue(any('付款' in s for s in trace['missing']))

    def test_cleaning_normalizes_deduplicates_and_preserves_original(self):
        def change(rows):
            rows[0]['amount']='1,000.00';rows[0]['currency']='usdt';rows[0]['timestamp']='2026-01-01 08:00:00'
            rows.append(dict(rows[0]))
        self.edit('funds',change)
        raw=(self.source/'funds.csv').read_bytes()
        output=self.base/'clean';report=clean_dataset(self.source,output)
        self.assertTrue(report['valid']);self.assertEqual((self.source/'funds.csv').read_bytes(),raw)
        self.assertEqual(report['counts']['funds']['input']-report['counts']['funds']['kept'],1)
        data=load_dataset(output);row=data.rows('funds','C001')[0]
        self.assertEqual(row['currency'],'USDT');self.assertEqual(row['amount'],'1000.00')
        self.assertEqual(row['_original_line'],2);self.assertEqual(len(row['_original_sha256']),64)

    def test_conflicting_ids_and_invalid_rows_quarantined_not_imputed(self):
        def change(rows):
            duplicate=dict(rows[0]);duplicate['amount']='99.00';rows.append(duplicate)
            rows[1]['amount']='NaN'
        self.edit('funds',change)
        report=clean_dataset(self.source,self.base/'clean')
        self.assertTrue(report['valid']);self.assertEqual(len(report['quarantined']),3)
        data=load_dataset(self.base/'clean')
        self.assertFalse(any(r['tx_id'] in {'T001','T002'} for r in data.rows('funds','C001')))

    def test_invalid_duplicate_cannot_leave_valid_conflicting_record(self):
        def change(rows):
            bad=dict(rows[0]);bad['amount']='not-a-number';rows.append(bad)
        self.edit('funds',change)
        report=clean_dataset(self.source,self.base/'clean')
        self.assertTrue(report['valid'])
        self.assertFalse(any(r['tx_id']=='T001' for r in load_dataset(self.base/'clean').rows('funds','C001')))

    def test_cleaning_rejects_in_place_and_cascades_missing_actors(self):
        with self.assertRaises(ValueError):clean_dataset(self.source,self.source)
        self.edit('actors',lambda rows:rows.remove(rows[0]))
        report=clean_dataset(self.source,self.base/'clean')
        self.assertTrue(report['valid']);self.assertGreater(len(report['quarantined']),1)
        self.assertTrue(any('主体不存在' in r['reason'] for r in report['quarantined']))

    def test_amount_and_ip_features_are_actual_model_inputs(self):
        x,matrices=encode_graph(self.graph)
        self.assertIn('observed_ip',matrices);self.assertIn('reverse_account_access',matrices)
        self.assertGreater(x[:,FEATURE_NAMES.index('log_distinct_ips')].max(),0)
        altered=self.graph.copy()
        for u,v,k,d in altered.edges(keys=True,data=True):
            if d['relation']=='transfer':d['amount']*=100;break
        y,_=encode_graph(altered)
        self.assertFalse(np.allclose(x[:,-4:-2],y[:,-4:-2]))

    def test_pca_is_finite_exploratory_and_text_affects_fingerprint(self):
        result=pca_projection(self.graph)
        self.assertEqual(len(result['points']),len(self.graph))
        self.assertLessEqual(sum(result['explained_variance_ratio']),1.000001)
        self.assertTrue(all(np.isfinite(r['pc1']) for r in result['points']))
        original=graph_fingerprint(self.graph)
        for u,v,k,d in self.graph.edges(keys=True,data=True):
            if d['relation']=='communicate':d['summary']='changed';break
        self.assertNotEqual(original,graph_fingerprint(self.graph))

    def test_empty_snapshot_and_network_scope_validation(self):
        empty=build_case_graph(self.dataset,'C001',as_of='2020-01-01T00:00:00+08:00')
        report=build_report(self.dataset,empty,Store(self.base/'audit.sqlite3'))
        self.assertFalse(report['pca']['available']);self.assertEqual(report['industry_traces'],[])
        self.edit('network_events',lambda rows:rows[0].update(network_scope=''))
        with self.assertRaisesRegex(ValueError,'network_scope'):load_dataset(self.source)

    def test_training_epoch_and_incompatible_old_model_rejected(self):
        with self.assertRaisesRegex(ValueError,'轮数'):train_model(self.dataset,self.base/'model',epochs=0)
        model=TemporalHeteroGNN();model.metadata={'hidden':12,'feature_names':['old_schema'],'origin':'synthetic','model_id':'old'};model.save(self.base/'model')
        with self.assertRaisesRegex(ValueError,'不兼容'):TemporalHeteroGNN.load(self.base/'model')

    def test_custom_report_windows_match_network_and_industry_results(self):
        options={'network_window_minutes':1,'industry_window_hours':1}
        report=build_report(self.dataset,self.graph,Store(self.base/'audit.sqlite3'),analysis_options=options)
        self.assertEqual(report['analysis_options'],options)
        self.assertFalse(any(r['kind']=='shared_ip' for r in report['findings']))
        self.assertEqual(report['network_analysis']['window_minutes'],1)
        self.assertTrue(all(t['window_hours']==1 for t in report['industry_traces']))

    def test_multiline_csv_physical_lines_and_currency_normalization(self):
        self.edit('communications',lambda rows:rows[0].update(summary='第一行\n第二行'))
        data=load_dataset(self.source)
        self.assertEqual(data.rows('communications','C001')[0]['_line'],3)
        self.assertEqual(data.rows('communications','C001')[1]['_line'],4)
        self.edit('funds',lambda rows:rows[0].update(currency='usdt'))
        with self.assertRaisesRegex(ValueError,'大写币种'):load_dataset(self.source)
        result=clean_dataset(self.source,self.base/'clean');self.assertTrue(result['valid'])

    def test_shared_public_ip_training_leakage_rejected(self):
        data=load_dataset(create_dataset(self.base/'training',24))
        for case in ('C001','C020'):
            row=next(r for r in data.tables['network_events'] if r['case_id']==case)
            row.update(ip='8.8.8.8',_scope='global')
        with self.assertRaisesRegex(ValueError,'公网IP'):train_model(data,self.base/'model',epochs=1)

    def test_behavior_timeline_deduplicates_derived_edges(self):
        timeline=behavior_timeline(self.dataset,self.graph)
        keys={(r['table'],r['record_id']) for r in timeline}
        self.assertEqual(len(keys),len(timeline))
        self.assertEqual(sum(r['table']=='network_events' for r in timeline),9)
        self.assertEqual(sum(r['table']=='funds' for r in timeline),len(self.dataset.rows('funds','C001')))
        self.assertEqual([r['timestamp'] for r in timeline],sorted(r['timestamp'] for r in timeline))


if __name__=='__main__':unittest.main()
