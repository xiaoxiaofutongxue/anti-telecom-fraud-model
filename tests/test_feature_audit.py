import copy,json,math,tempfile,unittest
from pathlib import Path
from decimal import Decimal
from fourflow.core import load_dataset
from fourflow.feature_audit import audit_features,evaluate_formula

ROOT=Path(__file__).resolve().parents[1]


class FeatureAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.root=Path(self.temp.name);self.dataset=load_dataset(ROOT/'data/enhanced_sample')
    def tearDown(self):self.temp.cleanup()
    def test_missing_is_not_zero_or_neutral_weight(self):
        for flow in ('funds','goods','information','people'):self.assertIsNone(evaluate_formula(flow,{})['value']);self.assertTrue(evaluate_formula(flow,{})['missing'])
    def test_four_formulas_with_explicit_values(self):
        self.assertAlmostEqual(evaluate_formula('funds',{'amount':30,'monthly_sender_volume':100,'time_anomaly_factor':2})['value'],.6)
        self.assertAlmostEqual(evaluate_formula('goods',{'transport_duration':20,'shortest_route_duration':10,'disguise_degree':.8})['value'],math.exp(-2)*.8)
        self.assertAlmostEqual(evaluate_formula('information',{'information_similarity':.9,'propagation_hops':2,'coded_language_intensity':2})['value'],.6)
        self.assertAlmostEqual(evaluate_formula('people',{'coappearance_frequency':6,'relationship_path_length':2,'time_overlap_factor':.5})['value'],1)
    def test_invalid_parameters_reject_nonfinite_negative_probability_and_bool(self):
        for bad in (-1,1.1,float('inf'),True):
            with self.assertRaises(ValueError):evaluate_formula('goods',{'transport_duration':2,'shortest_route_duration':1,'disguise_degree':bad})
        with self.assertRaises(ValueError):evaluate_formula('information',{'information_similarity':.5,'propagation_hops':1.5,'coded_language_intensity':1})
        with self.assertRaises(ValueError):evaluate_formula('goods',{'disguise_degree':float('nan')})
    def test_snapshot_excludes_future_transactions_from_monthly_denominator(self):
        rows=sorted(self.dataset.rows('funds','C001'),key=lambda r:r['_time']);r=rows[0];result=audit_features(self.dataset,'C001',r['_time'].isoformat());ids={rid for f in result['sender_features'] for rid in f['record_ids']};self.assertEqual(ids,{r['tx_id']});self.assertTrue(all(a['values']['monthly_sender_volume']==str(r['_amount']) for a in result['formula_audit'] if a['flow']=='funds'))
    def test_currency_and_chain_groups_never_share_entropy_denominator(self):
        d=copy.deepcopy(self.dataset);rows=d.tables['funds'];original=next(r for r in rows if r['case_id']=='C001');second=dict(original);second.update(tx_id='synthetic_other_currency',currency='XYZ',_amount=Decimal('1000000000000000000000000000000000000000000000000000000'));rows.append(second);result=audit_features(d,'C001');other=next(r for r in result['sender_features'] if r['currency']=='XYZ');self.assertEqual(other['transaction_count'],1);self.assertEqual(other['counterparty_amount_entropy_bits'],0);self.assertEqual(Decimal(other['amount_population_cv']),0);self.assertIsNone(other['mixer_use_rate'])
    def observation(self):
        r=self.dataset.rows('funds','C001')[0];doc={'kind':'fourflow_formula_observations','schema_version':1,'dataset_fingerprint':self.dataset.fingerprint,'records':[{'flow':'funds','record_id':r['tx_id'],'available_at':r['_time'].isoformat(),'confirmed':True,'reviewer':'合成测试员','evidence':[r['source_id']],'values':{'monthly_sender_volume':'100000','time_anomaly_factor':2},'definitions':{'monthly_sender_volume':'合成声明的全月发送总额，同币种','time_anomaly_factor':'合成已声明的时段因子2；不代表现实风险'}}]};return r,doc
    def save(self,doc):
        path=self.root/'observations.json';path.write_text(json.dumps(doc),encoding='utf-8');return path
    def test_enrichment_keeps_source_definition_and_computed_amount_from_original(self):
        r,doc=self.observation();doc['records'][0]['values']['amount']=999999999;result=audit_features(self.dataset,'C001',observations=self.save(doc));added=result['formula_audit'][-1];self.assertEqual(added['values']['amount'],str(r['_amount']));self.assertTrue(result['observations']['file_sha256']);self.assertTrue(added['definitions'])
    def test_enrichment_rejects_stale_fingerprint_unreviewed_or_undefined_values(self):
        _,doc=self.observation()
        for field,value in [('dataset_fingerprint','0'*64)]:
            bad=copy.deepcopy(doc);bad[field]=value
            with self.assertRaises(ValueError):audit_features(self.dataset,'C001',observations=self.save(bad))
        for field,value in [('confirmed',False),('reviewer',''),('definitions',{}),('evidence',[])]:
            bad=copy.deepcopy(doc);bad['records'][0][field]=value
            with self.assertRaises(ValueError):audit_features(self.dataset,'C001',observations=self.save(bad))
    def test_future_enrichment_does_not_leak_into_earlier_snapshot(self):
        r,doc=self.observation();doc['records'][0]['available_at']='2099-01-01T00:00:00+08:00';result=audit_features(self.dataset,'C001',as_of=r['_time'].isoformat(),observations=self.save(doc));self.assertFalse(any('reviewer' in x for x in result['formula_audit']))


if __name__=='__main__':unittest.main()
