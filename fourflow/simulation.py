"""隔离的全流程合成模拟：正常、脏数据、缺流、历史核验及材料接入。"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import copy,csv,hashlib,html,json,shutil,zipfile

import networkx as nx
import numpy as np

from .core import TZ,SCHEMA,load_dataset,temporal_paths,build_case_graph
from .extended_data import create_extended_dataset,create_causal_demo
from .cleaning import clean_dataset
from .model import TemporalHeteroGNN,resolve_model_directory,graph_fingerprint
from .roles import RoleHierarchyModel
from .evaluation import evaluate_models
from .analytics import amount_summary,detect_patterns,evidence_chain,principal_scenario
from .service import analysis_graph,identity_states
from .store import Store
from .investigation import industry_traces,network_analysis
from .projection import project_graph
from .trajectory import behavior_graph
from .reporting import export_report,write_csv
from .causal import load_study,estimate_study,save_causal_run
from .multimodal import LocalMediaProcessor,save_extraction,draft_communication


def _sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dirty(directory):
    def table(name):
        with (directory/(name+'.csv')).open(encoding='utf-8-sig',newline='') as stream:return list(csv.DictReader(stream))
    def save(name,rows):
        with (directory/(name+'.csv')).open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=SCHEMA[name]);writer.writeheader();writer.writerows(rows)
    funds=table('funds');first=funds[0];first['currency']=' '+first['currency'].lower()+' ';first['amount']=f"{Decimal(first['amount']):,.2f}"
    funds+=[dict(first),{**first,'tx_id':'BAD_NAN','amount':'NaN'},{**first,'tx_id':'CONFLICT','amount':'100'},{**first,'tx_id':'CONFLICT','amount':'200'},{**first,'tx_id':'BAD_SOURCE_TX','source_id':'BAD_SOURCE'}];save('funds',funds)
    network=table('network_events');network.append({**network[0],'access_id':'BAD_IP','ip':'999.1.2.3'});save('network_events',network)
    communications=table('communications');communications.append({**communications[0],'event_id':'BAD_ACTOR','from_actor':'NOT_PRESENT'});save('communications',communications)
    sources=table('sources');sources.append({**sources[0],'source_id':'BAD_SOURCE','title':'','reference':''});save('sources',sources)


def run_simulation(project_root,output_root=None,seed=1001,count=12,include_media=True,progress=None):
    if not isinstance(seed,int) or not 0<=seed<=2147483647 or not isinstance(count,int) or not 2<=count<=24:raise ValueError('模拟种子须为0至2147483647，案件数为2至24')
    root=Path(project_root).resolve();progress=progress or (lambda message:None)
    output=Path(output_root or root/'runtime/simulations').resolve();run=output/datetime.now(TZ).strftime('simulation_%Y%m%d_%H%M%S_%f');run.mkdir(parents=True,exist_ok=False)
    checks=[];links=[];evaluation=None;data=None;store=None;graph=None;fund=None;roles=None
    def check(name,function):
        progress('流程模拟：'+name)
        try:
            detail=function() or {};status=detail.pop('_status','通过');checks.append({'name':name,'status':status,'detail':detail});return detail
        except Exception as exc:checks.append({'name':name,'status':'失败','detail':{'error':str(exc)}});return None
    def inputs():
        nonlocal data,store,graph
        original=load_dataset(create_extended_dataset(run/'reference_input',count,seed,namespace='SIM13_'+str(seed),varied=True))
        shutil.copytree(original.root,run/'raw_input');_dirty(run/'raw_input');before={p.name:_sha(p) for p in (run/'raw_input').glob('*.csv')}
        try:load_dataset(run/'raw_input')
        except ValueError:pass
        else:raise AssertionError('脏输入不应直接通过严格导入')
        result=clean_dataset(run/'raw_input',run/'cleaned');assert result['valid'];assert len(result['quarantined'])==7
        assert before=={p.name:_sha(p) for p in (run/'raw_input').glob('*.csv')},'原始输入被清洗改写'
        data=load_dataset(run/'cleaned');store=Store(run/'simulation.sqlite3');cid=data.tables['cases'][0]['case_id'];graph=analysis_graph(data,cid,store)
        expected={r['case_id']:amount_summary(build_case_graph(original,r['case_id']))['gross_by_currency'] for r in original.tables['cases']}
        assert all(amount_summary(build_case_graph(data,c))['gross_by_currency']==amounts for c,amounts in expected.items())
        assert all(r['_lineage_verified'] for rows in data.tables.values() for r in rows)
        return {'cases':len(data.tables['cases']),'quarantined':len(result['quarantined']),'normalization_changes':len(result['changes']),'original_unchanged':True,'amounts_unchanged':True,'cleaning_report':'cleaned/cleaning_report.json'}
    check('导入、去重、冲突隔离与原始溯源',inputs)
    if data is not None:
        cid=graph.graph['case_id']
        def graph_checks():
            totals=amount_summary(graph);assert {d['flow'] for _,_,_,d in graph.edges(keys=True,data=True)}=={'funds','goods','information','people'}
            for currency in totals['gross_by_currency']:assert sum((r['net'] for r in totals['addresses'] if r['currency']==currency),Decimal(0))==0
            trajectory=behavior_graph(graph);assert trajectory['available'];dag=nx.DiGraph((e['from'],e['to']) for e in trajectory['edges']);assert nx.is_directed_acyclic_graph(dag)
            return {'nodes':len(graph),'edges':graph.number_of_edges(),'events':len(trajectory['nodes']),'currency_totals':{k:str(v) for k,v in totals['gross_by_currency'].items()},'patterns':len(detect_patterns(graph))}
        check('四流图谱、逐币种守恒和事件轨迹图',graph_checks)
        def paths():
            u,v,k,d=next(e for e in graph.edges(keys=True,data=True) if e[3]['relation']=='transfer' and e[0]!=e[1])
            result=temporal_paths(graph,u,v,currency=d['currency']);assert result['paths'];chain=evidence_chain(data,graph,result['paths'][0]);assert all(r['source_id'] and r['lineage_verified'] for r in chain)
            (run/'path_chain.json').write_text(json.dumps(chain,ensure_ascii=False,indent=2),encoding='utf-8');return {'paths':len(result['paths']),'first_chain_steps':len(chain),'truncated':result['truncated']}
        check('时序路径及逐步来源',paths)
        def network():
            result=network_analysis(graph);traces=industry_traces(data,graph);assert result['observations'] and traces
            return {'network_events':len(result['observations']),'ips':len(result['ips']),'orders':len(traces),'material_gaps':[{'order':t['order'],'missing':t['missing']} for t in traces]}
        check('IP网络与产业订单溯源',network)
        def projections():
            counts={}
            for method in ('PCA','LDA','LLE','t-SNE'):
                projection=project_graph(data,graph,method);assert all(np.isfinite(p['pc1']) and np.isfinite(p['pc2']) for p in projection['points']);counts[method]=len(projection['points'])
                write_csv(run/(method+'_coordinates.csv'),projection['points'],['node','pc1','pc2'])
            return {'projected_nodes':counts,'scope':'仅探索，LDA使用已知人员标签，不能当独立分类效果'}
        check('四种特征投影',projections)
        def models():
            nonlocal fund,roles,evaluation
            fund=TemporalHeteroGNN.load(resolve_model_directory(root));pointer=Path(json.loads((root/'runtime/models/roles_latest.json').read_text())['path']);roles=RoleHierarchyModel.load(pointer if pointer.is_absolute() else root/pointer)
            evaluation=evaluate_models(data,fund,roles);(run/'model_evaluation.json').write_text(json.dumps(evaluation,ensure_ascii=False,indent=2),encoding='utf-8')
            fund.save(run/'roundtrip_models/fund');roles.save(run/'roundtrip_models/roles');loaded=TemporalHeteroGNN.load(run/'roundtrip_models/fund');loaded_roles=RoleHierarchyModel.load(run/'roundtrip_models/roles')
            assert abs(fund.probability(graph)-loaded.probability(graph))<1e-12
            np.testing.assert_allclose(roles.probabilities(graph)[0],loaded_roles.probabilities(graph)[0],atol=1e-12)
            assert evaluation['selective']['unlabelled_persons']==evaluation['selective']['unlabelled_abstained'],'单次背景观测不应被强行赋予角色'
            errors=[r for r in evaluation['cases'] if r['fund_available'] and int(r['fund_score']>=.5)!=r['label']]
            role_errors=[r for r in evaluation['persons'] if r['eligible'] and not r['correct']]
            warning=bool(errors or role_errors) or evaluation['selective']['coverage']<.9
            return {'_status':'需关注' if warning else '通过','fund_f1':evaluation['fund'].get('f1'),'fund_confusion':evaluation['fund'].get('confusion'),'fund_error_cases':errors,'role_macro_f1':evaluation['roles'].get('macro_f1'),'role_errors':role_errors,'selective':evaluation['selective'],'note':'模拟指标和错例保留在model_evaluation.json；不能代表真实案件性能'}
        check('资金/人员模型输出、覆盖率及保存载入',models)
        if fund is not None and roles is not None:
            def perturbations():
                partial=graph.copy();partial.remove_edges_from([(u,v,k) for u,v,k,d in partial.edges(keys=True,data=True) if d['flow']=='information'])
                role_result=roles.predict(data,partial);assert role_result['eligible_count']==0;assert not fund.assess(data,partial)['available']
                stale=graph.copy();stale.graph['dataset_fingerprint']='OTHER';assert not fund.assess(data,stale)['available'];assert not roles.predict(data,stale)['available']
                scaled=graph.copy()
                for _,_,_,d in scaled.edges(keys=True,data=True):
                    if 'amount' in d:d['amount']*=Decimal(10)
                score_change=abs(fund.probability(scaled)-fund.probability(graph));assert score_change<1e-8
                renamed=nx.relabel_nodes(graph,{n:'RENAMED_'+n for n in graph});assert abs(fund.probability(renamed)-fund.probability(graph))<1e-9
                return {'missing_information_abstained':role_result['abstained_count'],'stale_snapshot_rejected':True,'same_currency_scale_score_delta':score_change,'identifier_renaming_invariant':True}
            check('缺流、错快照及金额/标识扰动',perturbations)
        def reviews():
            nonlocal graph
            pending=next(r for r in data.rows('identity_links',cid) if r['verified']=='false');before=graph_fingerprint(graph)
            store.review(data.fingerprint,cid,'identity:'+pending['link_id'],'已核实线索','合成自测：核对材料后当前确认','合成流程自测')
            graph=analysis_graph(data,cid,store);assert graph_fingerprint(graph)!=before
            historical=analysis_graph(data,cid,store,'2026-01-02T00:00:00+08:00');assert not any(d['record_id']==pending['link_id'] and d['relation']=='identity' for _,_,_,d in historical.edges(keys=True,data=True))
            store.review(data.fingerprint,cid,'identity:'+pending['link_id'],'已排除','合成自测：补充材料后排除','合成流程自测');graph=analysis_graph(data,cid,store)
            states=identity_states(data,graph,store);assert next(r for r in states if r['link_id']==pending['link_id'])['manual_review']['status']=='已排除'
            return {'source_annotation_preserved':pending['verified']=='false','future_review_not_in_history':True,'current_exclusion_effective':True}
        check('人工确认、排除及历史快照',reviews)
        def amounts():
            scenario=principal_scenario('100','.5','.8','USDT',['S001']);scenario['scope_as_of']=graph.graph.get('as_of','');store.save_scenario(data.fingerprint,cid,scenario)
            assert scenario['lower']=='125.00' and scenario['upper']=='200.00';return scenario
        check('本金条件情景及本地留痕',amounts)
        def reports():
            assessment=fund.assess(data,graph) if fund else None;person=roles.predict(data,graph) if roles else None
            report=export_report(data,graph,store,run/'reports',assessment=assessment,research={'roles':person})
            hashes=json.loads((report/'checksums.json').read_text());assert all(_sha(report/name)==digest for name,digest in hashes.items())
            with zipfile.ZipFile(report.with_suffix('.zip')) as archive:assert archive.testzip() is None
            empty=analysis_graph(data,cid,store,'2020-01-01');assert len(empty)==0;empty_report=export_report(data,empty,store,run/'reports')
            links.extend([('模拟个案完整报告',report/'report.html'),('空历史快照报告',empty_report/'report.html')])
            assert store.recent_audit();return {'case_report':str(report.relative_to(run)),'empty_report':str(empty_report.relative_to(run)),'checksummed_files':len(hashes),'audit_events':len(store.recent_audit())}
        check('报告、空快照、ZIP与审计日志',reports)
    if (root/'runtime/models/joint_latest.json').is_file() and (root/'data/advanced_sample_synthetic/manifest.json').is_file():
        def joint():
            from .dual_model import DualFlowModel
            from .hd_eia import quality_audit
            from .moid_xai import save_joint_report
            pointer=root/'runtime/models/joint_latest.json';before=pointer.read_bytes();path=Path(json.loads(before)['path']);model=DualFlowModel.load(path if path.is_absolute() else root/path)
            advanced=load_dataset(root/'data/advanced_sample_synthetic');cid=advanced.tables['cases'][0]['case_id'];advanced_graph=build_case_graph(advanced,cid);audit=quality_audit(advanced)
            report,prediction=save_joint_report(advanced,advanced_graph,model,run/'joint_reports')
            assert abs(prediction['explanation']['sum_residual'])<1e-6;assert before==pointer.read_bytes();assert all(p['prediction_available'] is False for p in prediction['predictions'] if p['actor_id']=='BYSTANDER')
            links.append(('双图/原始多模态/控制候选/多目标解释',report/'report.html'))
            return {'model_id':model.metadata['model_id'],'audited_cases':len(audit['cases']),'events':prediction['event_count'],'raw_materials':len(prediction['media']),'control_candidates':len(prediction['control_candidates']),'exact_flow_coalitions':16,'shapley_residual':prediction['explanation']['sum_residual'],'all_branches_trained':model.metadata['gradient_evidence'],'model_pointer_unchanged':True,'scope':'合成验证，控制边和视听表示不等于真实侦查事实'}
        check('双图、原始三模态、定向控制与MOID-XAI',joint)
    def causal():
        study=load_study(create_causal_demo(run/'causal_input',400,seed+17));result=estimate_study(study,3);assert result['available'] and abs(result['estimate']-1.5)<.35
        directory,result=save_causal_run(study,result,run/'causal_runs');links.append(('独立合成因果研究',directory/'report.html'))
        bad=copy.deepcopy(study);bad['design']['assumptions']['no_unmeasured_confounding']=False
        try:estimate_study(bad,0)
        except ValueError:pass
        else:raise AssertionError('未确认的识别假设不能执行估计')
        return {'known_effect':1.5,'estimate':result['estimate'],'interval':result['confidence_interval_95'],'interval_method':result['interval_method'],'unconfirmed_assumptions_rejected':True}
    check('因果设计、混杂调整及假设检查',causal)
    if include_media:
        def media():
            processor=LocalMediaProcessor(root);outputs=[];image=None
            for name in ('synthetic_record.png','synthetic_speech.wav','synthetic_scan.pdf','synthetic_silent_video.mp4'):
                result=processor.extract(root/'data/media_demo'/name,'synthetic');assert result['segments'];directory=save_extraction(result,run/'media');outputs.append({'file':name,'segments':len(result['segments']),'candidates':len(result['candidate_facts']),'output':str(directory.relative_to(run))})
                if name.endswith('.png'):image=result
            if data is not None:
                people=[r['actor_id'] for r in data.rows('actors',cid)];before=len(data.rows('communications',cid))
                draft=draft_communication(data,image,run/'reviewed_material_import',cid,people[0],people[1],'2026-01-01T10:00:00+08:00','合成流程自测','人工核对后的合成通信摘要')
                assert len(load_dataset(draft).rows('communications',cid))==before+1 and len(data.rows('communications',cid))==before
            return {'materials':outputs,'reviewed_import_creates_new_dataset':data is not None,'source_type':'synthetic'}
        check('图片/语音/PDF/视频及人工通信导入',media)
    summary={'version':'1.5.0','origin':'synthetic','seed':seed,'case_count':count,'created_at':datetime.now(TZ).isoformat(),'checks':checks,'passed':sum(r['status']=='通过' for r in checks),'warnings':sum(r['status']=='需关注' for r in checks),'failed':sum(r['status']=='失败' for r in checks),'media_included':include_media,'scope':'隔离的合成全流程模拟，不读写用户核验库、不修改生产数据或模型指针。功能通过不等于真实侦查效果达标。','model_evaluation_file':'model_evaluation.json' if evaluation else None,'links':[{'label':label,'path':str(path.relative_to(run))} for label,path in links]}
    (run/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');(run/'report.html').write_text(simulation_html(summary),encoding='utf-8')
    (run/'checksums.json').write_text(json.dumps({name:_sha(run/name) for name in ('result.json','report.html')},indent=2),encoding='utf-8');progress(f"流程模拟完成：{summary['passed']}项通过，{summary['warnings']}项需关注，{summary['failed']}项失败")
    return run,summary


def simulation_html(result):
    e=lambda value:html.escape(str(value),quote=True)
    rows=''.join('<tr><td>'+e(r['name'])+'</td><td class="'+('pass' if r['status']=='通过' else 'warn' if r['status']=='需关注' else 'fail')+'">'+e(r['status'])+'</td><td><pre>'+e(json.dumps(r['detail'],ensure_ascii=False,indent=2))+'</pre></td></tr>' for r in result['checks'])
    links=''.join('<li><a href="'+e(link['path'])+'">'+e(link['label'])+'</a></li>' for link in result['links'])
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:"><title>四流系统1.5 · 全流程模拟自测</title><style>body{{font:16px/1.65 'Microsoft YaHei UI',sans-serif;background:#f4f6fa;color:#172b44;max-width:1240px;margin:auto;padding:30px}}h1{{color:#173d79}}aside{{background:#fff0d7;padding:18px;border-radius:12px}}table{{width:100%;border-collapse:collapse;background:white;margin:24px 0}}th,td{{text-align:left;padding:16px;border-bottom:1px solid #dae3ee;vertical-align:top}}td:first-child{{width:25%}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font:13px/1.6 'Microsoft YaHei UI',sans-serif}}.pass{{color:#166534}}.warn{{color:#92400e}}.fail{{color:#b91c1c}}a{{color:#1d4ed8}}</style><h1>全流程合成模拟自测</h1><aside>{e(result['scope'])}</aside><p>时间：{e(result['created_at'])} · 种子 {result['seed']} · {result['case_count']}个合成案件</p><p>通过 {result['passed']} · 需关注 {result['warnings']} · 失败 {result['failed']}</p><ul>{links}</ul><p>完整模型分数、误报/漏报、未判定和标签比较见同目录model_evaluation.json。人工确认记录仅写本次simulation.sqlite3。原始脏数据、清洗报告、材料识别及研究输出均保留在本次目录。</p><table><thead><tr><th>板块/步骤</th><th>结果</th><th>实际输出与限制</th></tr></thead><tbody>{rows}</tbody></table><p>需求评估：结构化四流辅助研判主流程可据本次结果验收；真实跨地区/案型效果、自动法律或个案因果认定、原始视听语义效果及真实控制关系仍未验证，自主调证未实现，不能由这些合成测试替代。</p></html>'''
