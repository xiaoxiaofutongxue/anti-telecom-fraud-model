"""冻结模型的本地实证评测、分组区间及人工复核登记。"""
from pathlib import Path
from datetime import datetime
import hashlib,html,json
import numpy as np
from .core import TZ,load_dataset,build_case_graph,is_true
from .model import TemporalHeteroGNN,metrics,label_definition
from .roles import RoleHierarchyModel,role_task,classification_metrics
from .validation_groups import group_tokens
from .io_utils import atomic_json

EXAMPLE_CRITERIA={'min_cases':30,'min_positive_cases':10,'min_negative_cases':10,'min_independent_groups':10,'min_coverage':.8,'min_precision':.8,'min_recall':.8}
ROLE_CRITERIA={'min_cases':12,'min_labelled_persons':30,'min_independent_groups':10,'min_coverage':.8,'min_joint_accuracy':.8}
JOINT_CRITERIA={**EXAMPLE_CRITERIA,'min_role_macro_f1':.8,'min_level_macro_f1':.8,'min_person_coverage':.8,'min_control_precision':.8,'min_control_recall':.8}


def criteria_template(kind):return dict({'fund':EXAMPLE_CRITERIA,'roles':ROLE_CRITERIA,'joint':JOINT_CRITERIA}[kind])


def load_model(directory,kind):
    if kind=='fund':return TemporalHeteroGNN.load(directory)
    if kind=='roles':return RoleHierarchyModel.load(directory)
    if kind=='joint':
        from .dual_model import DualFlowModel
        return DualFlowModel.load(directory)
    raise ValueError('模型类型无效')


def _assets(dataset):
    return {n for case in dataset.tables['cases'] for n,d in build_case_graph(dataset,case['case_id']).nodes(data=True) if d['kind'] in {'wallet','account','ip'} and not (dataset.synthetic and d['entity_id'] in {'兑换服务地址','服务账户','收款账户'})}


def validate_external(dataset,model):
    m=model.metadata
    if m['origin']!=dataset.manifest['origin']:raise ValueError('实证数据与模型来源类型不一致，合成模型不能实证真实数据')
    if m.get('dataset_fingerprint')==dataset.fingerprint:raise ValueError('实证数据就是训练数据，须使用完全独立的留出数据')
    if m.get('label_definition') and label_definition(dataset.manifest)!=m['label_definition']:raise ValueError('资金预测任务定义不一致')
    if hasattr(model,'task') and role_task(dataset.manifest)!=model.task:raise ValueError('人员任务定义不一致')
    groups=group_tokens(dataset,required=not dataset.synthetic);used=set().union(*(set(v) for v in m.get('validation_groups',{}).values()))
    if not dataset.synthetic and not used:raise ValueError('模型缺少经核实的训练分组隔离信息，请使用1.5训练流程重新验证分组')
    if any(used.intersection(tokens) for tokens in groups.values()):raise ValueError('实证与训练/验证/内部测试共享已知案件、人员或组织分组')
    training_assets=set(m.get('training_assets',[]))|set().union(*(set(v) for v in m.get('split_entities',{}).values()))
    if _assets(dataset)&training_assets:raise ValueError('实证数据与训练/内部评估共享资产或IP')
    if m.get('training_media'):
        from .hd_eia import auxiliary
        if {r['sha256'] for r in auxiliary(dataset)['media']} & set(m['training_media']):raise ValueError('实证数据与训练共享原始媒体')
    return groups


def wilson(success,total):
    if not total:return None
    p=success/total;z=1.95996398454;den=1+z*z/total;center=(p+z*z/(2*total))/den;radius=z*np.sqrt(p*(1-p)/total+z*z/(4*total*total))/den
    return [float(max(0,center-radius)),float(min(1,center+radius))]


def _clusters(rows,groups):
    parent={r['case_id']:r['case_id'] for r in rows}
    def find(x):
        while parent[x]!=x:parent[x]=parent[parent[x]];x=parent[x]
        return x
    owners={}
    for cid,tokens in groups.items():
        if cid not in parent:continue
        for token in tokens:
            if token in owners:parent[find(cid)]=find(owners[token])
            owners[token]=cid
    clusters={}
    for r in rows:clusters.setdefault(find(r['case_id']),[]).append(r)
    return list(clusters.values())


def summarize(rows,groups,seed=1551):
    selected=[r for r in rows if r['label'] is not None];available=[r for r in selected if r['score'] is not None]
    if available:
        result=metrics([r['label'] for r in available],[r['score'] for r in available]);c=result['confusion']
        result['precision_wilson_case_level']=wilson(c['tp'],c['tp']+c['fp']);result['recall_wilson_case_level']=wilson(c['tp'],c['tp']+c['fn'])
    else:result={'n':0,'precision':None,'recall':None,'f1':None}
    clusters=_clusters(selected,groups);rng=np.random.default_rng(seed);values=[]
    if len(clusters)>=2:
        for _ in range(1000):
            sample=[r for index in rng.integers(0,len(clusters),len(clusters)) for r in clusters[index] if r['score'] is not None]
            if not sample or len({r['label'] for r in sample})<2:continue
            y=np.array([r['label'] for r in sample]);p=np.array([r['score']>=.5 for r in sample]);tp=int(((y==1)&p).sum());fp=int(((y==0)&p).sum());fn=int(((y==1)&~p).sum());values.append(2*tp/max(1,2*tp+fp+fn))
    result.update(labelled_cases=len(selected),available_cases=len(available),coverage=len(available)/max(1,len(selected)),positive_cases=sum(r['label']==1 for r in selected),negative_cases=sum(r['label']==0 for r in selected),independent_groups=len(clusters),f1_cluster_bootstrap_95=np.quantile(values,[.025,.975]).tolist() if values else None,usable_bootstrap=len(values),interval_scope='按声明关联分组整组重采样；Wilson按案件计算，组内相关时只作参考。零错误可能产生退化区间，不证明100%泛化。')
    return result


def assess_criteria(summary,criteria,kind='fund'):
    expected=criteria_template(kind)
    if not isinstance(criteria,dict) or set(criteria)!=set(expected):raise ValueError('验收标准字段必须与所选任务模板一致')
    for k,v in criteria.items():
        count=k in {'min_cases','min_positive_cases','min_negative_cases','min_independent_groups','min_labelled_persons'}
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not np.isfinite(v) or (count and (not isinstance(v,int) or v<1)) or (not count and not 0<=v<=1):raise ValueError('验收数量应为正整数，比例应在0至1之间')
    mapping={'min_cases':'labelled_cases','min_positive_cases':'positive_cases','min_negative_cases':'negative_cases','min_independent_groups':'independent_groups','min_labelled_persons':'labelled_persons',**{k:k[4:] for k in expected if k not in {'min_cases','min_positive_cases','min_negative_cases','min_independent_groups','min_labelled_persons'}}}
    checks=[{'criterion':k,'minimum':v,'actual':summary.get(mapping[k]),'passed':summary.get(mapping[k]) is not None and summary[mapping[k]]>=v} for k,v in criteria.items()]
    return {'passed':all(r['passed'] for r in checks),'checks':checks,'scope':'业务人员设定的本地验收规则；示例数值不是行业标准或法律结论'}


def evaluate_external(model,dataset,kind,output_root,criteria=None,progress=None):
    progress=progress or (lambda text:None);groups=validate_external(dataset,model);rows=[];persons=[];controls=[];case_labels={r['case_id']:int(r['label']) for r in dataset.tables['case_labels'] if is_true(r['confirmed'])}
    start_sha=model.metadata['weights_sha256'];role_y=[];role_p=[];level_y=[];level_p=[]
    if kind=='joint':
        from .hd_eia import auxiliary
        auxiliary_controls=auxiliary(dataset)['controls']
    for index,case in enumerate(dataset.tables['cases'],1):
        cid=case['case_id'];progress(f"实证 {index}/{len(dataset.tables['cases'])}：{cid}");graph=build_case_graph(dataset,cid)
        if kind=='fund':prediction=model.assess(dataset,graph);score=prediction.get('probability')
        else:
            try:prediction=model.predict(dataset,graph)
            except ValueError as exc:prediction={'available':False,'reason':str(exc)}
            score=prediction.get('fund_score') if kind=='joint' else None
            truth={r['actor_id']:r for r in dataset.rows('person_labels',cid) if is_true(r['confirmed'])}
            found={r['actor_id']:r for r in prediction.get('predictions',[])}
            for actor,target in truth.items():
                row=found.get(actor,{});active=row.get('prediction_available',False);right=active and row['role']==target['role_label'] and row['level']==target['level_label']
                persons.append({'case_id':cid,'actor_id':actor,'truth_role':target['role_label'],'truth_level':target['level_label'],'role':row.get('role','未判定'),'level':row.get('level','未判定'),'available':active,'correct':right if active else None,'reasons':row.get('reasons',row.get('abstention_reasons',[prediction.get('reason','')] ))})
                if row.get('role_distribution'):
                    role_y.append(model.task['roles'].index(target['role_label']));level_y.append(model.task['levels'].index(target['level_label']));role_p.append([row['role_distribution'][k] for k in model.task['roles']]);level_p.append([row['level_distribution'][k] for k in model.task['levels']])
            if kind=='joint':
                chosen={(graph.nodes[r['from']]['entity_id'],graph.nodes[r['to']]['entity_id']) for r in prediction.get('control_candidates',[])}
                for target in auxiliary_controls:
                    if target['case_id']==cid:controls.append({'case_id':cid,'from_actor':target['from_actor'],'to_actor':target['to_actor'],'label':int(target['label']),'selected':(target['from_actor'],target['to_actor']) in chosen,'available':prediction.get('available',False)})
        if prediction.get('known_split') or prediction.get('related_splits'):raise ValueError('实证样本与内部训练/评估记录相关，不能作为外部验证')
        rows.append({'case_id':cid,'region':case['region'],'case_type':case['case_type'],'label':case_labels.get(cid),'score':float(score) if score is not None else None,'available':prediction.get('available',False),'reason':prediction.get('reason','')})
    summary=summarize(rows,groups)
    if kind=='roles':summary=_role_summary(persons,groups)
    result={'version':'1.5.0','created_at':datetime.now(TZ).isoformat(),'model_id':model.metadata['model_id'],'model_weights_sha256':start_sha,'model_kind':kind,'origin':dataset.manifest['origin'],'dataset_path':str(dataset.root),'dataset_fingerprint':dataset.fingerprint,'summary':summary,'cases':rows,'persons':persons,'control_pairs':controls,'fund_errors':[r for r in rows if r['score'] is not None and r['label'] is not None and (r['score']>=.5)!=bool(r['label'])],'scope':'外部留出实证，未训练、校准或改变阈值。人工标签正确性仍需提供方核实。'}
    if role_y:result['raw_roles']=classification_metrics(role_y,np.array(role_p),len(model.task['roles']));result['raw_levels']=classification_metrics(level_y,np.array(level_p),len(model.task['levels']))
    result['person_coverage']=sum(r['available'] for r in persons)/len(persons) if persons else None
    if controls:
        tp=sum(r['label']==1 and r['selected'] for r in controls);fp=sum(r['label']==0 and r['selected'] for r in controls);fn=sum(r['label']==1 and not r['selected'] for r in controls);result['displayed_control']={'tp':tp,'fp':fp,'fn':fn,'precision':tp/max(1,tp+fp),'recall':tp/max(1,tp+fn),'f1':2*tp/max(1,2*tp+fp+fn)}
    summary.update(role_macro_f1=result.get('raw_roles',{}).get('macro_f1'),level_macro_f1=result.get('raw_levels',{}).get('macro_f1'),person_coverage=result['person_coverage'],control_precision=result.get('displayed_control',{}).get('precision'),control_recall=result.get('displayed_control',{}).get('recall'))
    result['criteria']=assess_criteria(summary,criteria,kind) if criteria else {'passed':False,'reason':'尚未设置业务验收标准'}
    result['subgroups']={field:{v:(_role_summary([p for p in persons if p['case_id'] in {r['case_id'] for r in rows if r[field]==v}],groups) if kind=='roles' else summarize([r for r in rows if r[field]==v],groups)) for v in sorted({r[field] for r in rows})} for field in ('region','case_type')}
    if model.metadata['weights_sha256']!=start_sha or load_dataset(dataset.root).fingerprint!=dataset.fingerprint:raise ValueError('评测过程中输入或模型版本发生变化')
    directory=Path(output_root)/datetime.now(TZ).strftime('empirical_%Y%m%d_%H%M%S_%f');directory.mkdir(parents=True,exist_ok=False);atomic_json(directory/'result.json',result)
    from .reporting import write_csv
    write_csv(directory/'case_scores.csv',rows,['case_id','region','case_type','label','score','available','reason']);write_csv(directory/'person_scores.csv',persons,['case_id','actor_id','truth_role','truth_level','role','level','available','correct','reasons']);write_csv(directory/'control_pairs.csv',controls,['case_id','from_actor','to_actor','label','selected'])
    e=lambda value:html.escape(str(value),quote=True)
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>单机实证评测</title><style>body{font:16px/1.8 Microsoft YaHei UI,sans-serif;max-width:1100px;margin:30px auto;padding:24px;color:#172b44}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6fa;padding:20px}aside{background:#fff0d7;padding:18px}</style><h1>冻结模型外部实证评测</h1><aside>来源：'+e(result['origin'])+'。'+e(result['scope'])+'</aside><h2>指标与覆盖率</h2><pre>'+e(json.dumps(summary,ensure_ascii=False,indent=2))+'</pre><h2>业务规则检查</h2><pre>'+e(json.dumps(result['criteria'],ensure_ascii=False,indent=2))+'</pre><p>完整错例、拒判、人员与控制对见同目录JSON/CSV。合成数据不能登记为真实业务实证通过。</p></html>'
    (directory/'report.html').write_text(page,encoding='utf-8');atomic_json(directory/'checksums.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()})
    return directory,result


def _role_summary(persons,groups):
    eligible=[r for r in persons if r['available']];correct=sum(bool(r['correct']) for r in eligible);case_ids={r['case_id'] for r in persons}
    return {'labelled_cases':len(case_ids),'labelled_persons':len(persons),'eligible_persons':len(eligible),'coverage':len(eligible)/max(1,len(persons)),'joint_accuracy':correct/len(eligible) if eligible else None,'correct_coverage':correct/len(persons) if persons else None,'independent_groups':len(_clusters([{'case_id':c} for c in case_ids],groups)),'interpretation':'同时报告可用候选正确率和全部标签覆盖率，不与资金二分类指标混用'}


def record_review(root,directory,model_directory,reviewer,note,accepted):
    if not reviewer.strip() or not note.strip() or type(accepted) is not bool:raise ValueError('请填写复核人和依据，明确通过或退回')
    directory=Path(directory).resolve();result=json.loads((directory/'result.json').read_text(encoding='utf-8'));hashes=json.loads((directory/'checksums.json').read_text())
    if not isinstance(hashes,dict) or 'result.json' not in hashes:raise ValueError('实证报告文件清单不完整')
    for name,digest in hashes.items():
        path=Path(name)
        if path.is_absolute() or path.drive or '..' in path.parts or not (directory/path).resolve().is_relative_to(directory):raise ValueError('实证报告清单路径越界')
        if hashlib.sha256((directory/path).read_bytes()).hexdigest()!=digest:raise ValueError('实证报告文件发生变化，不能登记通过')
    card=json.loads((Path(model_directory)/'model_card.json').read_text(encoding='utf-8'));sha=hashlib.sha256((Path(model_directory)/'weights.npz').read_bytes()).hexdigest()
    if sha!=result['model_weights_sha256'] or sha!=card['weights_sha256']:raise ValueError('实证报告与当前模型权重不一致')
    if accepted and (result['origin']!='real' or card['origin']!='real' or not result['criteria'].get('passed')):raise ValueError('只有真实外部数据且达到已声明业务标准，才能登记人工确认通过；合成验收不能代替')
    if accepted and load_dataset(result['dataset_path']).fingerprint!=result['dataset_fingerprint']:raise ValueError('实证数据已变化，须重新评测')
    proof=directory/'result.json';workspace=Path(root).resolve();proof_path=proof.relative_to(workspace).as_posix() if proof.is_relative_to(workspace) else str(proof)
    event={'created_at':datetime.now(TZ).isoformat(),'model_id':result['model_id'],'weights_sha256':sha,'origin':result['origin'],'accepted':accepted,'reviewer':reviewer.strip(),'note':note.strip(),'result_path':proof_path,'result_sha256':hashlib.sha256(proof.read_bytes()).hexdigest(),'dataset_fingerprint':result['dataset_fingerprint'],'scope':'本地业务人工复核记录，不是数字签章或法律/监管认证'}
    output=Path(root)/'runtime/deployments'/datetime.now(TZ).strftime('review_%Y%m%d_%H%M%S_%f.json');atomic_json(output,event);return output


def model_status(root,card):
    if card.get('origin')=='synthetic':return '合成演示模型'
    matches=[]
    for path in (Path(root)/'runtime/deployments').glob('review_*.json'):
        try:
            row=json.loads(path.read_text(encoding='utf-8'))
            if row['weights_sha256']==card['weights_sha256']:
                proof=Path(row['result_path'])
                if not proof.is_absolute():proof=Path(root)/proof
                if proof.is_file() and hashlib.sha256(proof.read_bytes()).hexdigest()==row['result_sha256']:matches.append(row)
        except (ValueError,OSError,KeyError):continue
    if not matches:return '真实模型：待外部实证复核'
    return '业务人员已确认实证通过' if max(matches,key=lambda r:r['created_at'])['accepted'] else '实证退回/待补充'
