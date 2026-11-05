"""本地任务编排：质量、图谱、四流分析、模型、报告；无外部调证或自动处置。"""
from pathlib import Path
from datetime import datetime
import hashlib,html,json,traceback
from .core import TZ,load_dataset
from .io_utils import atomic_json
from .model import graph_fingerprint
from .service import review_signature
from .windows import plan_windows


class Cancelled(Exception):pass


def run_analysis(dataset,graph,store,output_root,models=None,cancel=None,progress=None):
    models=models or {};progress=progress or (lambda text:None)
    output_root=Path(output_root).resolve()
    if output_root.is_relative_to(dataset.root.resolve()):raise ValueError('编排输出须放在原始数据目录之外')
    directory=output_root/datetime.now(TZ).strftime('analysis_%Y%m%d_%H%M%S_%f');directory.mkdir(parents=True,exist_ok=False)
    state={'version':'1.5.0','origin':dataset.manifest['origin'],'case_id':graph.graph['case_id'],'dataset_fingerprint':dataset.fingerprint,'graph_fingerprint':graph_fingerprint(graph),'status':'running','stages':[],'scope':'本地辅助分析任务编排，不联网、不自动取得外部证据、不自动认定违法。'}
    def stop():
        if cancel is not None and cancel.is_set():raise Cancelled('已在安全步骤边界停止，未发布未完成结果')
    def step(name,function):
        stop();row={'name':name,'status':'running'};state['stages'].append(row);atomic_json(directory/'run.json',state);progress(name)
        try:value=function();row.update(status='passed');atomic_json(directory/'run.json',state);return value
        except Exception as exc:row.update(status='failed',error=str(exc));raise
    try:
        step('核对输入及人工核验快照',lambda:_snapshot(dataset,graph,store))
        from .analytics import amount_summary,detect_patterns
        from .investigation import network_analysis,industry_traces
        amounts=step('逐币种精确金额与质量提示',lambda:amount_summary(graph));diagnostics={}
        findings=step('资金/IP模式与截断检查',lambda:detect_patterns(graph,diagnostics=diagnostics))
        network=step('IP与来源关联',lambda:network_analysis(graph));traces=step('产业订单与材料缺口',lambda:industry_traces(dataset,graph))
        plan=step('规划完整事件窗口',lambda:plan_windows(graph));scores=[]
        direct=0<len(graph)<=256 and len({(d['table'],d['record_id']) for _,_,_,d in graph.edges(keys=True,data=True) if d.get('verified')!='pending'})<=256
        work=[{'index':1,'graph':graph,'events':plan['total_records'],'nodes':len(graph)}] if direct else plan['windows']
        for window in work:
            stop();progress(f"模型窗口 {window['index']}/{len(work)}")
            result={'index':window['index'],'events':window['events'],'nodes':window['nodes'],'graph_fingerprint':graph_fingerprint(window['graph'])}
            for name,model in models.items():
                if model is None:continue
                try:
                    if name=='fund':result[name]=model.assess(dataset,window['graph'])
                    elif name=='joint':
                        result[name]=model.predict(dataset,window['graph'],explain=True)
                        if result[name].get('available'):
                            from .moid_xai import multiobjective_leads
                            result[name]['multiobjective']=multiobjective_leads(dataset,window['graph'],result[name])
                    else:result[name]=model.predict(dataset,window['graph'])
                except (ValueError,OSError,KeyError) as exc:result[name]={'available':False,'reason':str(exc)}
            scores.append(result)
        state['model_windows']=len(scores);atomic_json(directory/'windows.json',{'plan':{k:v for k,v in plan.items() if k!='windows'},'whole_case_model':direct,'windows':scores})
        first=scores[0] if direct and scores else {};research={'roles':first.get('roles'),'joint':first.get('joint')}
        from .reporting import export_report
        report=step('导出完整统计和来源报告',lambda:export_report(dataset,graph,store,directory/'reports',assessment=first.get('fund'),research=research))
        step('再次核对数据与核验状态',lambda:_snapshot(dataset,graph,store))
        state.update(status='complete',report=str(report.relative_to(directory))+'/report.html',window_report='windows.json',record_coverage=plan['coverage'],truncated=plan['truncated'],finding_count=len(findings),industry_orders=len(traces),ip_count=len(network['ips']),pattern_diagnostics=diagnostics)
        state['next_actions']=[]
        if not any(models.values()):state['next_actions'].append('未提供模型；当前已完成可追溯统计与关联分析')
        if not work:state['next_actions'].append('当前快照没有可评分的事件，未产生模型分数')
        if diagnostics.get('search_complete') is False:state['next_actions'].append('模式搜索或候选列表达到上限，请缩小时间范围复查；未显示不代表不存在')
        for row in scores:
            for name,result in row.items():
                if isinstance(result,dict) and result.get('available') is False:state['next_actions'].append(f"窗口{row['index']} {name}：{result.get('reason','不可用')}")
        if plan['truncated']:state['next_actions'].append('窗口达到上限，仍有原始事件未评分；报告保留覆盖率')
        atomic_json(directory/'run.json',state)
        e=lambda value:html.escape(str(value),quote=True)
        table=[]
        for row in scores:
            fund=row.get('fund',{});roles=row.get('roles',{});joint=row.get('joint',{});value=fund.get('probability',joint.get('fund_score','暂不输出'))
            table.append('<tr>'+''.join('<td>'+e(v)+'</td>' for v in [row['index'],row['nodes'],row['events'],value,roles.get('eligible_count','—'),len(joint.get('control_candidates',[])) if joint.get('available') else '—','；'.join(r.get('reason','') for r in (fund,roles,joint) if r.get('available') is False)])+'</tr>')
        page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>本地全流程分析</title><style>body{font:16px/1.8 Microsoft YaHei UI,sans-serif;max-width:1200px;margin:30px auto;padding:24px;color:#172b44}aside{background:#fff0d7;padding:18px}table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:10px;border-bottom:1px solid #dbe3ee;text-align:left}li{overflow-wrap:anywhere}</style><h1>本地全流程分析</h1><aside>数据来源：'+e(state['origin'])+'。'+e(state['scope'])+'</aside><p>已覆盖事件 '+str(plan['covered_records'])+'/'+str(plan['total_records'])+'；模型窗口 '+str(len(scores))+'。'+e(plan['interpretation'])+'</p><p><a href="'+e(state['report'])+'">完整统计、图谱、来源与人工核验报告</a> · <a href="windows.json">全部窗口评分与原因JSON</a></p><table><tr>'+''.join('<th>'+s+'</th>' for s in ['窗口','节点','事件','资金模式分数','可用人员','控制候选','未评分原因'])+'</tr>'+''.join(table)+'</table><h2>待办与限制</h2><ul>'+''.join('<li>'+e(s)+'</li>' for s in state['next_actions'])+'</ul></html>'
        (directory/'index.html').write_text(page,encoding='utf-8')
        atomic_json(directory/'checksums.json',{p.relative_to(directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file() and p.name!='checksums.json'})
        return directory,state
    except Cancelled:
        state.update(status='cancelled');atomic_json(directory/'run.json',state);raise
    except Exception as exc:
        state.update(status='failed',error=str(exc));atomic_json(directory/'run.json',state);raise


def _snapshot(dataset,graph,store):
    if graph.graph.get('dataset_fingerprint')!=dataset.fingerprint or load_dataset(dataset.root).fingerprint!=dataset.fingerprint:raise ValueError('数据文件在载入后变化，请重新导入')
    reviews=store.reviews(dataset.fingerprint,graph.graph['case_id'],graph.graph.get('as_of') or None);signature=graph.graph.get('review_signature')
    if (signature and signature!=review_signature(reviews)) or (reviews and not signature):raise ValueError('人工核验发生变化，请刷新后重新运行')
    return True
