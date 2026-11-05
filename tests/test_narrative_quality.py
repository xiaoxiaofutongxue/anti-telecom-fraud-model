from pathlib import Path
from decimal import Decimal
import json,tempfile,unittest
from openpyxl import Workbook
from fourflow.narrative import import_documents,load_corpus
from fourflow.narrative_extract import amount_mentions,asset_mentions,time_hint
from fourflow.narrative_analysis import analyze
from fourflow.narrative_outputs import subject_mentions,alias_matches
from fourflow.narrative_validation import evaluate_reference
from fourflow.narrative_report import export_narrative
from fourflow.store import Store
from fourflow.io_utils import atomic_json

ROOT=Path(__file__).resolve().parents[1]


class NarrativeQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.root=Path(self.temp.name);(self.root/'input').mkdir()
    def tearDown(self):self.temp.cleanup()
    def workbook(self):
        path=self.root/'input/sample.xlsx';w=Workbook();s=w.active;s.title='资产清单';s.append(['合成验证']);s.append(['来源：合成文书¶8，私钥由演示主体甲个人持有。']);s.append(['地址','币种','数量(BTC)','证据来源']);s.append(['1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa','BTC',12,None]);s.append(['二、收款账户']);s.append(['收款方','付款人/日期','金额(美元)','付款机构','说明','证据来源']);s.append(['合成账户甲','合成付款方,2025.12.18 / 2026.1.2','9,000 / 12,000','合成机构','两项明示金额','¶9']);s=w.create_sheet('人员流分析');s.append(['人员/角色','时间','行为/职责','证据来源']);s.append(['测试甲HUANG("小甲")','2025年','合成角色陈述','¶10']);s.append(['CC-1(合成角色)','2025年','测试甲，HUANG讨论；CC-10出现','¶11']);s.append(['CC-2(合成角色)','2025年','CC-10出现','¶12']);s=w.create_sheet('资金流分析');s.append(['时间','流转路径(来源→中介→去向)','金额/规模','证据来源']);s.append(['2025.12—2026.1','合成甲→合成乙→合成丙','约1万美元','¶13']);w.save(path);w.close();return path
    def corpus(self):return import_documents([self.workbook()],self.root/'corpus','synthetic')
    def test_shared_suffix_and_header_currency_values_preserve_source_spans(self):
        text='报价9,000+12,000+3,000 USDT';items=amount_mentions(text,'价格')
        self.assertEqual([Decimal(a['value']) for a in items],[9000,12000,3000]);self.assertTrue(all(a['currency']=='USDT' and a['category']=='费用/报价陈述' for a in items))
        self.assertTrue(all(text[a['span'][0]:a['span'][1]]==a['raw'] for a in items))
        self.assertEqual([a['currency'] for a in amount_mentions('9,000 / 12,000','金额(美元)')],['USD','USD'])
    def test_unreported_currency_and_device_model_numbers_are_not_invented(self):
        items=amount_mentions('合成甲2.3万(12.4)','金额/规模');self.assertEqual(items[0]['currency'],'未明确币种')
        self.assertEqual(amount_mentions('工人iPhone 8；经理iPhone 11+；HR iPhone 12','数量/规模'),[])
    def test_negative_amount_is_preserved_and_scientific_notation_is_not_partially_read(self):
        self.assertEqual(Decimal(amount_mentions('退款-100美元')[0]['value']),-100)
        self.assertEqual(amount_mentions('1e6USD'),[])
    def test_chinese_coin_names_are_normalized_without_losing_the_source_text(self):
        items=amount_mentions('存量约123枚比特币、4枚以太币')
        self.assertEqual([(a['currency'],a['value']) for a in items],[('BTC','123'),('ETH','4')]);self.assertIn('比特币',items[0]['raw'])
    def test_mixed_quantified_and_unquantified_amounts_both_survive(self):
        items=amount_mentions('全球损失数十亿美元；约123枚比特币')
        self.assertTrue(any(a['value'] is None and a['currency']=='USD' for a in items));self.assertTrue(any(a['value']=='123' and a['currency']=='BTC' for a in items))
    def test_monetary_ranges_are_not_extracted_as_single_exact_amounts(self):
        items=amount_mentions('单项1至2万美元','金额');self.assertEqual(len(items),1);self.assertIsNone(items[0]['value']);self.assertEqual(Decimal(items[0]['lower']),10000);self.assertEqual(Decimal(items[0]['upper']),20000);self.assertFalse(items[0]['transaction'])
    def test_ambiguous_dates_and_open_ended_intervals_keep_uncertainty(self):
        self.assertIsNone(time_hint('2025.11-12')['end']);self.assertNotEqual(time_hint('2025.11-12')['precision'],'day')
        self.assertEqual(time_hint('2025.12.18 / 2026.1.2')['precision'],'multiple_dates');self.assertIsNone(time_hint('2025.12.18 / 2026.1.2')['end'])
        self.assertTrue(time_hint('2015—至少2023')['open_end']);self.assertIsNone(time_hint('2015—2020+')['end'])
        self.assertTrue(time_hint('2026年3月25日前后')['approximate'])
    def test_btc_checksum_is_separate_from_source_truth_and_typo_is_retained(self):
        good=asset_mentions('1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa')[0];bad=asset_mentions('1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb')[0]
        self.assertEqual(good['checksum_status'],'valid');self.assertEqual(bad['checksum_status'],'invalid');self.assertTrue(bad['raw'].endswith('Nb'))
    def test_decimal_hemispheres_masks_and_channel_domain_are_detected(self):
        items=asset_mentions('16.467242N,98.648357E / 16.472469N 98.645868E；钱包***abcd1；t.me/test')
        self.assertEqual(sum(a['kind']=='coordinate' for a in items),2);self.assertTrue(any(a['kind']=='wallet_fragment' and not a['complete'] for a in items));self.assertTrue(any(a['kind']=='domain' and a['value']=='t.me' for a in items));self.assertFalse(any(a['kind']=='ip' for a in items))
    def test_multi_section_header_restores_currency_and_separate_source_context(self):
        corpus=self.corpus();row=next(r for r in corpus.records if r['sheet']=='资产清单' and r['position']==7)
        self.assertEqual(row['fields']['金额(美元)'],'9,000 / 12,000');self.assertEqual(row['amounts'][0]['source_column'],'C7');self.assertEqual(row['time']['precision'],'multiple_dates')
        wallet=next(r for r in corpus.records if r['sheet']=='资产清单' and r['position']==4);self.assertEqual(wallet['source_citation_scope'],'sheet_context');self.assertEqual(wallet['source_context']['position'],2)
    def test_alias_linking_handles_two_character_names_without_cc_prefix_collision(self):
        case=analyze(self.corpus())['cases'][0];cc=next(a for a in case['entities'] if a['label']=='CC-1');self.assertEqual(len(cc['record_ids']),1)
        name=next(a for a in case['entities'] if a['label']=='测试甲HUANG');self.assertEqual(len(name['record_ids']),2)
        self.assertFalse(alias_matches('CC-1','CC-10'));self.assertTrue(alias_matches('测试甲','原文测试甲'))
    def test_explicit_groups_keep_source_statement_and_do_not_invent_control(self):
        subjects=subject_mentions('CC-5/CC-6(合成角色)');self.assertEqual([s['label'] for s in subjects],['CC-5','CC-6']);self.assertTrue(all(s['group_statement'] for s in subjects))
        self.assertEqual([s['label'] for s in subject_mentions('TV-1/2/3(合成群体)')],['TV-1','TV-2','TV-3'])
    def test_eight_outputs_graph_and_identity_remain_source_statements(self):
        case=analyze(self.corpus())['cases'][0];self.assertEqual(len(case['requirement_outputs']),8);self.assertTrue(case['path_graph']['edges']);self.assertTrue(case['identity_statements']);self.assertTrue(all(not e['verified'] for e in case['path_graph']['edges']+case['identity_statements']))
        self.assertEqual(next(t for t in case['requirement_outputs'] if t['name']=='涉案金额区间推算')['status'],'明细条件核算');self.assertIsNone(case['amount_reconstruction'][0]['whole_case_loss_interval'])
    def test_excluded_source_does_not_contribute_to_role_or_timeline(self):
        corpus=self.corpus();store=Store(self.root/'reviews.sqlite3');r=next(r for r in corpus.records if r['fields'].get('人员/角色','').startswith('测试甲'))
        store.review(corpus.fingerprint,r['case_id'],'narrative:'+r['id'],'已排除','合成核验','测试员');case=analyze(corpus,store)['cases'][0]
        self.assertFalse(any(s['record_id']==r['id'] for s in case['role_statements']+case['timeline']))
    def test_numeric_duplicate_mentions_ignore_decimal_format(self):
        p=self.root/'input/a.txt';p.write_text('12 USD\n12.0 USD',encoding='utf-8');case=analyze(import_documents([p],self.root/'corpus','synthetic'))['cases'][0]
        self.assertTrue(all(a['same_value_mentions']==2 for a in case['amount_mentions']))
    def test_reference_reports_extraction_counts_without_classification_accuracy(self):
        corpus=self.corpus();ref={'kind':'narrative_source_reference','schema_version':1,'sources':[{'case_id':c['case_id'],'sha256':c['sha256']} for c in corpus.cases],'checks':[{'case_id':corpus.cases[0]['case_id'],'sheet':'资产清单','row':7,'field':'金额(美元)','feature':'amount','expected':[['9000','USD'],['12000','USD']]}]}
        report=evaluate_reference(corpus,ref);self.assertEqual(report['passed'],1);self.assertIsNone(report['classification_accuracy']);self.assertFalse(report['classification_validated'])
        ref['sources'][0]['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'指纹'):evaluate_reference(corpus,ref)
    def test_source_identifier_path_injection_is_rejected_before_export(self):
        corpus=self.corpus();manifest=dict(corpus.manifest);manifest['cases'][0]['case_id']='../../outside';atomic_json(corpus.root/'narrative_manifest.json',manifest)
        with self.assertRaisesRegex(ValueError,'编号'):load_corpus(corpus.root)
    def test_report_exposes_eight_outputs_and_exports_checklist_and_path_edges(self):
        corpus=self.corpus();folder,_=export_narrative(corpus,self.root/'reports');self.assertTrue((folder/'investigation_checklist.csv').is_file());self.assertTrue((folder/'path_segments.csv').is_file());self.assertTrue(any(folder.glob('*_paths.svg')));self.assertIn('大纲八项输出核对',(folder/'index.html').read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
