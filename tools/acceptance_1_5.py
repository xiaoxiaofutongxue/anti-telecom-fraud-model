"""隔离验收1.5；输入均为显式合成材料，不改现用模型与核验库。"""
from pathlib import Path
from datetime import datetime,timedelta
from decimal import Decimal
import csv,hashlib,html,json,shutil,sys,traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fourflow.core import TZ,load_dataset,build_case_graph
from fourflow.io_utils import atomic_json,point_model
from fourflow.intake import read_table,suggest_columns,import_tables
from fourflow.store import Store
from fourflow.service import analysis_graph
from fourflow.empirical import load_model,evaluate_external,criteria_template,record_review
from fourflow.operations import backup_workspace,verify_backup,restore_backup,environment_check
from fourflow.orchestrator import run_analysis


def main():
    run=ROOT/'runtime/upgrade_1_5'/datetime.now(TZ).strftime('acceptance_%Y%m%d_%H%M%S_%f');run.mkdir(parents=True)
    checks=[];links=[];models={};pointers={};frozen={}
    for kind,name in [('fund','latest.json'),('roles','roles_latest.json'),('joint','joint_latest.json')]:
        pointers[name]=(ROOT/'runtime/models'/name).read_bytes();path=Path(json.loads(pointers[name])['path']);path=path if path.is_absolute() else ROOT/path
        models[kind]=load_model(path,kind);frozen[kind]={'directory':str(path),'sha256':hashlib.sha256((path/'weights.npz').read_bytes()).hexdigest(),'model_id':models[kind].metadata['model_id']}
    atomic_json(run/'frozen_models.json',frozen)
    def check(title,function):
        print(title,flush=True)
        try:detail=function() or {};status='passed'
        except Exception as exc:status='failed';detail={'error':str(exc),'traceback':traceback.format_exc()}
        checks.append({'title':title,'status':status,'detail':detail});atomic_json(run/'progress.json',checks)
    def intake():
        source=ROOT/'data/intake_demo/synthetic_transactions.xlsx';snapshot=read_table(source)
        result=import_tables([{'path':str(source),'sheet':snapshot['sheet'],'sha256':snapshot['sha256'],'table':'funds','columns':suggest_columns('funds',snapshot['headers'])}],run/'imported',{'case_id':'SIM_IMPORT','title':'合成Excel接入验收','region':'合成地区','case_type':'合成接入','origin':'synthetic'})
        assert result['valid'],result['errors'];data=load_dataset(run/'imported');assert data.tables['funds'][0]['from_id']=='000123'
        from fourflow.cleaning import clean_dataset
        cleaned=clean_dataset(data.root,run/'cleaned');assert cleaned['valid'];data=load_dataset(run/'cleaned')
        store=Store(run/'intake.sqlite3');graph=analysis_graph(data,'SIM_IMPORT',store)
        output,state=run_analysis(data,graph,store,run/'intake_workflow',models)
        from fourflow.analytics import amount_summary
        assert amount_summary(graph)['gross_by_currency']['CNY']==Decimal('180.01');assert state['status']=='complete' and state['record_coverage']==1
        assert state['next_actions'],'未定义任务不得当作已评分';links.append(('Excel接入后完整报告',str((output/'index.html').relative_to(run))))
        return {'records':2,'accounts_preserved':True,'amount':'180.01 CNY','tasks_undefined_explicitly_unscored':True,'original_sha256':snapshot['sha256']}
    check('Excel接入、清洗、来源与一键全流程',intake)
    def large():
        path=run/'large_synthetic.csv'
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f);w.writerow(['流水号','交易时间','付款账号','收款账号','金额','币种','账户域','转出类型','转入类型'])
            for i in range(350):w.writerow([f'SIM_L{i:04d}',(datetime(2026,1,1,8)+timedelta(seconds=i)).isoformat(),f'{i:06d}',f'{i+1:06d}','0.01','CNY','合成银行域','account','account'])
        s=read_table(path);result=import_tables([{'path':str(path),'sha256':s['sha256'],'table':'funds','columns':suggest_columns('funds',s['headers'])}],run/'large_data',{'case_id':'SIM_LARGE','title':'合成大图窗口验收','region':'合成地区','case_type':'合成大图','origin':'synthetic'});assert result['valid'],result['errors']
        data=load_dataset(run/'large_data');store=Store(run/'large.sqlite3');graph=analysis_graph(data,'SIM_LARGE',store);output,state=run_analysis(data,graph,store,run/'large_workflow')
        window=json.loads((output/'windows.json').read_text(encoding='utf-8'));assert state['record_coverage']==1 and not state['truncated'] and state['model_windows']>1
        from fourflow.analytics import amount_summary
        assert amount_summary(graph)['gross_by_currency']['CNY']==Decimal('3.50');assert all(r['nodes']<=256 for r in window['windows'])
        links.append(('351节点大图、完整金额与分窗覆盖',str((output/'index.html').relative_to(run))));return {'nodes':len(graph),'events':350,'windows':state['model_windows'],'event_coverage':1,'exact_total':'3.50 CNY','window_scores_not_aggregated':True}
    check('大图分窗、覆盖率与逐币种金额',large)
    def workflow():
        data=load_dataset(ROOT/'data/advanced_sample_synthetic');cid=data.tables['cases'][0]['case_id'];store=Store(run/'joint.sqlite3');output,state=run_analysis(data,analysis_graph(data,cid,store),store,run/'joint_workflow',models)
        row=json.loads((output/'windows.json').read_text(encoding='utf-8'))['windows'][0];assert row['joint']['available'];assert abs(row['joint']['explanation']['sum_residual'])<1e-6
        links.append(('双图联合研判与来源报告',str((output/'index.html').relative_to(run))));return {'status':state['status'],'joint_score':row['joint']['fund_score'],'control_candidates':len(row['joint']['control_candidates']),'shapley_residual':row['joint']['explanation']['sum_residual']}
    check('资金、人员、双图模型与解释全流程',workflow)
    def external():
        from fourflow.domain_validation import evaluate_domains
        sets={title:ROOT/'data'/folder for title,folder in [('未见地区','advanced_region_holdout_synthetic'),('未见案型','advanced_type_holdout_synthetic'),('地区案型同时未见','advanced_joint_holdout_synthetic')]}
        directory,result=evaluate_domains(models['joint'],sets,run/'domains');links.append(('冻结后72案合成域评测',str((directory/'report.html').relative_to(run))))
        empirical,result2=evaluate_external(models['joint'],load_dataset(sets['地区案型同时未见']),'joint',run/'empirical',criteria_template('joint'));links.append(('实证评估接口合成验证',str((empirical/'report.html').relative_to(run))))
        try:record_review(run,empirical,Path(frozen['joint']['directory']),'合成验收人员','验证禁止合成审批',True)
        except ValueError as exc:assert '合成验收' in str(exc)
        else:raise AssertionError('合成结果不应能登记真实实证通过')
        return {'cases':sum(r['metrics']['cases'] for r in result['runs']),'fund_errors':sum(len(r['metrics']['fund_errors']) for r in result['runs']),'synthetic_approval_rejected':True,'origin':'synthetic','scope':'同源生成机制，不能证明真实地区与案型泛化'}
    check('独立域评测、错例保留与合成审批阻断',external)
    def legacy():
        from fourflow.simulation import run_simulation
        directory,result=run_simulation(ROOT,run/'simulation',seed=1001,count=12,include_media=True,progress=lambda s:print(s,flush=True));assert result['failed']==0,result['checks'];links.append(('13环节与原始图片/语音/PDF/视频自测',str((directory/'report.html').relative_to(run))))
        return {'passed':result['passed'],'warnings':result['warnings'],'failed':result['failed'],'warnings_preserved':[r for r in result['checks'] if r['status']=='需关注']}
    check('既有全板块模拟与原始材料识别',legacy)
    def recovery():
        workspace=run/'private_workspace';workspace.mkdir();
        for folder in ('fourflow','tools','docs','.run'):shutil.copytree(ROOT/folder,workspace/folder,ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('app.py','cli.py','launch.pyw','启动系统.vbs','requirements.txt','requirements-lock.txt','README.md'):shutil.copyfile(ROOT/name,workspace/name)
        for i,(kind,pointer) in enumerate([('fund','latest.json'),('roles','roles_latest.json'),('joint','joint_latest.json')]):
            target=workspace/f'runtime/models/m{i}';shutil.copytree(frozen[kind]['directory'],target);point_model(workspace,pointer,target)
        shutil.copytree(ROOT/'runtime/models/whisper_base',workspace/'runtime/models/whisper_base')
        proof=workspace/'runtime/empirical_runs/fixture';proof.mkdir(parents=True);atomic_json(proof/'result.json',{'origin':'synthetic','scope':'恢复验收夹具'})
        db=Store(workspace/'runtime/analysis.sqlite3');data=load_dataset(ROOT/'data/advanced_sample_synthetic');cid=data.tables['cases'][0]['case_id'];db.review(data.fingerprint,cid,'acceptance15','待核验','合成恢复验收','合成验收人员')
        backup=backup_workspace(workspace,data,run/'backups');manifest=verify_backup(backup);restored=restore_backup(backup,run/'restored');assert (restored/'runtime/empirical_runs/fixture/result.json').is_file()
        restored_data=load_dataset(restored/'data/restored_dataset');assert restored_data.fingerprint==data.fingerprint;assert Store(restored/'runtime/analysis.sqlite3').reviews(data.fingerprint,cid)[0]['note']=='合成恢复验收'
        for kind,pointer in [('fund','latest.json'),('roles','roles_latest.json'),('joint','joint_latest.json')]:
            path=json.loads((restored/'runtime/models'/pointer).read_text())['path'];assert not Path(path).is_absolute();assert load_model(restored/path,kind).metadata['weights_sha256']==frozen[kind]['sha256']
        health=environment_check(restored,restored_data);assert health['ready'],health
        return {'backup_bytes':backup.stat().st_size,'verified_files':len(manifest['files']),'dataset_identical':True,'reviews_preserved':True,'all_three_models_load':True,'local_asr_weight_included':True,'empirical_reports_included':True,'existing_venv_used':True}
    check('完整私有备份、恢复、核验库及三模型加载',recovery)
    def freeze():
        for name,raw in pointers.items():assert (ROOT/'runtime/models'/name).read_bytes()==raw
        for record in frozen.values():assert hashlib.sha256((Path(record['directory'])/'weights.npz').read_bytes()).hexdigest()==record['sha256']
        return {'weights_and_pointers_unchanged':True}
    check('发布模型权重与当前引用未被验收改写',freeze)
    result={'version':'1.5.0','created_at':datetime.now(TZ).isoformat(),'origin':'synthetic','checks':checks,'passed':sum(r['status']=='passed' for r in checks),'failed':sum(r['status']=='failed' for r in checks),'links':links,'frozen_models':frozen,'fresh_dependency_install_tested':False,'scope':'本机工程及合成验收；真实标签、真实团伙控制恢复和真实跨地区/案型效果仍未验证。'}
    atomic_json(run/'result.json',result)
    e=lambda value:html.escape(str(value),quote=True)
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>1.5 单机实证准备验收</title><style>body{font:16px/1.8 Microsoft YaHei UI,sans-serif;max-width:1120px;margin:32px auto;padding:24px;color:#1e293b;background:#f4f6fa}section,table{background:white;border-radius:12px}section{padding:22px;margin:18px 0}aside{background:#fff0d7;padding:18px}table{width:100%;border-collapse:collapse}td,th{padding:12px;text-align:left;border-bottom:1px solid #dbe3ee}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#1d4ed8}</style><h1>1.5 单机实证准备与验收</h1><aside>'+e(result['scope'])+'</aside><p>'+e(result['created_at'])+f" · {result['passed']}项通过，{result['failed']}项失败</p><section><h2>可复核输出</h2><ul>"+''.join('<li><a href="'+e(path)+'">'+e(title)+'</a></li>' for title,path in links)+'</ul></section><table><tr><th>验收项</th><th>结果</th><th>实测明细</th></tr>'+''.join('<tr><td>'+e(r['title'])+'</td><td>'+('通过' if r['status']=='passed' else '失败')+'</td><td><pre>'+e(json.dumps(r['detail'],ensure_ascii=False,indent=2))+'</pre></td></tr>' for r in checks)+'</table><section><h2>未完成的验证</h2><p>本次没有真实数据，未训练真实业务模型，未证明真实控制关系、现实因果识别或跨地区泛化。未验证新机器全新安装；备份与搬移继续使用本机现有虚拟环境。</p><p>材料未提供HD-EIA/MOID-XAI原始公式，现有为明确定义的工程实现。APK、NFC、NeRF及外部自动取证不在单机CSV/Excel核心流程实现范围内。</p></section></html>'
    (run/'index.html').write_text(page,encoding='utf-8');print(json.dumps({'output':str(run),'passed':result['passed'],'failed':result['failed']},ensure_ascii=False),flush=True)
    atomic_json(ROOT/'runtime/upgrade_1_5/acceptance_latest.json',{'path':str(run)})
    if result['failed']:sys.exit(1)


if __name__=='__main__':main()
