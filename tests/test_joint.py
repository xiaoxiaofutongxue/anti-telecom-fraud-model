"""高级联合功能回归；全用D盘临时合成数据，不写用户核验库及模型指针。"""
from pathlib import Path
import copy,hashlib,json,shutil,tempfile,unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace
import numpy as np
import torch
from fourflow.core import load_dataset,build_case_graph,parse_time
from fourflow.advanced_data import create_advanced_dataset
from fourflow.hd_eia import auxiliary,prepare,attach_targets,decode_raw,clean_and_validate,quality_audit
from fourflow.dual_model import DualFlowModel,train_joint,split_cases
from fourflow.moid_xai import explain_joint,pareto_rank,save_joint_report,multiobjective_leads
from fourflow.domain_validation import evaluate_domains
from fourflow.advanced_templates import create_advanced_template
from fourflow.reporting import export_report
from fourflow.store import Store

ROOT=Path(__file__).resolve().parents[1]


class JointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(2);cls.temp=tempfile.TemporaryDirectory(prefix='joint_tests_',dir=ROOT/'runtime');cls.root=Path(cls.temp.name)
        cls.data=load_dataset(create_advanced_dataset(cls.root/'data',12,1501,'TEST14'));cls.aux=auxiliary(cls.data);cls.cid=cls.data.tables['cases'][0]['case_id'];cls.graph=build_case_graph(cls.data,cls.cid)
        cls.model=train_joint(cls.data,cls.root/'model',3,seed=1501)
        cls.item=attach_targets(prepare(cls.data,cls.graph,cls.aux),cls.data,cls.aux)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def clone(self,name):
        path=self.root/name;shutil.copytree(self.data.root,path);return path
    def update_media(self,path,change):
        rows=json.loads((path/'multimodal_links.json').read_text(encoding='utf-8'));change(rows)
        (path/'multimodal_links.json').write_text(json.dumps(rows,ensure_ascii=False),encoding='utf-8');m=json.loads((path/'manifest.json').read_text(encoding='utf-8'));m['advanced']['sha256']['multimodal_links.json']=hashlib.sha256((path/'multimodal_links.json').read_bytes()).hexdigest();(path/'manifest.json').write_text(json.dumps(m,ensure_ascii=False),encoding='utf-8')
    def test_all_neural_branches_receive_gradient(self):
        self.assertTrue(all(v>0 for v in self.model.metadata['gradient_evidence'].values()))
    def test_raw_inputs_are_pixels_waveform_and_temporal_frames(self):
        self.assertEqual(tuple(self.item['media']['image'].shape),(1,3,32,32));self.assertEqual(tuple(self.item['media']['audio'].shape),(1,1,4096));self.assertEqual(tuple(self.item['media']['video'].shape),(1,4,3,32,32))
        self.assertGreater(float((self.item['media']['video'][0,0]-self.item['media']['video'][0,-1]).abs().sum()),0)
    def test_save_load_roundtrip_predictions(self):
        loaded=DualFlowModel.load(self.root/'model')
        a=self.model.predict(self.data,self.graph);b=loaded.predict(self.data,self.graph)
        self.assertEqual(a['fund_score'],b['fund_score']);self.assertEqual(a['predictions'],b['predictions'])
    def test_parameters_reject_corruption(self):
        path=self.root/'bad_model';shutil.copytree(self.root/'model',path);(path/'weights.npz').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):DualFlowModel.load(path)
    def test_invalid_reference_threshold_rejected(self):
        path=self.root/'bad_reference';shutil.copytree(self.root/'model',path);m=json.loads((path/'model_card.json').read_text(encoding='utf-8'));m['input_reference']['threshold']=float('nan');(path/'model_card.json').write_text(json.dumps(m),encoding='utf-8')
        with self.assertRaises(ValueError):DualFlowModel.load(path)
    def test_auxiliary_change_rejected(self):
        path=self.clone('bad_aux');(path/'control_labels.csv').write_text('bad',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'指纹'):auxiliary(load_dataset(path))
    def test_unreviewed_media_rejected(self):
        path=self.clone('unreviewed');self.update_media(path,lambda rows:rows[0].update(confirmed=False))
        with self.assertRaisesRegex(ValueError,'核验'):auxiliary(load_dataset(path))
    def test_media_path_escape_rejected(self):
        path=self.clone('escape');self.update_media(path,lambda rows:rows[0].update(path='../outside.png'))
        with self.assertRaises(ValueError):auxiliary(load_dataset(path))
    def test_media_before_event_rejected(self):
        path=self.clone('early');self.update_media(path,lambda rows:rows[0].update(available_at='2000-01-01'))
        with self.assertRaisesRegex(ValueError,'时间'):auxiliary(load_dataset(path))
    def test_future_media_is_not_in_historical_inference(self):
        path=self.clone('future');self.update_media(path,lambda rows:[r.update(available_at='2030-01-01') for r in rows])
        data=load_dataset(path);graph=build_case_graph(data,self.cid,'2029-01-01');item=prepare(data,graph)
        self.assertTrue(all(len(v)==0 for v in item['media'].values()))
    def test_cached_decode_does_not_hide_file_replacement(self):
        path=self.clone('replacement');data=load_dataset(path);a=auxiliary(data);g=build_case_graph(data,self.cid);cache={};prepare(data,g,a,cache)
        row=next(r for r in a['media'] if r['case_id']==self.cid);(path/row['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'变化'):prepare(data,g,a,cache)
    def test_missing_flows_abstain(self):
        graph=self.graph.copy();graph.remove_edges_from([(u,v,k) for u,v,k,d in graph.edges(keys=True,data=True) if d['flow']=='information'])
        result=self.model.predict(self.data,graph);self.assertFalse(result['available'])
    def test_background_single_observation_not_assigned_role(self):
        result=self.model.predict(self.data,self.graph);row=next(r for r in result['predictions'] if r['actor_id']=='BYSTANDER');self.assertFalse(row['prediction_available']);self.assertEqual(row['role'],'未判定')
    def test_teacher_labels_do_not_enter_forward(self):
        data=copy.deepcopy(self.data)
        for r in data.tables['person_labels']:r['role_label']='unseen';r['level_label']='unseen'
        for r in data.tables['case_labels']:r['label']=str(1-int(r['label']))
        other=prepare(data,self.graph,self.aux)
        with torch.no_grad():a=self.model.network(self.item);b=self.model.network(other)
        for key in ('fund','role','level','control'):self.assertTrue(torch.equal(a[key],b[key]))
    def test_shapley_additivity_and_fixed_reference(self):
        result=explain_joint(self.model,self.item,self.data,self.graph)
        self.assertEqual(len(result['coalition_scores']),16);self.assertLess(abs(result['sum_residual']),1e-7);self.assertEqual(len(result['branch_ablation']),8)
        self.assertTrue(any(r['sources'] for r in result['salient_nodes']))
    def test_no_media_mask_is_explicit_and_finite(self):
        item=dict(self.item);item['media']={k:v[:0] for k,v in self.item['media'].items()};item['media_event']={k:v[:0] for k,v in self.item['media_event'].items()}
        with torch.no_grad():out=self.model.network(item)
        self.assertTrue(torch.isfinite(out['fund']));self.assertTrue(torch.equal(out['attention'][:,1:],torch.zeros_like(out['attention'][:,1:])))
    def test_control_labels_are_directed_and_unknown_masked(self):
        self.assertEqual(len(self.item['pairs']),42);self.assertEqual(int((self.item['control_target']>=0).sum()),30);self.assertEqual(int((self.item['control_target']==1).sum()),5)
    def test_pareto_dominance_and_bad_weights(self):
        keys=('coverage','traceability','uncertainty','impact');rows=[{'id':'a','objectives':dict(zip(keys,[.8]*4))},{'id':'b','objectives':dict(zip(keys,[.2]*4))}];r=pareto_rank(rows)
        self.assertTrue(r['candidates'][0]['pareto_front']);self.assertFalse(r['candidates'][1]['pareto_front'])
        with self.assertRaises(ValueError):pareto_rank(rows,dict(zip(keys,[0]*4)))
    def test_joint_report_and_checksums(self):
        path,result=save_joint_report(self.data,self.graph,self.model,self.root/'reports');hashes=json.loads((path/'checksums.json').read_text())
        self.assertTrue(all(hashlib.sha256((path/n).read_bytes()).hexdigest()==v for n,v in hashes.items()));self.assertTrue(path.with_suffix('.zip').exists());self.assertTrue(result['multiobjective']['candidates'])
    def test_stale_joint_cannot_enter_case_report(self):
        result=self.model.predict(self.data,self.graph);result['graph_fingerprint']='wrong';store=Store(self.root/'review.sqlite3')
        with self.assertRaisesRegex(ValueError,'快照'):export_report(self.data,self.graph,store,self.root/'report_bad',research={'joint':result})
    def test_cleaning_preserves_raw_media_and_alignment(self):
        result=clean_and_validate(self.data.root,self.root/'cleaned');self.assertTrue(result['cleaning']['valid']);self.assertEqual(len(result['audit']['cases']),12)
    def test_empty_template_does_not_forge_confirmation(self):
        path=create_advanced_template(self.root/'template');m=json.loads((path/'manifest.json').read_text(encoding='utf-8'));self.assertEqual(m['origin'],'real');self.assertEqual(m['advanced']['control_task']['task_id'],'');self.assertEqual(json.loads((path/'multimodal_links.json').read_text()),[])
    def test_same_dataset_rejected_as_external(self):
        with self.assertRaisesRegex(ValueError,'训练数据'):evaluate_domains(self.model,{'bad':self.data.root},self.root/'domains')
    def test_real_data_not_scored_by_synthetic_model(self):
        data=copy.deepcopy(self.data);data.manifest['origin']='real';self.assertFalse(self.model.predict(data,self.graph)['available'])
    def test_case_change_clears_joint_text_tree_and_old_output(self):
        from fourflow.advanced_ui import AdvancedPanel
        panel=AdvancedPanel.__new__(AdvancedPanel);panel.app=SimpleNamespace(dataset=self.data,graph=self.graph,current_case=lambda:self.cid,fill_tree=Mock(),set_text=Mock());panel.context=('old','old','');panel.result={'old':True};panel.output=Path('old');panel.tree=Mock();panel.text=Mock();panel.summary=Mock()
        panel.refresh();self.assertIsNone(panel.result);self.assertIsNone(panel.output);panel.app.fill_tree.assert_called_once_with(panel.tree,[],[]);panel.app.set_text.assert_called_once()
    def test_actor_identifier_permutation_preserves_logits(self):
        import networkx as nx
        mapping={n:'actor:RENAMED'+str(i) for i,n in enumerate(self.graph) if self.graph.nodes[n]['kind']=='actor'};graph=nx.relabel_nodes(self.graph,mapping,copy=True);other=prepare(self.data,graph,self.aux)
        with torch.no_grad():a=self.model.network(self.item);b=self.model.network(other)
        self.assertAlmostEqual(float(a['fund']),float(b['fund']),places=5)
        for old,new in mapping.items():np.testing.assert_allclose(a['role'][self.item['nodes'].index(old)].numpy(),b['role'][other['nodes'].index(new)].numpy(),rtol=1e-5,atol=1e-5)
    def test_joint_attachment_is_exported_with_case_report(self):
        result=self.model.predict(self.data,self.graph);store=Store(self.root/'attached.sqlite3');path=export_report(self.data,self.graph,store,self.root/'attached_reports',research={'joint':result})
        saved=json.loads((path/'analysis.json').read_text(encoding='utf-8'));self.assertEqual(saved['joint_analysis']['model_id'],self.model.metadata['model_id']);self.assertTrue((path/'joint_control_candidates.csv').exists())
    def test_rank_ranges_match_pareto_first_display_policy(self):
        keys=('coverage','traceability','uncertainty','impact');rows=[{'id':'a','objectives':dict(zip(keys,[.6]*4))},{'id':'b','objectives':dict(zip(keys,[.5]*4))},{'id':'c','objectives':dict(zip(keys,[.99,0,0,0]))}];result=pareto_rank(rows)
        self.assertEqual([r['id'] for r in result['candidates']],['a','c','b'])
        for i,r in enumerate(result['candidates'],1):self.assertLessEqual(r['rank_range'][0],i);self.assertGreaterEqual(r['rank_range'][1],i)
    def test_ip_leads_name_address_and_keep_each_record_source(self):
        result=multiobjective_leads(self.data,self.graph,{})
        rows=[r for r in result['candidates'] if r['target']=='信息/IP'];self.assertTrue(rows);self.assertEqual(len({r['question'] for r in rows}),len(rows))
        self.assertTrue(all(r['sources'] and all(s['record_id'] and s['source_id'] and s['csv_line'] for s in r['sources']) for r in rows))


if __name__=='__main__':unittest.main()
