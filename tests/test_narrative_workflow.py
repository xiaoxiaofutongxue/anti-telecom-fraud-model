from pathlib import Path
import copy,csv,json,tempfile,unittest
from html.parser import HTMLParser
from urllib.parse import urlsplit,unquote
from openpyxl import Workbook
from fourflow.narrative import import_documents,search
from fourflow.narrative_analysis import analyze
from fourflow.narrative_report import export_narrative
from fourflow.narrative_session import remember,restore_report,validate_report
from fourflow.store import Store
from fourflow.io_utils import atomic_json

ROOT=Path(__file__).resolve().parents[1]


class NarrativeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.root=Path(self.temp.name);(self.root/'inputs').mkdir();self.workspace=self.root/'workspace'
        w=Workbook();s=w.active;s.title='资产清单';s.append(['收款方','付款人/日期','金额(美元)','证据来源']);s.append(['合成收款甲','FV-1,2026.2.2','58,379.20','③¶47']);s.append(['合成收款乙','FV-10,2026.2.3','100,379.20','③¶48']);s=w.create_sheet('说明');s.append(['文字']);s.append(['合成其他编号FV-100，2026.2.2']);s.append(['同一数值58379.200美元的重复提及，未证明同一笔'])
        p=self.root/'inputs/input.xlsx';w.save(p);w.close();self.corpus=import_documents([p],self.workspace/'data/corpus','synthetic');self.store=Store(self.workspace/'runtime/analysis.sqlite3')
    def tearDown(self):self.temp.cleanup()
    def exported(self):return export_narrative(self.corpus,self.workspace/'runtime/narrative_runs',self.store)
    def test_payment_rows_with_non_flow_sheet_name_are_funds_and_not_a_fact(self):
        row=next(r for r in self.corpus.records if r['fields'].get('收款方')=='合成收款甲');self.assertEqual(row['flows'],['funds']);self.assertFalse(row['verified'])
    def test_numeric_search_normalizes_amount_separators_and_does_not_fuzzy_match_other_numbers(self):
        a=search(self.corpus,'58,379.20');b=search(self.corpus,'58379.200');self.assertEqual({r['record']['id'] for r in a},{r['record']['id'] for r in b});self.assertEqual(len(a),2);self.assertTrue(all(r['match_mode']=='numeric_exact' for r in a));self.assertFalse(any('100,379.20' in r['record']['text'] for r in a));self.assertEqual(search(self.corpus,'999999.99'),[])
    def test_identifier_search_does_not_match_prefix_of_another_id(self):
        hits=search(self.corpus,'FV-1');self.assertEqual(len(hits),1);self.assertNotIn('FV-10',hits[0]['record']['text'])
    def test_reopened_report_matches_current_dataset_version_and_reviews(self):
        folder,result=self.exported();remember(self.workspace,self.corpus,result,folder);restored,notice=restore_report(self.workspace,self.corpus,analyze(self.corpus,self.store));self.assertEqual(restored,folder);self.assertEqual(notice,'');self.assertFalse(Path(json.loads((self.workspace/'runtime/narrative_latest.json').read_text(encoding='utf-8'))['output']).is_absolute())
    def test_review_change_prevents_old_report_from_reopening(self):
        folder,result=self.exported();remember(self.workspace,self.corpus,result,folder);row=next(r for r in self.corpus.records if r['fields'].get('收款方'))
        self.store.review(self.corpus.fingerprint,row['case_id'],'narrative:'+row['id'],'待补充材料','合成测试依据','测试员');restored,notice=restore_report(self.workspace,self.corpus,analyze(self.corpus,self.store));self.assertIsNone(restored);self.assertIn('核验意见',notice)
    def test_file_modification_prevents_reopening_without_losing_data(self):
        folder,result=self.exported();remember(self.workspace,self.corpus,result,folder);(folder/'index.html').write_text('changed',encoding='utf-8');restored,notice=restore_report(self.workspace,self.corpus,result);self.assertIsNone(restored);self.assertIn('已修改',notice);self.corpus.validate()
    def test_upgraded_analysis_rejects_old_version_report(self):
        folder,result=self.exported();remember(self.workspace,self.corpus,result,folder);new=copy.deepcopy(result);new['version']='new-version';restored,notice=restore_report(self.workspace,self.corpus,new);self.assertIsNone(restored);self.assertIn('版本',notice)
    def test_different_corpus_fingerprint_rejects_old_report(self):
        folder,result=self.exported();remember(self.workspace,self.corpus,result,folder)
        # Fingerprint difference can be supplied to validation via a stand-in without changing original files.
        from types import SimpleNamespace
        different=SimpleNamespace(validate=lambda:None,fingerprint='0'*64)
        with self.assertRaisesRegex(ValueError,'资料'):validate_report(folder,different,result,self.workspace)
    def test_report_pointer_and_checksum_path_escape_are_rejected(self):
        folder,result=self.exported();atomic_json(self.workspace/'runtime/narrative_latest.json',{'path':'data/corpus','output':'../inputs'});restored,notice=restore_report(self.workspace,self.corpus,result);self.assertIsNone(restored);self.assertIn('工作区',notice)
        hashes=json.loads((folder/'checksums.json').read_text(encoding='utf-8'));hashes['../../outside']='0'*64;atomic_json(folder/'checksums.json',hashes)
        with self.assertRaisesRegex(ValueError,'越界'):validate_report(folder,self.corpus,result,self.workspace)
    def test_report_and_csv_include_human_review_and_excluded_source_is_preserved(self):
        row=next(r for r in self.corpus.records if r['fields'].get('收款方')=='合成收款甲');self.store.review(self.corpus.fingerprint,row['case_id'],'narrative:'+row['id'],'已排除','合成排除测试 <script>','测试员');folder,result=self.exported();self.assertEqual(result['cases'][0]['amount_reconstruction'][0]['listed_nominal_total'],'100379.20');page=(folder/'index.html').read_text(encoding='utf-8');self.assertIn('人工核验记录',page);self.assertIn('测试员',page);self.assertIn('&lt;script&gt;',page);source=(folder/'source_rows.html').read_text(encoding='utf-8');self.assertIn(row['id'],source);self.assertIn('当前核验：已排除',source)
        with (folder/'reviews.csv').open(encoding='utf-8-sig',newline='') as stream:reviews=list(csv.DictReader(stream))
        self.assertEqual(reviews[0]['status'],'已排除');self.assertEqual(reviews[0]['reviewer'],'测试员')
    def test_remember_without_output_clears_old_report_after_review(self):
        folder,result=self.exported();remember(self.workspace,self.corpus,result,folder);remember(self.workspace,self.corpus,result);state=json.loads((self.workspace/'runtime/narrative_latest.json').read_text(encoding='utf-8'));self.assertNotIn('output',state);self.assertEqual(restore_report(self.workspace,self.corpus,result),(None,''))
    def test_multi_case_report_has_unique_diagram_ids_and_complete_navigation(self):
        other=self.root/'inputs/other.txt';other.write_text('合成第二案件\n资金流：合成丙→合成丁，2美元',encoding='utf-8')
        self.corpus=import_documents([self.root/'inputs/input.xlsx',other],self.workspace/'data/multi','synthetic');folder,result=self.exported()
        class Page(HTMLParser):
            def __init__(self):super().__init__();self.ids=[];self.links=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if 'id' in attrs:self.ids.append(attrs['id'])
                if tag=='a' and 'href' in attrs:self.links.append(attrs['href'])
        pages={}
        for filename in ('index.html','source_rows.html'):
            page=Page();page.feed((folder/filename).read_text(encoding='utf-8'));pages[filename]=page
            self.assertEqual(len(page.ids),len(set(page.ids)),filename)
        for filename,page in pages.items():
            for href in page.links:
                url=urlsplit(href)
                if url.scheme or url.netloc:continue
                destination=unquote(url.path) or filename;self.assertTrue((folder/destination).is_file(),href)
                if url.fragment:self.assertIn(unquote(url.fragment),pages[destination].ids,href)
        for case in result['cases']:
            for suffix in ('_start','_tasks','_reconstruction','_paths','_reviews'):
                self.assertIn(case['case_id']+suffix,pages['index.html'].ids)
            self.assertIn('#'+case['case_id']+'_start',pages['index.html'].links)
            self.assertIn('index.html#'+case['case_id']+'_start',pages['source_rows.html'].links)
        self.assertIn('资料来源：合成验证资料',(folder/'index.html').read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
