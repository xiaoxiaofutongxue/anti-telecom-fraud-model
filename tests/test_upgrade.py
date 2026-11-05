import copy
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

import numpy as np

from fourflow.core import load_dataset,build_case_graph,SCHEMA,parse_time
from fourflow.extended_data import create_extended_dataset,create_causal_demo,create_media_demo
from fourflow.roles import RoleHierarchyModel,train_roles
from fourflow.model import TemporalHeteroGNN,graph_fingerprint,train_model
from fourflow.analytics import detect_patterns
from fourflow.simulation import run_simulation
from fourflow.cleaning import clean_dataset
from fourflow.investigation import industry_traces
from fourflow.store import Store
from fourflow.service import analysis_graph
from fourflow.causal import load_study,estimate_study,cross_fit,validate_design
from fourflow.multimodal import LocalMediaProcessor,draft_communication
from fourflow.research_rendering import hierarchy_svg
from fourflow.rendering import node_choices

ROOT=Path(__file__).resolve().parents[1]


def update_csv(path,mutate):
    with path.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream);fields=reader.fieldnames;rows=list(reader)
    mutate(rows)
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)


class UpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent=ROOT/'runtime/test_workspace';parent.mkdir(parents=True,exist_ok=True)
        cls.temporary=tempfile.TemporaryDirectory(dir=parent);cls.base=Path(cls.temporary.name)
        cls.data=load_dataset(create_extended_dataset(cls.base/'data',12));cls.graph=build_case_graph(cls.data,'C001')
        cls.roles=train_roles(cls.data,cls.base/'roles',20)
        cls.fund=train_model(load_dataset(create_extended_dataset(cls.base/'fund_data',24)),cls.base/'fund_model',2)
        cls.study=load_study(create_causal_demo(cls.base/'study',400));cls.media=create_media_demo(cls.base/'media');cls.processor=LocalMediaProcessor(ROOT)

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()
    def setUp(self):self.temp=tempfile.TemporaryDirectory(dir=self.base);self.work=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_model_fingerprint_covers_flow_eligibility_and_effective_time(self):
        edge=next(iter(self.graph.edges(keys=True)))
        for field,value in [('flow','changed'),('verified','pending'),('effective_from','2026-12-01T00:00:00+08:00')]:
            changed=self.graph.copy();changed.edges[edge][field]=value
            self.assertNotEqual(graph_fingerprint(changed),graph_fingerprint(self.graph))

    def test_fund_scorer_rejects_another_dataset_snapshot(self):
        graph=self.graph.copy();graph.graph['dataset_fingerprint']='stale'
        result=TemporalHeteroGNN().assess(self.data,graph)
        self.assertFalse(result['available']);self.assertIn('快照',result['reason'])

    def test_nonfinite_model_output_is_not_exportable_probability(self):
        model=TemporalHeteroGNN();model.params['out'][:]=np.finfo(float).max;model.params['out_bias']=np.array(np.finfo(float).max)
        with np.errstate(over='ignore',invalid='ignore'),self.assertRaisesRegex(ValueError,'非有限'):model.probability(self.graph)

    def test_missing_flow_and_isolated_person_are_not_forced_into_hierarchy(self):
        graph=self.graph.copy();graph.remove_edges_from([(u,v,k) for u,v,k,d in graph.edges(keys=True,data=True) if d['flow']=='information'])
        result=self.roles.predict(self.data,graph)
        self.assertEqual(result['eligible_count'],0);self.assertEqual(result['hierarchy_links'],[])
        self.assertTrue(all(r['role']=='未判定' and r['abstention_reasons'] for r in result['predictions']))
        self.assertIn('暂不分类',hierarchy_svg(result))

    def test_large_actor_input_shift_is_disclosed_and_abstained(self):
        graph=self.graph.copy();node=next(n for n,d in graph.nodes(data=True) if d['kind']=='actor');other=next(n for n,d in graph.nodes(data=True) if d['kind']=='actor' and n!=node)
        original=next(d for _,_,_,d in graph.edges(keys=True,data=True) if d['relation']=='communicate')
        for i in range(300):graph.add_edge(node,other,key='extra'+str(i),**{**original,'record_id':'extra'+str(i)})
        result=self.roles.predict(self.data,graph);row=next(r for r in result['predictions'] if r['node']==node)
        self.assertFalse(row['prediction_available']);self.assertTrue(any('参考范围' in v for v in row['abstention_reasons']))

    def test_empty_source_is_rejected_and_cleaned_dependencies_are_quarantined(self):
        directory=create_extended_dataset(self.work/'bad',3)
        update_csv(directory/'sources.csv',lambda rows:rows[0].update(title='',reference=''))
        with self.assertRaisesRegex(ValueError,'来源标题'):load_dataset(directory)
        result=clean_dataset(directory,self.work/'clean');self.assertTrue(result['valid'])
        self.assertTrue(any(r['table']=='sources' for r in result['quarantined']))
        self.assertEqual(load_dataset(self.work/'clean').rows('funds','C001'),[])

    def test_naive_datetime_cutoff_is_normalized_to_beijing(self):
        one=build_case_graph(self.data,'C001',datetime(2026,1,1,8,10));two=build_case_graph(self.data,'C001','2026-01-01T08:10:00+08:00')
        self.assertEqual(graph_fingerprint(one),graph_fingerprint(two))

    def test_new_identity_confirmation_is_not_claimed_effective_before_dispatch(self):
        store=Store(self.work/'audit.sqlite3');store.review(self.data.fingerprint,'C001','identity:I4','已核实线索','合成验证当前确认','合成测试')
        graph=analysis_graph(self.data,'C001',store)
        self.assertTrue(any(d['record_id']=='I4' for _,_,_,d in graph.edges(keys=True,data=True)))
        traces=industry_traces(self.data,graph)
        self.assertFalse(any(s['table']=='identity_links' and s['record_id']=='I4' for t in traces for s in t['steps']))

    def test_group_interval_uses_t_and_dominant_group_suppresses_effect(self):
        study=copy.deepcopy(self.study);study['groups']=np.array(['G'+str(i%12) for i in range(len(study['t']))])
        result=estimate_study(study,0);self.assertTrue(result['available']);self.assertEqual(result['interval_degrees_of_freedom'],11);self.assertGreater(result['interval_critical_value'],2.1)
        study=copy.deepcopy(self.study);study['groups']=np.array(['BIG' if i<360 else 'G'+str(i) for i in range(len(study['t']))])
        result=estimate_study(study,0);self.assertFalse(result['available']);self.assertLess(result['diagnostics']['effective_group_count'],8);self.assertNotIn('estimate',result)

    def test_failed_optional_permutations_are_reported_without_erasing_primary_result(self):
        x,t,y,groups=[self.study[k] for k in ('x','t','y','groups')]
        def fit(xx,tt,yy,gg,*args,**kwargs):
            if xx.shape[1]==x.shape[1] and not np.array_equal(tt,t):raise ValueError('置换样本缺少某组')
            return cross_fit(xx,tt,yy,gg,*args,**kwargs)
        with patch('fourflow.causal.cross_fit',side_effect=fit):result=estimate_study(self.study,3)
        self.assertTrue(result['available']);self.assertEqual(len(result['refutations']['failed_permutations']),3);self.assertIsNone(result['refutations']['placebo_mean'])

    def test_causal_metadata_columns_and_invalid_assumptions_cannot_be_study_variables(self):
        for change in ({'assumptions':None},{'adjustment':['source_id']}):
            design={**self.study['design'],**change}
            with self.assertRaises(ValueError):validate_design(design)

    def test_hybrid_pdf_reads_image_text_even_with_a_native_text_header(self):
        from pypdf import PdfReader,PdfWriter,PageObject
        from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
        writer=PdfWriter(clone_from=str(self.media/'synthetic_scan.pdf'));page=writer.pages[0];overlay=PageObject.create_blank_page(width=page.mediabox.width,height=page.mediabox.height)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        overlay[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        content=DecodedStreamObject();content.set_data(b'BT /F1 12 Tf 20 20 Td (Native header with enough characters to bypass scan detection.) Tj ET');overlay[NameObject('/Contents')]=content;page.merge_page(overlay)
        path=self.work/'mixed.pdf'
        with path.open('wb') as stream:writer.write(stream)
        quick=self.processor.extract(path,'synthetic',pdf_mode='auto');hybrid=self.processor.extract(path,'synthetic',pdf_mode='hybrid')
        self.assertEqual({r['method'] for r in quick['segments']},{'pdf_text'});self.assertTrue(any(r['value']=='192.0.2.10' for r in hybrid['candidate_facts']))
        self.assertEqual({r.get('view') for r in hybrid['segments']},{'native_text','page_raster'})

    def test_material_import_preserves_source_that_would_be_rewritten(self):
        directory=create_extended_dataset(self.work/'source',3);update_csv(directory/'sources.csv',lambda rows:rows[0].update(reference='communications.csv'))
        data=load_dataset(directory);original=(directory/'communications.csv').read_bytes();material=self.processor.extract(self.media/'synthetic_record.png','synthetic')
        people=[r['actor_id'] for r in data.rows('actors','C001')];draft=draft_communication(data,material,self.work/'draft','C001',people[0],people[1],'2026-01-01T10:00:00+08:00','合成测试','人工确认的合成通信')
        loaded=load_dataset(draft);proof=loaded.evidence_file('C001','S001');self.assertEqual(proof.read_bytes(),original);self.assertIn('_import_parent',str(proof))

    def test_pattern_limits_are_disclosed_instead_of_claiming_complete_search(self):
        graph=self.graph.copy();graph.clear();template=next(d for _,_,_,d in self.graph.edges(keys=True,data=True) if d['relation']=='transfer')
        for i in range(401):graph.add_node(str(i),kind='wallet',entity_id=str(i))
        for i in range(400):graph.add_edge(str(i),str(i+1),key=str(i),**{**template,'record_id':str(i)})
        diagnostic={};detect_patterns(graph,diagnostics=diagnostic)
        self.assertFalse(diagnostic['search_complete']);self.assertTrue(diagnostic['skipped_cycle_currencies']);self.assertTrue(diagnostic['fund_findings_truncated'])

    def test_namespace_prevents_accidental_cross_dataset_asset_overlap(self):
        one=load_dataset(create_extended_dataset(self.work/'one',3,77,namespace='ONE13',varied=True));two=load_dataset(create_extended_dataset(self.work/'two',3,77,namespace='TWO13',varied=True))
        assets=lambda data:{n for c in data.tables['cases'] for n,d in build_case_graph(data,c['case_id']).nodes(data=True) if d['kind'] in {'wallet','ip'} and d['entity_id']!='兑换服务地址'}
        self.assertFalse(assets(one)&assets(two));self.assertEqual(len(one.tables['person_labels']),18)
        self.assertEqual(sum(r['actor_id']=='BYSTANDER' for r in one.tables['actors']),3)

    def test_invalid_actor_reference_is_rejected_before_inference(self):
        directory=self.work/'bad_model';shutil.copytree(self.base/'roles',directory);path=directory/'model_card.json';card=json.loads(path.read_text(encoding='utf-8'));card['actor_reference']['scale'][0]=0;path.write_text(json.dumps(card),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'人员输入参考'):RoleHierarchyModel.load(directory)

    def test_readable_node_choices_do_not_merge_same_named_entities(self):
        graph=self.graph.copy();actors=[n for n,d in graph.nodes(data=True) if d['kind']=='actor']
        graph.nodes[actors[0]]['label']='同名主体';graph.nodes[actors[1]]['label']='同名主体'
        forward,reverse=node_choices(graph)
        self.assertNotEqual(forward[actors[0]],forward[actors[1]])
        self.assertEqual(reverse[forward[actors[0]]],actors[0]);self.assertEqual(len(reverse),len(graph))

    def test_isolated_end_to_end_simulation_has_no_pipeline_failures_or_model_pointer_changes(self):
        project=self.work/'project';models=project/'runtime/models';models.mkdir(parents=True)
        shutil.copytree(self.base/'fund_model',models/'fund');shutil.copytree(self.base/'roles',models/'roles')
        (models/'latest.json').write_text(json.dumps({'path':'runtime/models/fund'}));(models/'roles_latest.json').write_text(json.dumps({'path':'runtime/models/roles'}))
        before={p.name:p.read_bytes() for p in models.glob('*.json')}
        directory,result=run_simulation(project,self.work/'runs',seed=57,count=2,include_media=False)
        self.assertEqual(result['failed'],0,result['checks']);self.assertEqual(len(result['checks']),11)
        self.assertTrue((directory/'report.html').is_file());self.assertTrue((directory/'cleaned/cleaning_report.json').is_file())
        self.assertEqual(before,{p.name:p.read_bytes() for p in models.glob('*.json')});self.assertFalse((project/'runtime/analysis.sqlite3').exists())


if __name__=='__main__':unittest.main()
