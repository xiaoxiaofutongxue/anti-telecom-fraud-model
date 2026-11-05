"""声明DAG下的后门识别、分组交叉拟合AIPW、混杂调整及稳健性检查。"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from datetime import datetime

import networkx as nx
import numpy as np

from .core import TZ,parse_time

ASSUMPTIONS=('consistency','no_unmeasured_confounding','positivity','pre_treatment_covariates','independent_groups')


def validate_design(design,require_ack=True):
    if not isinstance(design,dict):raise ValueError('因果研究设计须为JSON对象')
    if not isinstance(design.get('assumptions',{}),dict):raise ValueError('assumptions须为假设声明对象')
    if design.get('schema_version')!=1 or design.get('origin') not in {'real','synthetic'} or not isinstance(design.get('task_id'),str) or not design['task_id'].strip():raise ValueError('因果研究须明确版本、来源和task_id')
    if require_ack and (not isinstance(design.get('acknowledged_by'),str) or not design['acknowledged_by'].strip() or any(design.get('assumptions',{}).get(k) is not True for k in ASSUMPTIONS)):raise ValueError('须由分析人员明确确认一致性、无未测混杂、重叠性、处理前协变量及组间独立假设，不能自动代为确认')
    dag=design.get('dag',{})
    if not isinstance(dag,dict):raise ValueError('因果图须为包含nodes和edges的对象')
    nodes=dag.get('nodes');edges=dag.get('edges')
    if not isinstance(nodes,list) or not 2<=len(nodes)<=50 or any(not isinstance(x,str) or not x.strip() for x in nodes) or len(nodes)!=len(set(nodes)) or not isinstance(edges,list) or any(not isinstance(e,list) or len(e)!=2 or e[0] not in nodes or e[1] not in nodes for e in edges):raise ValueError('因果图节点/边无效或超过50节点上限')
    graph=nx.DiGraph();graph.add_nodes_from(nodes);graph.add_edges_from(edges)
    if not nx.is_directed_acyclic_graph(graph):raise ValueError('因果图必须是有向无环图')
    treatment,outcome=design.get('treatment'),design.get('outcome');adjustment=design.get('adjustment')
    if not isinstance(treatment,str) or not isinstance(outcome,str) or not isinstance(adjustment,list) or any(not isinstance(a,str) for a in adjustment):raise ValueError('处理、结果和调整变量须为列名字符串')
    if treatment not in graph or outcome not in graph or treatment==outcome or not isinstance(adjustment,list) or len(adjustment)!=len(set(adjustment)) or any(a not in graph or a in {treatment,outcome} for a in adjustment):raise ValueError('处理、结果或调整变量无效')
    if len(adjustment)>12:raise ValueError('当前本地因果分析最多支持12个调整变量')
    reserved={'observation_id','group_id','covariate_time','treatment_time','outcome_time','source_id'}
    if reserved.intersection([*adjustment,treatment,outcome]):raise ValueError('研究变量不能使用编号、分组、时间或来源的保留列名')
    latent=design.get('unobserved_nodes',[])
    if not isinstance(latent,list) or any(v not in graph or v in {treatment,outcome} for v in latent):raise ValueError('未观测节点声明无效')
    if design.get('outcome_type','continuous') not in {'continuous','binary'}:raise ValueError('outcome_type只能是continuous或binary')
    if set(adjustment).intersection(design.get('unobserved_nodes',[])):raise ValueError('不能调整未观测变量')
    if set(adjustment).intersection(nx.descendants(graph,treatment)):raise ValueError('调整集合含处理后变量/中介，不能作为总效应的后门调整')
    backdoor=graph.copy();backdoor.remove_edges_from(list(backdoor.out_edges(treatment)))
    if not nx.is_d_separator(backdoor,{treatment},{outcome},set(adjustment)):raise ValueError('声明的调整集合未阻断全部后门路径，当前设计不能识别该总效应')
    return graph


def load_study(directory,require_ack=True):
    root=Path(directory).resolve();design_path=root/'causal_design.json';design=json.loads(design_path.read_text(encoding='utf-8-sig'));validate_design(design,require_ack)
    digest=hashlib.sha256(design_path.read_bytes());sources={}
    source_path=root/'sources.csv';digest.update(source_path.read_bytes())
    with source_path.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        if not {'source_id','title','reference'}.issubset(reader.fieldnames or []) or len(reader.fieldnames)!=len(set(reader.fieldnames)):raise ValueError('因果研究sources.csv缺少来源列或列名重复')
        for row in reader:
            sid=(row.get('source_id') or '').strip();ref=(row.get('reference') or '').strip()
            if None in row or not sid or sid in sources or not ref or not (row.get('title') or '').strip():raise ValueError('研究来源编号重复、缺失、引用/标题为空或多余列值')
            if not ref.startswith('ref:'):
                path=(root/ref).resolve()
                if Path(ref).is_absolute() or Path(ref).drive or not path.is_relative_to(root):raise ValueError('因果研究材料须为研究目录内相对路径')
                if not path.is_file():raise ValueError('研究来源材料缺失；无法附文件时须明确使用ref:引用')
                digest.update(hashlib.sha256(path.read_bytes()).digest())
            sources[sid]=row
    path=root/'observations.csv';digest.update(path.read_bytes());rows=[];seen=set()
    variables=[*design['adjustment'],design['treatment'],design['outcome']]
    required={'observation_id','group_id','covariate_time','treatment_time','outcome_time','source_id',*variables}
    with path.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        if not required.issubset(reader.fieldnames or []) or len(reader.fieldnames)!=len(set(reader.fieldnames)):raise ValueError('observations.csv缺少观测编号、分组、三阶段时间、来源、研究变量或列名重复')
        for raw in reader:
            try:
                if None in raw:raise ValueError('观测存在多余列值')
                row={k:(raw.get(k) or '').strip() for k in required}
                if not row['observation_id'] or row['observation_id'] in seen or not row['group_id']:raise ValueError('观测编号重复或分组缺失')
                if row['source_id'] not in sources:raise ValueError('观测缺少有效来源')
                times=[parse_time(row[k]) for k in ('covariate_time','treatment_time','outcome_time')]
                if not times[0]<=times[1]<times[2]:raise ValueError('须满足协变量测量≤处理时间<结果时间，防止未来信息或处理后调整')
                values={k:float(row[k]) for k in variables}
                if not all(np.isfinite(v) for v in values.values()):raise ValueError('研究变量须为有限数字，不自动补缺失值')
                if values[design['treatment']] not in {0.,1.}:raise ValueError('当前估计器要求二元0/1处理')
                if design.get('outcome_type','continuous')=='binary' and values[design['outcome']] not in {0.,1.}:raise ValueError('二元结果须为0/1')
                row.update(values);row['_line']=reader.line_num;rows.append(row);seen.add(row['observation_id'])
            except (ValueError,TypeError) as exc:raise ValueError(f'observations.csv:{reader.line_num} {exc}') from exc
            if len(rows)>50000:raise ValueError('当前本地因果研究上限50000条，请分研究批次处理')
    if len(rows)<50 or len({r['group_id'] for r in rows})<10:raise ValueError('因果研究至少需要50条观测和10个独立组')
    treatment=np.array([r[design['treatment']] for r in rows])
    if min((treatment==0).sum(),(treatment==1).sum())<10:raise ValueError('处理组和对照组均至少需要10条观测')
    x=np.array([[r[k] for k in design['adjustment']] for r in rows],dtype=float).reshape(len(rows),len(design['adjustment']))
    return {'root':root,'design':design,'rows':rows,'sources':sources,'fingerprint':digest.hexdigest(),'x':x,'t':treatment,'y':np.array([r[design['outcome']] for r in rows]),'groups':np.array([r['group_id'] for r in rows])}


def _features(train,test):
    if train.shape[1]==0:return np.ones((len(train),1)),np.ones((len(test),1))
    mean=train.mean(axis=0);scale=np.maximum(train.std(axis=0),1e-6)
    def basis(value):
        z=np.clip((value-mean)/scale,-10,10)
        return np.column_stack([np.ones(len(value)),z,z*z,*[z[:,i]*z[:,j] for i in range(z.shape[1]) for j in range(i+1,z.shape[1])]])
    return basis(train),basis(test)


def _logistic(x,y,test):
    beta=np.zeros(x.shape[1]);penalty=np.eye(len(beta))*1e-4;penalty[0,0]=0
    p0=np.clip(y.mean(),1e-5,1-1e-5);beta[0]=np.log(p0/(1-p0))
    if len(np.unique(y))==1:return np.full(len(test),p0)
    for _ in range(60):
        p=1/(1+np.exp(-np.clip(x@beta,-30,30)));w=np.maximum(p*(1-p),1e-7)
        gradient=x.T@(p-y)/len(y)+penalty@beta
        hessian=(x.T*w)@x/len(y)+penalty+np.eye(len(beta))*1e-8
        step=np.linalg.solve(hessian,gradient);norm=np.linalg.norm(step)
        beta-=step*min(1,5/max(1e-12,norm))
        if norm<1e-7:break
    return 1/(1+np.exp(-np.clip(test@beta,-30,30)))


def _outcome(x,y,test,binary):
    if binary:return _logistic(x,y,test)
    penalty=np.eye(x.shape[1])*1e-4;penalty[0,0]=0
    return test@np.linalg.solve(x.T@x+penalty,x.T@y)


def cross_fit(x,t,y,groups,binary=False,seed=731):
    unique=np.unique(groups);rng=np.random.default_rng(seed);rng.shuffle(unique)
    fold_groups=np.array_split(unique,5);e=np.empty(len(t));m0=np.empty(len(t));m1=np.empty(len(t));folds=np.empty(len(t),dtype=int)
    for fold,held in enumerate(fold_groups):
        test=np.isin(groups,held);train=~test
        if min(int((t[train]==0).sum()),int((t[train]==1).sum()))<5:raise ValueError('分组交叉拟合某折缺少足够处理/对照，请增加独立组')
        a,b=_features(x[train],x[test]);tt=t[train];yy=y[train]
        e[test]=_logistic(a,tt,b);m0[test]=_outcome(a[tt==0],yy[tt==0],b,binary);m1[test]=_outcome(a[tt==1],yy[tt==1],b,binary);folds[test]=fold
    clipped=np.clip(e,1e-6,1-1e-6)
    psi=m1-m0+t*(y-m1)/clipped-(1-t)*(y-m0)/(1-clipped)
    if not all(np.isfinite(v).all() for v in (e,m0,m1,psi)):raise ValueError('因果估计数值不稳定，请复核变量尺度、样本重叠与极端值')
    return {'e':e,'m0':m0,'m1':m1,'psi':psi,'folds':folds,'estimate':float(psi.mean()),'numerical_clipping_count':int((e!=clipped).sum())}


def estimate_study(study,refutations=12):
    if not isinstance(refutations,int) or not 0<=refutations<=100:raise ValueError('置换检查次数须为0至100')
    d=study['design'];validate_design(d)
    x,t,y,groups=(study[k] for k in ('x','t','y','groups'));binary=d.get('outcome_type','continuous')=='binary'
    fitted=cross_fit(x,t,y,groups,binary);e=fitted['e'];bounded=np.clip(e,1e-6,1-1e-6)
    w1=t/bounded;w0=(1-t)/(1-bounded)
    ess=[float(w.sum()**2/max(1e-12,(w*w).sum())) for w in (w0,w1)]
    counts=np.array([int((groups==g).sum()) for g in np.unique(groups)]);effective_groups=float(len(t)**2/(counts*counts).sum())
    extreme=float(((e<.02)|(e>.98)).mean());balance=[]
    for j,name in enumerate(d['adjustment']):
        scale=max(float(np.sqrt((x[t==1,j].var()+x[t==0,j].var())/2)),1e-8)
        before=float((x[t==1,j].mean()-x[t==0,j].mean())/scale)
        after=float((np.average(x[:,j],weights=w1)-np.average(x[:,j],weights=w0))/scale)
        balance.append({'variable':name,'smd_before':before,'smd_after':after})
    result={'available':extreme<=.1 and min(ess)>=10,'task_id':d['task_id'],'origin':d['origin'],'study_fingerprint':study['fingerprint'],'question':d.get('question',''),'n':len(t),'independent_groups':len(np.unique(groups)),'treatment':d['treatment'],'outcome':d['outcome'],'adjustment':d['adjustment'],'dag':d['dag'],'assumptions':d['assumptions'],'acknowledged_by':d['acknowledged_by'],'identification':'在声明DAG与识别假设成立条件下，调整集合满足总效应后门准则','method':'5折按组交叉拟合AIPW；逻辑倾向模型与分处理组结果模型；正交影响函数；分组稳健近似标准误','diagnostics':{'propensity_min':float(e.min()),'propensity_max':float(e.max()),'extreme_fraction':extreme,'effective_sample_size_control':ess[0],'effective_sample_size_treated':ess[1],'numerical_clipping_count':fitted['numerical_clipping_count'],'balance':balance},'naive_difference':float(y[t==1].mean()-y[t==0].mean()),'scope':'队列总体平均效应；不是当前个案或个人的犯罪因果认定','warnings':[]}
    result['diagnostics'].update(effective_group_count=effective_groups,largest_group_share=float(counts.max()/len(t)))
    result['cross_fit_assignments']=[{'observation_id':row['observation_id'],'group_id':row['group_id'],'fold':int(fitted['folds'][i])} for i,row in enumerate(study['rows'])]
    result['available']=result['available'] and effective_groups>=8
    if not result['available']:
        result['reason']='样本重叠、有效样本量或有效独立组数不足，仅输出诊断，不给出可靠总体效应结论';return result
    theta=fitted['estimate'];influence=fitted['psi']-theta;unique=np.unique(groups)
    cluster=np.array([influence[groups==g].sum() for g in unique]);se=float(np.sqrt(len(unique)/(len(unique)-1)*(cluster*cluster).sum()/len(t)**2))
    ipw=float(np.mean(t*y/bounded-(1-t)*y/(1-bounded)));gformula=float((fitted['m1']-fitted['m0']).mean())
    from scipy.stats import t as student_t
    critical=float(student_t.ppf(.975,len(unique)-1))
    result.update(estimate=theta,standard_error=se,confidence_interval_95=[theta-critical*se,theta+critical*se],interval_method='分组稳健标准误 × t临界值；自由度=独立组数-1，仍为近似区间',interval_degrees_of_freedom=len(unique)-1,interval_critical_value=critical,g_formula=gformula,ipw=ipw,confounding_adjustment_delta=theta-result['naive_difference'])
    if len(unique)<30 or effective_groups<15:result['warnings'].append('独立组数较少或组大小不均衡，即使用t临界值区间仍可能不可靠，应增加独立组并复核设计')
    if binary and abs(theta)>1:result['warnings'].append('二元结果的AIPW点估计超出风险差理论范围[-1,1]，需复核重叠和模型，不能视为可直接采纳的效果')
    if any(abs(r['smd_after'])>.1 for r in balance):result['warnings'].append('调整后仍有协变量不平衡，不能把去偏估计视为偏差已消除')
    if fitted['numerical_clipping_count']:result['warnings'].append('极端倾向分数使用数值截断，可能引入额外偏差')
    rng=np.random.default_rng(941);placebos=[];failures=[]
    for at in range(refutations):
        try:placebos.append(cross_fit(x,rng.permutation(t),y,groups,binary)['estimate'])
        except (ValueError,np.linalg.LinAlgError) as exc:failures.append({'replicate':at+1,'reason':str(exc)})
    random_covariate=cross_fit(np.column_stack([x,rng.normal(size=len(y))]),t,y,groups,binary)['estimate']
    omitted=[]
    for j,name in enumerate(d['adjustment']):
        altered=np.delete(x,j,axis=1);value=cross_fit(altered,t,y,groups,binary)['estimate']
        omitted.append({'omitted_variable':name,'estimate_without_adjustment_variable':value,'change':value-theta,'interpretation':'刻意遗漏调整变量的敏感性检查；不能替代满足后门准则的主估计'})
    result['refutations']={'placebo_permutation_estimates':placebos,'placebo_mean':float(np.mean(placebos)) if placebos else None,'random_covariate_estimate':random_covariate,'random_covariate_change':random_covariate-theta,'leave_one_adjustment_out':omitted,'interpretation':'置换与随机协变量为稳健性压力检查，不是无未测混杂的证明；这里不把置换结果当作因果成立检验'}
    result['refutations'].update(requested_permutations=refutations,failed_permutations=failures)
    if failures:result['warnings'].append(f'{len(failures)}次置换未满足拟合条件，保留失败原因，没有把失败当作零效应')
    result['cross_fit_assignments']=[{'observation_id':row['observation_id'],'group_id':row['group_id'],'fold':int(fitted['folds'][i])} for i,row in enumerate(study['rows'])]
    result['warnings'].append('置信区间依赖识别、模型及独立组假设，不能证明这些假设；未测混杂仍可能使结果有偏')
    if d['origin']=='synthetic':result['warnings'].append('全部为合成观测，不代表真实反电诈因果效果')
    return result


def save_causal_run(study,result,output_root):
    if any(result.get(k)!=study['design'].get(k) for k in ('task_id','origin','treatment','outcome')) or result.get('study_fingerprint')!=study['fingerprint']:raise ValueError('研究结果与当前设计、来源或数据快照不一致，不能保存混合版本')
    root=Path(output_root);root.mkdir(parents=True,exist_ok=True)
    timestamp=datetime.now(TZ);run=root/timestamp.strftime('causal_%Y%m%d_%H%M%S_%f');run.mkdir()
    previous=[]
    for path in sorted(root.glob('causal_*/result.json'))[-10:]:
        if path.parent==run:continue
        try:
            old=json.loads(path.read_text(encoding='utf-8'))
            if all(old.get(key)==result.get(key) for key in ('task_id','origin','treatment','outcome')):previous.append({'created_at':old.get('created_at'),'study_fingerprint':old.get('study_fingerprint'),'adjustment':old.get('adjustment'),'estimate':old.get('estimate'),'naive_difference':old.get('naive_difference'),'available':old.get('available')})
        except (ValueError,OSError):continue
    result={**result,'created_at':timestamp.isoformat(),'previous_runs':previous,'history_interpretation':'数据、声明假设或调整集变化后重新估计并留版本；不宣称自动修正全部因果偏差'}
    (run/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (run/'design.json').write_text(json.dumps(study['design'],ensure_ascii=False,indent=2),encoding='utf-8')
    from .reporting import write_csv
    write_csv(run/'balance.csv',result['diagnostics']['balance'],['variable','smd_before','smd_after'])
    write_csv(run/'cross_fit.csv',result.get('cross_fit_assignments',[]),['observation_id','group_id','fold'])
    write_csv(run/'sources.csv',list(study['sources'].values()),['source_id','title','reference'])
    from .causal_rendering import causal_html
    (run/'report.html').write_text(causal_html(result),encoding='utf-8')
    (run/'checksums.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in run.iterdir() if p.is_file()},indent=2),encoding='utf-8')
    return run,result


def create_causal_template(root):
    root=Path(root)
    if root.exists() and any(root.iterdir()):raise ValueError('因果模板须生成到新的空目录')
    root.mkdir(parents=True,exist_ok=True)
    design={'schema_version':1,'origin':'real','task_id':'replace_with_reviewed_study_id','question':'请定义队列、二元处理和后续结果','treatment':'treatment','outcome':'outcome','outcome_type':'continuous','adjustment':['baseline'],'dag':{'nodes':['baseline','treatment','outcome'],'edges':[['baseline','treatment'],['baseline','outcome'],['treatment','outcome']]},'unobserved_nodes':[],'assumptions':{k:False for k in ASSUMPTIONS},'acknowledged_by':''}
    (root/'causal_design.json').write_text(json.dumps(design,ensure_ascii=False,indent=2),encoding='utf-8')
    for name,fields in [('observations',['observation_id','group_id','covariate_time','treatment_time','outcome_time','source_id','baseline','treatment','outcome']),('sources',['source_id','title','reference'])]:
        with (root/(name+'.csv')).open('w',encoding='utf-8-sig',newline='') as stream:csv.writer(stream).writerow(fields)
    (root/'填写说明.txt').write_text('本目录为真实队列研究空模板，没有合成或真实观测。\n请先由业务人员定义研究变量、因果图及调整集，不要机械保留示例DAG。\n处理须为0/1；结果为有限数字或在outcome_type=binary时为0/1。\n同一相关主体/单位/家庭的重复观测应使用同一group_id；须至少50条、10个独立组、两组各至少10条。\n协变量测量时间≤处理时间<结果时间；缺失值不自动填充。\nsources引用目录内材料或明确的ref:引用。复核后由分析人员逐项确认假设并填写姓名；程序不自动代确认。\n识别假设不充分、未测混杂或组间干扰会使估计有偏。\n',encoding='utf-8')
    return root
