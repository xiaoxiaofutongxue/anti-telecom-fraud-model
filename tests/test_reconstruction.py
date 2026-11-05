"""Fictional edge cases: source isolation, precision and explicit direction, not real efficacy."""
from pathlib import Path
import tempfile,unittest
from openpyxl import Workbook
from fourflow.narrative import import_documents
from fourflow.narrative_analysis import analyze
from fourflow.narrative_outputs import subject_mentions
from fourflow.narrative_reconstruction import dates_in_field,source_scopes,amount_envelope
from fourflow.narrative_report import export_narrative
from fourflow.store import Store

ROOT=Path(__file__).resolve().parents[1]


class ReconstructionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.root=Path(self.temp.name);(self.root/'input').mkdir()
    def tearDown(self):self.temp.cleanup()
    def corpus(self,cycle=False):
        w=Workbook();s=w.active;s.title='人员流分析';s.append(['人员/角色','时间','行为/职责','证据来源'])
        for row in [('合成甲HUANG','2025年','向JIANG汇报' if cycle else '合成角色','①¶1'),('合成乙JIANG','2025年','直报huang','②¶2'),('Worker-1','2025年','JIANG直属','②¶3'),('TV-1','2025年','合成劳工','①¶4'),('TV-1','2025年','合成劳工，非同一身份','③¶1'),('合成孤立人','2025年','角色称谓没有上下级','①¶5')]:s.append(row)
        s=w.create_sheet('说明');s.append(['说明']);s.append(['文书①②的宣誓书为同一份（合成验证）'])
        s=w.create_sheet('支付明细');s.append(['收款方','付款人/日期','金额(美元)','证据来源']);s.append(['壳公司1','FV-1, 2025.11.25/26、12.9','50,000 / 120,000 / 121,000','③¶2']);s.append(['壳公司2','FV-1, 2025.12.1','58379.20','③¶3']);s.append(['壳公司2','FV-1, 2025.12.1','58379.200','③¶4'])
        p=self.root/'input/sample.xlsx';w.save(p);w.close();return import_documents([p],self.root/'corpus','synthetic')
    def test_anonymous_ids_are_separated_but_same_source_markers_collapse(self):
        c=analyze(self.corpus())['cases'][0];people=[a for a in c['entities'] if a['label']=='TV-1'];self.assertEqual(len(people),2);self.assertEqual({a['namespace'] for a in people},{'doc_①②','doc_③'});self.assertTrue(all(len(a['record_ids'])==1 for a in people))
    def test_anonymous_place_prefix_is_not_an_alias(self):
        self.assertEqual(subject_mentions('安龙TV-1(合成别称)')[0]['aliases'],['安龙TV-1'])
    def test_partial_hierarchy_uses_directions_and_keeps_isolates_out(self):
        c=analyze(self.corpus())['cases'][0];h=c['hierarchy_reconstruction'];self.assertEqual(len(h['edges']),2);levels={a['label']:h['partial_levels'].get(a['id']) for a in h['nodes']};self.assertEqual([levels[k] for k in ['合成甲HUANG','合成乙JIANG','Worker-1']],[0,1,2]);self.assertIsNone(levels['合成孤立人']);self.assertTrue(all(not a['verified'] for a in h['edges']))
    def test_cycle_never_forces_a_hierarchy(self):
        h=analyze(self.corpus(cycle=True))['cases'][0]['hierarchy_reconstruction'];self.assertEqual(len(h['cycles']),1);self.assertEqual(h['partial_levels'],{})
    def test_ordered_dates_and_cent_values_remain_exact(self):
        c=analyze(self.corpus())['cases'][0];events=c['statement_events'];self.assertEqual([r['date_start'] for r in events[:3]],['2025-11-25','2025-11-26','2025-12-09']);self.assertEqual(events[1]['date_inherited_parts'],['year','month']);a=c['amount_reconstruction'][0];self.assertEqual(a['listed_nominal_total'],'407758.400');self.assertEqual(a['conditional_distinct_transfer_interval']['lower'],'349379.20');self.assertEqual(len(a['possible_duplicate_groups']),1);self.assertIsNone(a['whole_case_loss_interval'])
    def test_mixed_scope_and_missing_scope_do_not_merge_with_known_source(self):
        rows=[{'id':'a','source_citation':'①¶1','text':''},{'id':'b','source_citation':'③¶1','text':''},{'id':'c','source_citation':'①③','text':''},{'id':'d','source_citation':'','text':''},{'id':'e','source_citation':'','text':''}];scopes=source_scopes(rows)['rows'];self.assertTrue(scopes['c']['ambiguous']);self.assertNotEqual(scopes['d']['namespace'],scopes['e']['namespace'])
    def test_negated_same_source_declaration_never_merges_namespaces(self):
        rows=[{'id':'a','source_citation':'①¶1','text':'文书①②并非为同一份'},{'id':'b','source_citation':'②¶1','text':''}];scopes=source_scopes(rows);self.assertNotEqual(scopes['rows']['a']['namespace'],scopes['rows']['b']['namespace']);self.assertFalse(scopes['same_source_declarations'])
    def test_date_ranges_not_assigned_to_individual_components(self):
        d=dates_in_field('2026.1.21—2.17');self.assertTrue(d['range']);self.assertEqual([r['date'] for r in d['items']],['2026-01-21','2026-02-17'])
    def test_invalid_and_approximate_date_list_is_not_exact(self):
        self.assertTrue(dates_in_field('2025.12.1 / 12.999')['reason']);self.assertTrue(dates_in_field('2025.12.1前后')['reason']);self.assertTrue(dates_in_field('2025.12.31 / 1.2')['reason']);self.assertTrue(dates_in_field('2025.2.30')['reason'])
    def test_exclusion_removes_calculations_and_directions(self):
        corpus=self.corpus();store=Store(self.root/'review.sqlite3')
        for r in corpus.records:
            if r['fields'].get('人员/角色')=='合成乙JIANG' or r['fields'].get('收款方'):store.review(corpus.fingerprint,r['case_id'],'narrative:'+r['id'],'已排除','合成排除','测试员')
        c=analyze(corpus,store)['cases'][0];self.assertEqual(c['statement_events'],[]);self.assertFalse(any(e['to_label']=='合成乙JIANG' for e in c['hierarchy_reconstruction']['edges']))
    def test_negation_does_not_create_positive_direction(self):
        corpus=self.corpus();row=next(r for r in corpus.records if r['fields'].get('人员/角色')=='合成乙JIANG');row['fields']['行为/职责']='并未直报HUANG';row['text']='并未直报HUANG'
        from fourflow.narrative_reconstruction import hierarchy_claims
        # Directly reuse entity seeds without persisting changed source material.
        actors=[{'id':'A','kind':'person_mention','label':'合成甲HUANG','aliases':['HUANG','合成甲HUANG'],'record_ids':[]},{'id':'B','kind':'person_mention','label':'合成乙JIANG','aliases':['JIANG','合成乙JIANG'],'record_ids':[row['id']]}]
        h=hierarchy_claims([row],actors,{row['id']:{'namespace':'doc_①②','ambiguous':False}});self.assertFalse(h['edges']);self.assertTrue(h['unresolved'])
    def test_report_exports_source_reconstruction_and_safe_csv(self):
        folder,_=export_narrative(self.corpus(),self.root/'reports');page=(folder/'index.html').read_text(encoding='utf-8');self.assertIn('结构还原与核算',page);self.assertIn('文书范围',page);self.assertTrue((folder/'statement_payments.csv').is_file());self.assertTrue((folder/'hierarchy_candidates.csv').is_file());self.assertTrue((folder/'amount_reconstruction.csv').is_file())
        import csv
        with (folder/'assets.csv').open(encoding='utf-8-sig',newline='') as stream:people=[r for r in csv.DictReader(stream) if r['label']=='TV-1']
        self.assertEqual({r['namespace'] for r in people},{'doc_①②','doc_③'})


if __name__=='__main__':unittest.main()
