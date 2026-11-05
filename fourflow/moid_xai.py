"""MOID-XAI工程版：多目标Pareto、偏好稳定性、四流精确Shapley及原始来源。

解释模型在遮蔽输入时如何变化，不解释违法事实或因果效应。
"""
import copy,math
from datetime import datetime
from pathlib import Path
import hashlib,html,json,zipfile
import numpy as np
import torch
from .core import FLOWS,TZ,parse_time
from .hd_eia import prepare,mask_untrained_modalities
from .model import graph_fingerprint
from .investigation import industry_traces,network_analysis


def pareto_rank(rows,weights=None):
    keys=('coverage','traceability','uncertainty','impact')
    weights=weights or dict(zip(keys,(.3,.3,.2,.2)))
    if set(weights)!=set(keys) or any(not isinstance(v,(int,float)) or not np.isfinite(v) or v<0 for v in weights.values()) or sum(weights.values())<=0:raise ValueError('多目标权重须为四个非负有限值，且不能全零')
    weights={k:v/sum(weights.values()) for k,v in weights.items()};result=copy.deepcopy(rows)
    for r in result:
        if set(r['objectives'])!=set(keys) or any(not np.isfinite(v) or not 0<=v<=1 for v in r['objectives'].values()):raise ValueError('目标值须为0至1有限值')
        r['weighted_priority']=sum(weights[k]*r['objectives'][k] for k in keys);r['dominated_by']=[]
    for i,row in enumerate(result):
        for j,other in enumerate(result):
            if i!=j and all(other['objectives'][k]>=row['objectives'][k] for k in keys) and any(other['objectives'][k]>row['objectives'][k] for k in keys):row['dominated_by'].append(other['id'])
        row['pareto_front']=not row['dominated_by']
    # 权重每项分别增/减20%，记录名次范围，避免把某次偏好当作唯一决策。
    scenarios=[weights]
    for key in keys:
        for factor in (.8,1.2):
            changed={k:v*(factor if k==key else 1) for k,v in weights.items()};scenarios.append(changed)
    ranks={r['id']:[] for r in result}
    for w in scenarios:
        ordered=sorted(result,key=lambda r:(not r['pareto_front'],-sum(w[k]*r['objectives'][k] for k in keys),r['id']))
        for i,r in enumerate(ordered):ranks[r['id']].append(i+1)
    for r in result:r['rank_range']=[min(ranks[r['id']]),max(ranks[r['id']])]
    return {'weights':weights,'candidates':sorted(result,key=lambda r:(not r['pareto_front'],-r['weighted_priority'],r['id'])),'interpretation':'优先级为人工核验资源分配偏好，不是违法概率或自动处置建议；四目标均为越大越优先。'}


def explain_joint(model,item,dataset,graph):
    model.network.eval();flows=list(FLOWS);cache={};base=graph.copy()
    # 固定完整输入的参考时刻，防止删除某一流后其他事件突然变新。
    if not base.graph.get('as_of'):base.graph['as_of']=max(d['timestamp'] for _,_,_,d in graph.edges(keys=True,data=True) if d.get('verified')!='pending')
    def score(mask):
        if mask in cache:return cache[mask]
        if not mask:
            with torch.no_grad():value=float((model.network.fund(torch.zeros(model.hidden*2)).squeeze()/model.temperatures['fund']).sigmoid())
        else:
            sub=base.copy();sub.remove_edges_from([(u,v,k) for u,v,k,d in sub.edges(keys=True,data=True) if not mask&(1<<flows.index(d['flow']))])
            tensor=mask_untrained_modalities(prepare(dataset,sub),model.metadata.get('trained_modalities',['image','audio','video']))
            with torch.no_grad():value=float((model.network(tensor)['fund']/model.temperatures['fund']).sigmoid())
        cache[mask]=value;return value
    for mask in range(16):score(mask)
    shapley={}
    for i,flow in enumerate(flows):
        shapley[flow]=sum(math.factorial(mask.bit_count())*math.factorial(3-mask.bit_count())/24*(cache[mask|(1<<i)]-cache[mask]) for mask in range(16) if not mask&(1<<i))
    with torch.no_grad():original=model.network(item);baseline=float((original['fund']/model.temperatures['fund']).sigmoid())
    if abs(cache[15]-baseline)>1e-5:raise ValueError('解释基准与模型输入不一致')
    ablations=[]
    for branch in ('entity','trajectory','interaction','media','image','audio','video','text'):
        with torch.no_grad():value=float((model.network(item,branch)['fund']/model.temperatures['fund']).sigmoid())
        ablations.append({'branch':branch,'score_after_mask':value,'delta':baseline-value})
    differentiable=dict(item);differentiable['x']=item['x'].detach().clone().requires_grad_(True)
    out=model.network(differentiable);gradient=torch.autograd.grad(out['fund'],differentiable['x'])[0]
    saliency=(gradient*differentiable['x']).abs().sum(1).detach().numpy();order=np.argsort(-saliency)[:10]
    salient=[]
    for index in order:
        node=item['nodes'][int(index)];sources=[]
        for table,rid in sorted(item['support'][node]):
            event=next(r for r in item['events'] if r['table']==table and r['record_id']==rid);sources.append({k:event[k] for k in ('table','record_id','source_id','csv_line','timestamp')})
        salient.append({'node':node,'label':graph.nodes[node]['label'],'gradient_times_input':float(saliency[index]),'sources':sources})
    return {'method':'精确四流Shapley(16子集)+八支路遮蔽+节点梯度乘输入','baseline':baseline,'empty_prior':cache[0],'flow_shapley':shapley,'coalition_scores':{str(k):v for k,v in cache.items()},'sum_residual':float(sum(shapley.values())-(cache[15]-cache[0])),'branch_ablation':ablations,'salient_nodes':salient,'limitations':'空图使用学习到的零池化先验。删除流会改变输入分布；Shapley、梯度及注意力是模型解释，不是因果、控制或违法证明。'}


def multiobjective_leads(dataset,graph,prediction,weights=None):
    rows=[]
    for i,edge in enumerate(prediction.get('control_candidates',[])):
        evidence=edge['evidence'];flows={r['table'] for r in evidence};score=edge['score']
        rows.append({'id':'CTRL'+str(i+1),'target':'人员联系','question':'核验定向联系 '+edge['from']+' → '+edge['to'],'objectives':{'coverage':min(1,len(flows)/4),'traceability':1. if evidence and all(r['source_id'] for r in evidence) else 0.,'uncertainty':1-abs(2*score-1),'impact':score},'sources':evidence})
    for t in industry_traces(dataset,graph):
        rows.append({'id':'ORDER_'+t['order'],'target':'产业','question':'补充订单来源 '+t['order'],'objectives':{'coverage':len(t['flows'])/4,'traceability':min(1,len(t['source_ids'])/3),'uncertainty':min(1,len(t['missing'])/4),'impact':t['priority_score']/100},'sources':t['steps']})
    from .analytics import detect_patterns
    for i,r in enumerate([r for r in detect_patterns(graph) if r['kind'] not in {'shared_ip','multi_ip'}][:20]):
        rows.append({'id':'FUND'+str(i+1),'target':'资金','question':'复核资金模式 '+r['title'],'objectives':{'coverage':.25,'traceability':min(1,len(r['source_ids'])/3),'uncertainty':.8,'impact':min(1,len(r['record_ids'])/8)},'sources':[{'record_ids':r['record_ids'],'source_ids':r['source_ids'],'detail':r['detail']}]})
    network=network_analysis(graph)
    for i,row in enumerate(network['ips'][:20]):
        records=[r for r in network['observations'] if r['ip_node']==row['ip_node']]
        rows.append({'id':'IP'+str(i+1),'target':'信息/IP','question':'核验IP '+row['ip']+'（'+row['scope']+'）的业务背景','objectives':{'coverage':.25,'traceability':min(1,len(row['source_ids'])/3),'uncertainty':1.,'impact':min(1,row['event_count']/6)},'sources':records})
    if not rows:rows=[{'id':'GAP','target':'补证','question':'当前输入不足，先补齐来源及观测','objectives':dict(zip(('coverage','traceability','uncertainty','impact'),(0.,0.,1.,0.))),'sources':[]}]
    return pareto_rank(rows,weights)


def save_joint_report(dataset,graph,model,output_root,weights=None):
    prediction=model.predict(dataset,graph,explain=True)
    if not prediction.get('available'):raise ValueError(prediction['reason'])
    prediction['multiobjective']=multiobjective_leads(dataset,graph,prediction,weights)
    # 对应文件或图变化后不能把旧结果另存成新快照。
    from .hd_eia import auxiliary
    if prediction['aux_fingerprint']!=auxiliary(dataset)['fingerprint'] or prediction['graph_fingerprint']!=graph_fingerprint(graph):raise ValueError('联合报告输入已变化，请重新运行')
    directory=Path(output_root)/datetime.now(TZ).strftime('joint_%Y%m%d_%H%M%S_%f');directory.mkdir(parents=True,exist_ok=False)
    (directory/'result.json').write_text(json.dumps(prediction,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    from .reporting import write_csv
    write_csv(directory/'control_candidates.csv',prediction['control_candidates'],['from','to','score','status','evidence'])
    write_csv(directory/'person_candidates.csv',prediction['predictions'],['actor_id','alias','role','level','score','prediction_available','reasons','candidate_role','candidate_level'])
    write_csv(directory/'multiobjective.csv',prediction['multiobjective']['candidates'],['id','target','question','weighted_priority','pareto_front','rank_range','objectives','sources'])
    e=lambda s:html.escape(str(s),quote=True)
    def table(headers,rows):return '<table><tr>'+''.join('<th>'+e(h)+'</th>' for h in headers)+'</tr>'+''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</table>'
    people=table(['主体','角色候选','层级候选','分数','暂不分类原因'],[[r['alias'],r['role'],r['level'],f"{r['score']:.3f}" if r['prediction_available'] else '暂不输出','；'.join(r['reasons'])] for r in prediction['predictions']])
    edges=table(['方向候选','模型分数','观测依据数'],[[r['from']+' → '+r['to'],f"{r['score']:.3f}",len(r['evidence'])] for r in prediction['control_candidates']])
    flow=table(['流','对分数的Shapley贡献'],[[FLOWS[k],f'{v:+.5f}'] for k,v in prediction['explanation']['flow_shapley'].items()])
    targets=table(['核验问题','优先级','Pareto前沿','偏好扰动名次范围'],[[r['question'],f"{r['weighted_priority']:.3f}",r['pareto_front'],r['rank_range']] for r in prediction['multiobjective']['candidates']])
    page=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>双图与多模态联合研判</title><style>body{{font:16px/1.7 'Microsoft YaHei UI',sans-serif;background:#f4f6fa;color:#172b44;max-width:1150px;margin:auto;padding:30px}}section{{background:white;padding:24px;margin:20px 0;border-radius:12px}}aside{{background:#fff0d7;padding:18px}}table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{padding:10px;text-align:left;border-bottom:1px solid #dbe3ee;overflow-wrap:anywhere}}th{{background:#eef4ff}}</style><h1>1.5 双图与原始多模态联合研判</h1><aside>{e(prediction['scope'])} · 来源 {e(prediction['origin'])}。本项目独立定义HD-EIA/MOID-XAI工程实现，材料未提供可核对的原算法公式。</aside><section>模型 {e(prediction['model_id'])}<br>资金目标模式分数 {prediction['fund_score']:.4f}；事件 {prediction['event_count']}，原始媒体 {len(prediction['media'])} 份；未见域字段 {e(prediction['unseen_domain_fields'])}。<p>完整来源、媒体抽样范围、分数和限制见同目录result.json。</p></section><section><h2>人员候选</h2>{people}</section><section><h2>有监督定向控制候选</h2>{edges}<p>每条边仍需独立业务核验；无边不等于无控制。环抑制只是候选展示约束。</p></section><section><h2>四流模型解释</h2>{flow}<p>{e(prediction['explanation']['limitations'])}</p></section><section><h2>多目标核验排序</h2>{targets}<p>{e(prediction['multiobjective']['interpretation'])}</p></section></html>'''
    (directory/'report.html').write_text(page,encoding='utf-8')
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}
    (directory/'checksums.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')
    with zipfile.ZipFile(directory.with_suffix('.zip'),'w',zipfile.ZIP_DEFLATED) as archive:
        for p in directory.iterdir():archive.write(p,p.name)
    return directory,prediction
