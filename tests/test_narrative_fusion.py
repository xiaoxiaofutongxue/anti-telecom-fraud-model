from pathlib import Path
import copy,csv,json,tempfile,unittest
from openpyxl import Workbook
from fourflow.narrative import import_documents
from fourflow.narrative_extract import asset_mentions,time_hint
from fourflow.narrative_analysis import analyze
from fourflow.narrative_fusion import analyze_fusion,build_observations,network_findings,observation_time
from fourflow.narrative_fields import field_view
from fourflow.narrative_report import export_narrative
from fourflow.narrative import _record
from fourflow.store import Store

ROOT=Path(__file__).resolve().parents[1]


class FusionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.root=Path(self.temp.name);(self.root/'input').mkdir();self.serial=0
    def tearDown(self):self.temp.cleanup()
    def row(self,fields,case='SYNTHETIC',citation=''):
        self.serial+=1
        if citation:fields={**fields,'证据来源':citation}
        return _record({'case_id':case,'source_id':'S'},'合成数据',self.serial,fields)
    def network_row(self,actor='A1',ip='8.8.8.8',time='2026-02-01T08:05:00+08:00',**extra):
        return self.row({'actor_id':actor,'ip':ip,'timestamp':time,**extra})
    def workflow_rows(self):
        return [self.row({'from_account':'AC1','to_account':'AC2','amount':'500.50','currency':'USD','chain':'合成银行域','order_id':'O1','actor_id':'A1','timestamp':'2026-02-01T08:00:00+08:00'}),self.network_row(),self.row({'actor_id':'A1','person_name':'合成甲','职责':'合成收货人员','timestamp':'2026-02-01T08:10:00+08:00'}),self.row({'order_id':'O1','goods_label':'合成通信设备','receiver_id':'A1','shipment_id':'SH1','timestamp':'2026-02-01T08:30:00+08:00'})]
    def test_explicit_order_and_actor_time_join_covers_four_flows(self):
        rows=self.workflow_rows();f=analyze_fusion(rows,[]);t=f['industry']['traces'][0]
        self.assertEqual(set(t['flows']),{'funds','information','people','industry'});self.assertEqual(set(t['record_ids']),{r['id'] for r in rows});self.assertEqual(t['temporal_checks'][0]['status'],'窗口内先付款后产业记录');self.assertFalse(t['verified'])
    def test_equal_amount_or_similar_order_never_links_payment(self):
        rows=self.workflow_rows();rows[0]['fields']['order_id']='O10';f=analyze_fusion(rows,[]);t=f['industry']['traces'][0];self.assertNotIn(rows[0]['id'],t['record_ids']);self.assertNotIn('funds',t['flows'])
    def test_shared_ip_does_not_join_another_actor_into_industry(self):
        rows=self.workflow_rows();other=self.network_row(actor='A2');rows.append(other);f=analyze_fusion(rows,[]);self.assertTrue(f['network']['findings']);self.assertNotIn(other['id'],f['industry']['traces'][0]['record_ids'])
    def test_one_observation_with_actor_and_account_is_not_shared_ip(self):
        r=self.network_row(account_id='AC1',chain='合成银行域');self.assertFalse(analyze_fusion([r],[])['network']['findings'])
    def test_repeated_same_actor_is_not_shared_ip(self):
        rows=[self.network_row(),self.network_row(time='2026-02-01T08:06:00+08:00')];self.assertFalse(analyze_fusion(rows,[])['network']['findings'])
    def test_ipv6_normalization_and_timezones(self):
        rows=[self.network_row(ip='2606:4700:4700:0:0:0:0:1111'),self.network_row(actor='A2',ip='2606:4700:4700::1111',time='2026-02-01T00:06:00Z')];f=analyze_fusion(rows,[]);self.assertEqual(len(f['network']['ips']),1);self.assertEqual(f['network']['findings'][0]['kind'],'shared_ip');self.assertIn('2606:4700:4700::1111',[a['value'] for a in asset_mentions('源IP为[2606:4700:4700::1111]')])
    def test_private_ip_domains_are_isolated(self):
        rows=[self.network_row(ip='10.0.0.1',network_scope='合成域甲'),self.network_row(actor='A2',ip='10.0.0.1',network_scope='合成域乙')];f=analyze_fusion(rows,[]);self.assertEqual(len(f['network']['ips']),2);self.assertFalse(f['network']['findings'])
    def test_private_ip_without_domain_is_not_usable(self):
        f=analyze_fusion([self.network_row(ip='10.0.0.1')],[]);self.assertEqual(f['network']['usable_observations'],0);self.assertTrue(f['quality']['warnings'])
    def test_private_ip_cannot_impersonate_global_network_domain(self):
        f=analyze_fusion([self.network_row(ip='10.0.0.1',network_scope='GLOBAL')],[]);self.assertEqual(f['network']['usable_observations'],0);self.assertTrue(any('global' in issue for issue in f['quality']['warnings'][0]['issues']))
    def test_non_ascii_numeric_port_does_not_crash_or_create_network_observation(self):
        for port in ('²','１２３','١٢٣','1.0','1e2','9999999999999999999999999999999999999'):
            f=analyze_fusion([self.network_row(source_port=port)],[]);self.assertEqual(f['network']['usable_observations'],0);self.assertTrue(any('端口' in issue for issue in f['quality']['warnings'][0]['issues']))
    def test_datetime_utc_overflow_is_reported_as_invalid(self):
        for raw in ('0001-01-01T00:00:00+23:59','9999-12-31T23:59:59-23:59'):
            self.assertIsNone(time_hint(raw)['start']);self.assertIsNone(observation_time(raw)['instant'])
    def test_overflow_timestamp_file_can_still_export_for_review(self):
        c=self.csv_corpus('actor_id,ip,timestamp\nA1,8.8.8.8,0001-01-01T00:00:00+23:59\n');folder,result=export_narrative(c,self.root/'invalid_time_reports');self.assertEqual(result['cases'][0]['fusion']['network']['usable_observations'],0);self.assertTrue((folder/'source_rows.html').is_file())
    def test_invalid_ip_or_port_retains_source_and_skips_network(self):
        for extra in ({'ip':'999.1.1.1'},{'ip':'8.8.8.8:443'},{'source_port':'70000'},{'source_port':'-1'}):
            row=self.network_row(**extra);f=analyze_fusion([row],[]);self.assertEqual(f['network']['usable_observations'],0);self.assertEqual(len(f['observations']),1);self.assertTrue(f['quality']['warnings'])
    def test_missing_timezone_or_date_only_does_not_create_access_timeline(self):
        for time in ('2026-02-01','2026-02-01T08:05:00','2026年2月',''):
            f=analyze_fusion([self.network_row(time=time)],[]);self.assertEqual(f['network']['usable_observations'],0)
    def test_invalid_time_is_not_an_instant(self):
        for raw in ('2026-02-31T08:00:00+08:00','2026-13-01','2026-02-01T25:00:00Z','约2026-02-01T08:00:00Z','2026-02-01T08:00:00+08:99'):
            self.assertIsNone(observation_time(raw)['instant'])
    def test_general_timeline_preserves_exact_time_and_sorts_utc(self):
        c=self.csv_corpus('actor_id,ip,timestamp\nA1,8.8.8.8,2026-02-01T00:15:00Z\nA2,1.1.1.1,2026-02-01T08:05:00+08:00\n');case=analyze(c)['cases'][0];self.assertTrue(all(r['time']['precision']=='time' for r in case['timeline']));self.assertEqual(case['timeline'][0]['time']['raw'],'2026-02-01T08:05:00+08:00');self.assertFalse(any('省略' in issue for r in c.records for issue in r['issues']))
    def test_invalid_exact_time_is_not_reinterpreted_as_a_partial_date(self):
        t=time_hint('2026-02-31T08:00:00+08:00');self.assertEqual(t['precision'],'invalid_time');self.assertIsNone(t['start'])
    def test_multi_ip_requires_same_actor_and_time_window(self):
        rows=[self.network_row(ip=ip) for ip in ('8.8.8.8','1.1.1.1','9.9.9.9')];f=analyze_fusion(rows,[]);self.assertIn('multi_ip',[r['kind'] for r in f['network']['findings']]);rows[-1]['fields']['timestamp']='2026-02-02T08:05:00+08:00';self.assertNotIn('multi_ip',[r['kind'] for r in analyze_fusion(rows,[])['network']['findings']])
    def test_conflicting_alias_columns_are_not_arbitrarily_selected(self):
        rows=self.workflow_rows();rows[-1]['fields']['订单号']='O2';f=analyze_fusion(rows,[]);self.assertEqual(f['quality']['conflicting_rows'],1);self.assertNotIn(rows[0]['id'],f['industry']['traces'][0]['record_ids']);self.assertNotIn('订单编号',field_view(rows[-1]['fields'])['values'])
    def test_conflicting_amount_columns_block_payment_association(self):
        rows=self.workflow_rows();rows[0]['fields']['交易金额']='600';f=analyze_fusion(rows,[]);self.assertNotIn('funds',f['industry']['traces'][0]['flows'])
    def test_duplicate_content_is_reported_without_deleting_original_rows(self):
        a=self.network_row();b=self.network_row();f=analyze_fusion([a,b],[]);self.assertEqual(f['quality']['duplicate_candidate_groups'],[[a['id'],b['id']]]);self.assertEqual(len(f['observations']),2)
    def test_exclusion_updates_coverage_network_and_industry(self):
        rows=self.workflow_rows();f=analyze_fusion(rows,[],[rows[0]['id'],rows[1]['id']]);self.assertEqual(f['quality']['excluded_rows'],2);self.assertEqual(f['network']['usable_observations'],0);self.assertNotIn('funds',f['industry']['traces'][0]['flows'])
    def test_formula_row_is_quarantined(self):
        r=self.network_row();r['formula_present']=True;f=analyze_fusion([r],[]);self.assertEqual(f['quality']['formula_rows'],1);self.assertFalse(f['observations'])
    def test_cross_document_identifiers_do_not_implicitly_merge(self):
        rows=self.workflow_rows()
        for r in rows:r['source_citation']='①¶1'
        rows[-1]['source_citation']='③¶1';f=analyze_fusion(rows,[]);self.assertNotIn(rows[0]['id'],f['industry']['traces'][0]['record_ids'])
    def test_missing_account_domain_prevents_cross_row_account_join(self):
        rows=self.workflow_rows();rows[0]['fields'].pop('chain');rows[1]['fields'].pop('actor_id');rows[1]['fields'].update(account_id='AC1');f=analyze_fusion(rows,[]);self.assertNotIn(rows[1]['id'],f['industry']['traces'][0]['record_ids']);self.assertTrue(any('账户域' in issue for issue in f['observations'][0]['missing']))
    def test_time_conflict_is_visible_but_does_not_erase_order_evidence(self):
        rows=self.workflow_rows();rows[0]['fields']['timestamp']='2026-02-01T09:00:00+08:00';t=analyze_fusion(rows,[])['industry']['traces'][0];self.assertIn(rows[0]['id'],t['record_ids']);self.assertEqual(t['temporal_checks'][0]['status'],'付款晚于产业记录')
    def test_missing_context_time_is_not_attached_to_industry(self):
        rows=self.workflow_rows();rows[1]['fields']['timestamp']='2026-02-01';f=analyze_fusion(rows,[]);self.assertNotIn(rows[1]['id'],f['industry']['traces'][0]['record_ids']);self.assertTrue(any('未并入' in m for m in f['industry']['traces'][0]['missing']))
    def test_limits_are_explicit(self):
        rows=[self.row({'order_id':'O'+str(i),'goods_label':'合成设备'}) for i in range(205)];f=analyze_fusion(rows,[]);self.assertEqual(len(f['industry']['traces']),200);self.assertEqual(f['industry']['omitted_traces'],5)
    def test_invalid_windows_rejected(self):
        with self.assertRaises(ValueError):analyze_fusion([],[],window_hours=0)
        with self.assertRaises(ValueError):analyze_fusion([],[],network_minutes=1500)
    def csv_corpus(self,text):
        self.serial+=1;p=self.root/'input'/('sample'+str(self.serial)+'.csv');p.write_text(text,encoding='utf-8');return import_documents([p],self.root/('corpus'+str(self.serial)),'synthetic')
    def test_csv_aliases_and_separate_currency_produce_payment_statement(self):
        c=self.csv_corpus('from_account,to_account,amount,currency,chain,order_id,timestamp\nAC1,AC2,500.50,USD,合成银行域,O1,2026-02-01T08:00:00+08:00\n');result=analyze(c)['cases'][0];self.assertEqual(result['statement_events'][0]['amount'],'500.50');self.assertIn('funds',c.records[0]['flows']);self.assertEqual(c.records[0]['fields']['amount'],'500.50')
        self.assertEqual(result['statement_events'][0]['event_kind'],'account_payment_statement')
    def test_unit_headers_with_spaces_and_fullwidth_parentheses(self):
        c=self.csv_corpus('付款方,收款人,交易金额 （ USD ）,日期\n合成甲,合成乙,123.45,2026-02-01\n');case=analyze(c)['cases'][0];self.assertEqual(case['statement_events'][0]['amount'],'123.45');self.assertEqual(case['statement_events'][0]['valuation_currency'],'USD')
    def test_decimal_precision_is_not_rounded_to_default_context(self):
        value='123456789012345678901234567890.01';c=self.csv_corpus('from_account,to_account,amount,currency\nAC1,AC2,'+value+',USD\n');case=analyze(c)['cases'][0];self.assertEqual(case['statement_events'][0]['amount'],value);self.assertEqual(case['amount_reconstruction'][0]['listed_nominal_total'],value)
    def test_negative_or_unknown_currency_is_not_linked_as_positive_payment(self):
        for amount,currency in (('-5','USD'),('0','USD'),('500','')):
            rows=self.workflow_rows();rows[0]=self.row({'from_account':'AC1','to_account':'AC2','amount':amount,'currency':currency,'order_id':'O1'});f=analyze_fusion(rows,[]);self.assertNotIn('funds',f['industry']['traces'][0]['flows']);self.assertTrue(f['quality']['warnings'])
    def test_separate_case_files_remain_separate(self):
        first=self.root/'input/first.csv';second=self.root/'input/second.csv';first.write_text('from_account,to_account,amount,currency,order_id\nAC1,AC2,500,USD,O1\n',encoding='utf-8');second.write_text('order_id,goods_label\nO1,合成设备\n',encoding='utf-8');c=import_documents([first,second],self.root/'separate','synthetic');cases=analyze(c)['cases'];self.assertEqual(len(cases),2);trace=next(x for x in cases if x['fusion']['industry']['traces'])['fusion']['industry']['traces'][0];self.assertNotIn('funds',trace['flows'])
    def test_export_escapes_source_text_in_new_fusion_tables(self):
        c=self.csv_corpus('order_id,goods_label\nO1,<script>alert(1)</script>\n');folder,_=export_narrative(c,self.root/'safe_reports');page=(folder/'index.html').read_text(encoding='utf-8');self.assertNotIn('<script>',page);self.assertIn('&lt;script&gt;',page)
    def test_checklist_recognizes_supplied_logs_without_claiming_verified_identity(self):
        c=self.csv_corpus('actor_id,ip,timestamp\nA1,8.8.8.8,2026-02-01T08:00:00+08:00\n');case=analyze(c)['cases'][0];request=next(q for q in case['investigation_checklist'] if q['category']=='IP与通信轨迹');self.assertEqual(request['priority'],'核验已有资料');self.assertEqual(request['record_ids'],[c.records[0]['id']]);self.assertIn('不作为身份认定',request['action'])
    def test_csv_multiline_row_location_and_source_hash_are_preserved(self):
        c=self.csv_corpus('actor_id,summary,timestamp\nA1,"合成第一行\n合成第二行",2026-02-01T08:00:00Z\n');self.assertEqual(c.records[0]['position'],2);self.assertIn('2-3',c.records[0]['range']);self.assertIn('\n',c.records[0]['fields']['summary']);c.validate()
    def test_csv_bad_column_count_and_duplicate_headers_reject_without_partial_dataset(self):
        for text in ('ip,ip\n8.8.8.8,1.1.1.1\n','ip,actor_id\n8.8.8.8,A1,EXTRA\n'):
            with self.assertRaises(ValueError):self.csv_corpus(text)
    def test_workbook_english_headers_and_export_have_complete_sources(self):
        p=self.root/'input/all.xlsx';w=Workbook();w.remove(w.active)
        for index,row in enumerate(self.workflow_rows()):
            s=w.create_sheet('合成表'+str(index));s.append(list(row['fields']));s.append(list(row['fields'].values()))
        w.save(p);w.close();c=import_documents([p],self.root/'book','synthetic');folder,result=export_narrative(c,self.root/'reports');case=result['cases'][0];self.assertEqual(set(case['fusion']['industry']['traces'][0]['flows']),{'funds','information','people','industry'})
        for filename in ('fourflow_coverage.csv','network_observations.csv','network_findings.csv','industry_candidates.csv','data_quality.csv','field_mapping.csv'):self.assertTrue((folder/filename).is_file())
        page=(folder/'index.html').read_text(encoding='utf-8');self.assertIn('四流关联与产业溯源',page);self.assertIn('O1',page);self.assertIn('同订单编号的付款陈述',page)
        store=Store(self.root/'store.sqlite3');store.review(c.fingerprint,case['case_id'],'narrative:'+c.records[0]['id'],'已排除','合成流程验证','合成核验员');updated=analyze(c,store)['cases'][0];self.assertFalse(updated['statement_events']);self.assertNotIn('funds',updated['fusion']['industry']['traces'][0]['flows']);self.assertEqual(updated['flows']['funds'],0)


if __name__=='__main__':unittest.main()
