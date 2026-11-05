"""双头人员角色/层级图神经网络。所有卷积层和分类头均参与训练。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import numpy as np

from .core import TZ,FLOWS,build_case_graph,entity_id,is_true,parse_time
from .model import TemporalHeteroGNN,FEATURE_NAMES,CHANNELS,ENCODER_VERSION,encode_graph,graph_fingerprint
from .model_quality import calibration_summary,flow_reference,actor_reference,validate_reference


def role_task(manifest):
    task=manifest.get('role_task')
    if not isinstance(task,dict) or not isinstance(task.get('task_id'),str) or not task['task_id'].strip():raise ValueError('manifest须明确role_task.task_id及角色、层级类别')
    for field,minimum,maximum in [('roles',2,20),('levels',2,8)]:
        values=task.get(field)
        if not isinstance(values,list) or not minimum<=len(values)<=maximum or any(not isinstance(x,str) or not x.strip() for x in values) or len(values)!=len(set(values)):raise ValueError('角色/层级类别须为不重复的非空字符串列表')
    pairs=task.get('compatible_pairs')
    if not isinstance(pairs,list) or not pairs or any(not isinstance(pair,list) or len(pair)!=2 or pair[0] not in task['roles'] or pair[1] not in task['levels'] for pair in pairs):raise ValueError('须明确允许的角色与层级组合compatible_pairs')
    if len({tuple(pair) for pair in pairs})!=len(pairs):raise ValueError('允许的角色与层级组合不能重复，避免重复累计组合分数')
    if {p[0] for p in pairs}!=set(task['roles']) or {p[1] for p in pairs}!=set(task['levels']):raise ValueError('每个角色和层级至少须出现在一种允许组合中')
    return {k:task[k] for k in ('task_id','roles','levels','compatible_pairs')}


def softmax(logits):
    z=logits-logits.max(axis=1,keepdims=True);p=np.exp(z)
    return p/p.sum(axis=1,keepdims=True)


def classification_metrics(y,p,classes):
    y=np.asarray(y,dtype=int);pred=np.argmax(p,axis=1)
    matrix=np.zeros((classes,classes),dtype=int)
    for a,b in zip(y,pred):matrix[a,b]+=1
    f1=[]
    for c in range(classes):
        tp=matrix[c,c];fp=matrix[:,c].sum()-tp;fn=matrix[c].sum()-tp
        f1.append(float(2*tp/max(1,2*tp+fp+fn)))
    return {'n':len(y),'accuracy':float((pred==y).mean()),'macro_f1':float(np.mean(f1)),'confusion':matrix.tolist(),'nll':float(-np.log(np.maximum(p[np.arange(len(y)),y],1e-12)).mean()),'calibration':calibration_summary(y,p)}


class RoleHierarchyModel:
    def __init__(self,task,hidden=16,seed=51):
        self.task=role_task({'role_task':task});self.hidden=hidden
        self.network=TemporalHeteroGNN(hidden,seed);self.params=self.network.params
        rng=np.random.default_rng(seed+1)
        for head,count in [('role',len(task['roles'])),('level',len(task['levels']))]:
            self.params[head+'_w']=rng.normal(0,.12,(hidden,count));self.params[head+'_b']=np.zeros(count)
        self.role_temperature=1.;self.level_temperature=1.;self.metadata={}

    def forward(self,tensor):
        _,cache=self.network.forward(tensor);h=np.maximum(cache[6],0)
        return h@self.params['role_w']+self.params['role_b'],h@self.params['level_w']+self.params['level_b'],cache

    def gradient(self,tensor,indices,roles,levels,role_weights=None,level_weights=None,label_smoothing=0.):
        r,l,cache=self.forward(tensor);p=softmax(r);q=softmax(l)
        indices=np.asarray(indices,dtype=int);roles=np.asarray(roles);levels=np.asarray(levels);n=len(indices)
        rw=np.ones(n) if role_weights is None else np.asarray(role_weights)[roles]
        lw=np.ones(n) if level_weights is None else np.asarray(level_weights)[levels]
        if not 0<=label_smoothing<=.2:raise ValueError('标签平滑须为0至0.2')
        rt=np.eye(p.shape[1])[roles]*(1-label_smoothing)+label_smoothing/p.shape[1]
        lt=np.eye(q.shape[1])[levels]*(1-label_smoothing)+label_smoothing/q.shape[1]
        def logsoftmax(value):
            z=value-value.max(axis=1,keepdims=True);return z-np.log(np.exp(z).sum(axis=1,keepdims=True))
        loss=float((-np.sum(rt*logsoftmax(r[indices]),axis=1)*rw-.7*np.sum(lt*logsoftmax(l[indices]),axis=1)*lw).mean())
        dr=np.zeros_like(p);dl=np.zeros_like(q)
        dr[indices]=(p[indices]-rt)*rw[:,None]/n
        dl[indices]=(q[indices]-lt)*.7*lw[:,None]/n
        x,adj,ax,z1,h1,ah,z2,_=cache;h2=np.maximum(z2,0)
        grads={key:np.zeros_like(value) for key,value in self.params.items()}
        for head,d in [('role',dr),('level',dl)]:grads[head+'_w']=h2.T@d;grads[head+'_b']=d.sum(axis=0)
        dz2=(dr@self.params['role_w'].T+dl@self.params['level_w'].T)*(z2>0)
        grads['self2']=h1.T@dz2;grads['bias2']=dz2.sum(axis=0);dh1=dz2@self.params['self2'].T
        for rel,a in adj.items():
            grads['r2_'+rel]=ah[rel].T@dz2;dh1+=a.T@(dz2@self.params['r2_'+rel].T)
        dz1=dh1*(z1>0);grads['self1']=x.T@dz1;grads['bias1']=dz1.sum(axis=0)
        for rel in adj:grads['r1_'+rel]=ax[rel].T@dz1
        return loss,grads

    def probabilities(self,graph):
        r,l,_=self.forward(encode_graph(graph))
        if not np.isfinite(r).all() or not np.isfinite(l).all():raise ValueError('角色模型计算数值不稳定，请检查输入和模型')
        return softmax(r/self.role_temperature),softmax(l/self.level_temperature)

    def predict(self,dataset,graph):
        if not self.metadata.get('trained'):return {'available':False,'reason':'角色层级模型尚未训练'}
        if self.metadata['origin']=='synthetic' and not dataset.synthetic:return {'available':False,'reason':'合成角色模型不对真实数据评分，须使用真实核实标签重新训练'}
        try:
            if role_task(dataset.manifest)!=self.task:return {'available':False,'reason':'角色层级任务定义与模型不一致'}
        except ValueError as exc:return {'available':False,'reason':str(exc)}
        if not 0<len(graph)<=256:return {'available':False,'reason':'角色模型支持1至256节点的有效图'}
        if not any(d.get('verified')!='pending' for _,_,_,d in graph.edges(keys=True,data=True)):return {'available':False,'reason':'当前图没有可用于推理的有效关联，不输出角色分类'}
        if graph.graph.get('dataset_fingerprint')!=dataset.fingerprint:return {'available':False,'reason':'角色分析图与数据版本不一致'}
        p,q=self.probabilities(graph);raw,_=encode_graph(graph);nodes=sorted(graph);predictions=[];levels={}
        reference=self.metadata.get('actor_reference');required=set(self.metadata.get('input_reference',{}).get('required_flows',[]));observed={d['flow'] for _,_,_,d in graph.edges(keys=True,data=True) if d.get('verified')!='pending'}
        missing=sorted(required-observed)
        allowed=[(self.task['roles'].index(a),self.task['levels'].index(b)) for a,b in self.task['compatible_pairs']]
        for i,node in enumerate(nodes):
            if graph.nodes[node]['kind']!='actor':continue
            joint=np.array([p[i,a]*q[i,b] for a,b in allowed]);mass=float(joint.sum());joint/=max(1e-12,mass);choice=int(joint.argmax());a,b=allowed[choice]
            inconsistent=(int(p[i].argmax()),int(q[i].argmax())) not in allowed
            incident=[d for u,v,k,d in graph.edges(keys=True,data=True) if node in (u,v) and d.get('verified')!='pending']
            support=len({(d['table'],d['record_id']) for d in incident});support_flows=sorted({d['flow'] for d in incident});reasons=[]
            if support<2:reasons.append('有效关联记录不足2条')
            if missing:reasons.append('缺少训练共同具备的'+ '、'.join(FLOWS[f] for f in missing))
            shift=float(np.max(np.abs((raw[i]-np.array(reference['mean']))/np.array(reference['scale'])))) if reference else None
            if reference and shift>reference['threshold']:reasons.append('人员特征超出训练/验证输入参考范围')
            if float(joint[choice])<.6 or mass<.2 or inconsistent:reasons.append('组合分数偏低或双分类头冲突')
            eligible=not reasons
            predictions.append({'node':node,'actor_id':graph.nodes[node]['entity_id'],'alias':graph.nodes[node]['label'],'role':self.task['roles'][a] if eligible else '未判定','level':self.task['levels'][b] if eligible else '未判定','level_index':b if eligible else None,'candidate_role':self.task['roles'][a],'candidate_level':self.task['levels'][b],'prediction_available':eligible,'abstention_reasons':reasons,'support_records':support,'support_flows':support_flows,'feature_shift':shift,'role_probability':float(p[i,a]),'level_probability':float(q[i,b]),'joint_probability':float(joint[choice]),'allowed_mass':mass,'incompatible_heads':inconsistent,'low_confidence':not eligible,'role_distribution':dict(zip(self.task['roles'],map(float,p[i]))),'level_distribution':dict(zip(self.task['levels'],map(float,q[i]))),'status':'模型候选，待人工核验' if eligible else '证据不足/输入偏离，暂不分类'})
            if eligible:levels[node]=b
        links={}
        for u,v,k,d in graph.edges(keys=True,data=True):
            if u==v or u not in levels or v not in levels or d.get('verified')=='pending' or d['relation'] not in {'communicate','actor_relation'} or levels[u]==levels[v]:continue
            upper,lower=(u,v) if levels[u]<levels[v] else (v,u)
            row=links.setdefault((upper,lower),{'upper':upper,'lower':lower,'record_ids':set(),'source_ids':set(),'status':'层级预测与已有联系构成的候选，不证明指挥或控制关系'})
            row['record_ids'].add(d['record_id']);row['source_ids'].add(d['source_id'])
        candidates=[{**r,'record_ids':sorted(r['record_ids']),'source_ids':sorted(r['source_ids'])} for r in links.values()]
        digest=graph_fingerprint(graph);known=next((n for n,items in self.metadata.get('graph_fingerprints',{}).items() if digest in items),None)
        assets={n for n,d in graph.nodes(data=True) if d['kind'] in {'wallet','account','ip'} and not(dataset.synthetic and d['entity_id'] in {'兑换服务地址','服务账户','收款账户'})}
        related=[n for n,items in self.metadata.get('split_entities',{}).items() if assets.intersection(items)]
        return {'available':True,'model_id':self.metadata['model_id'],'origin':self.metadata['origin'],'graph_fingerprint':digest,'dataset_fingerprint':dataset.fingerprint,'task':self.task,'predictions':predictions,'hierarchy_links':candidates,'known_split':known,'related_splits':related,'eligible_count':sum(r['prediction_available'] for r in predictions),'abstained_count':sum(not r['prediction_available'] for r in predictions),'missing_flows':missing,'input_reference_available':bool(reference),'interpretation':'角色/层级为监督模型候选；证据不足、缺流、输入偏离或分数冲突时暂不分类，内部候选分布仍保留供复核。输入偏离是启发式检查，不保证识别所有未知场景。上下层候选不等于犯罪、指挥或控制关系。'}

    def save(self,directory):
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
        np.savez_compressed(directory/'weights.npz',**self.params,feature_mean=self.network.mean,feature_scale=self.network.scale,role_temperature=np.array(self.role_temperature),level_temperature=np.array(self.level_temperature))
        self.metadata.update(kind='role_hierarchy_gnn',hidden=self.hidden,role_task=self.task,feature_names=FEATURE_NAMES,relation_channels=CHANNELS,encoder_version=ENCODER_VERSION,weights_sha256=hashlib.sha256((directory/'weights.npz').read_bytes()).hexdigest())
        (directory/'model_card.json').write_text(json.dumps(self.metadata,ensure_ascii=False,indent=2),encoding='utf-8')

    @classmethod
    def load(cls,directory):
        directory=Path(directory);m=json.loads((directory/'model_card.json').read_text(encoding='utf-8'))
        validate_reference(m,len(FEATURE_NAMES),FLOWS)
        if m.get('kind')!='role_hierarchy_gnn' or m.get('encoder_version')!=ENCODER_VERSION or m.get('feature_names')!=FEATURE_NAMES or m.get('relation_channels')!=CHANNELS or not isinstance(m.get('hidden'),int) or not 1<=m['hidden']<=128:raise ValueError('角色模型结构或编码版本不兼容')
        if m.get('origin') not in {'real','synthetic'} or not m.get('model_id') or not m.get('trained'):raise ValueError('角色模型卡缺少来源、训练状态或模型编号')
        if not {'metrics','epochs','selected_epoch','splits','limitations'}.issubset(m):raise ValueError('角色模型卡缺少训练评估字段')
        if hashlib.sha256((directory/'weights.npz').read_bytes()).hexdigest()!=m.get('weights_sha256'):raise ValueError('角色模型权重校验失败')
        model=cls(role_task(m),m['hidden'])
        with np.load(directory/'weights.npz',allow_pickle=False) as archive:
            for key in model.params:
                value=archive[key]
                if value.shape!=model.params[key].shape or not np.isfinite(value).all():raise ValueError('角色模型参数形状或数值无效')
                model.params[key]=value.copy()
            model.network.mean=archive['feature_mean'].copy();model.network.scale=archive['feature_scale'].copy()
            model.role_temperature=float(archive['role_temperature']);model.level_temperature=float(archive['level_temperature'])
        if model.network.mean.shape!=(len(FEATURE_NAMES),) or model.network.scale.shape!=model.network.mean.shape or not np.isfinite(model.network.mean).all() or not np.isfinite(model.network.scale).all() or (model.network.scale<=0).any() or not all(np.isfinite(t) and t>0 for t in (model.role_temperature,model.level_temperature)):raise ValueError('角色模型归一化或温度参数无效')
        model.metadata=m;return model


def train_roles(dataset,output,epochs=100,progress=None):
    if not isinstance(epochs,int) or not 1<=epochs<=500:raise ValueError('训练轮数须为1至500')
    progress=progress or (lambda message:None);task=role_task(dataset.manifest)
    rows=[r for r in dataset.tables.get('person_labels',[]) if is_true(r['confirmed'])]
    if len(rows)<30 or len({r['case_id'] for r in rows})<12:raise ValueError('角色训练至少需要12个案件、30个人员，具有经核实角色/层级标签')
    allowed={tuple(pair) for pair in task['compatible_pairs']};by_case={}
    for row in rows:
        if (row['role_label'],row['level_label']) not in allowed:raise ValueError('人员标签不在允许的角色/层级组合内')
        by_case.setdefault(row['case_id'],[]).append(row)
    graphs={cid:build_case_graph(dataset,cid) for cid in by_case}
    if any(not g.number_of_edges() for g in graphs.values()):raise ValueError('角色标签不能对应空图')
    ordered=sorted(graphs,key=lambda c:min(d['timestamp'] for _,_,_,d in graphs[c].edges(keys=True,data=True)))
    a,b=int(len(ordered)*.6),int(len(ordered)*.8);splits={'train':ordered[:a],'validation':ordered[a:b],'test':ordered[b:]}
    from .validation_groups import check_group_splits
    validation_groups=check_group_splits(dataset,splits)
    ranges={n:{'start':min(d['timestamp'] for c in ids for _,_,_,d in graphs[c].edges(keys=True,data=True)),'end':max([d['timestamp'] for c in ids for _,_,_,d in graphs[c].edges(keys=True,data=True)]+[r['_time'].isoformat() for c in ids for r in by_case[c]])} for n,ids in splits.items()}
    if ranges['train']['end']>=ranges['validation']['start'] or ranges['validation']['end']>=ranges['test']['start']:raise ValueError('角色训练的事件/标签可用时间跨越验证或测试区间')
    assets={};seen={}
    for name,ids in splits.items():
        for field in ('role_label','level_label'):
            expected=set(task['roles'] if field=='role_label' else task['levels'])
            if {r[field] for c in ids for r in by_case[c]}!=expected:raise ValueError(f'{name}集缺少某些角色/层级类别，请补充标签')
        assets[name]=set()
        for cid in ids:
            for node,data in graphs[cid].nodes(data=True):
                if data['kind'] not in {'wallet','account','ip'} or (dataset.synthetic and data['entity_id'] in {'兑换服务地址','服务账户','收款账户'}):continue
                if node in seen and seen[node]!=name:raise ValueError('角色训练不同集合共享地址/账户或同域IP，须按关联案件分组')
                seen[node]=name;assets[name].add(node)
    tensors={c:encode_graph(g) for c,g in graphs.items()};targets={}
    for cid,g in graphs.items():
        index={n:i for i,n in enumerate(sorted(g))};rr=by_case[cid]
        if any(entity_id('actor',r['actor_id']) not in index for r in rr):raise ValueError('某个人员标签没有对应的有效事件节点')
        targets[cid]=([index[entity_id('actor',r['actor_id'])] for r in rr],[task['roles'].index(r['role_label']) for r in rr],[task['levels'].index(r['level_label']) for r in rr])
    model=RoleHierarchyModel(task);x=np.concatenate([tensors[c][0] for c in splits['train']]);model.network.mean=x.mean(axis=0);model.network.scale=np.maximum(x.std(axis=0),.1)
    weights=[]
    for at,size in [(1,len(task['roles'])),(2,len(task['levels']))]:
        labels=[v for c in splits['train'] for v in targets[c][at]];counts=np.bincount(labels,minlength=size);weights.append(len(labels)/(size*counts))
    first={k:np.zeros_like(v) for k,v in model.params.items()};second={k:np.zeros_like(v) for k,v in model.params.items()}
    best=None;best_loss=float('inf');history=[];best_epoch=0
    def evaluate(ids,temperatures=True):
        rp=[];lp=[];yr=[];yl=[]
        for c in ids:
            r,l,_=model.forward(tensors[c]);indices,roles,levels=targets[c]
            rp.extend(softmax(r/(model.role_temperature if temperatures else 1))[indices]);lp.extend(softmax(l/(model.level_temperature if temperatures else 1))[indices]);yr.extend(roles);yl.extend(levels)
        return classification_metrics(yr,np.array(rp),len(task['roles'])),classification_metrics(yl,np.array(lp),len(task['levels']))
    for epoch in range(1,epochs+1):
        total=0;gradient={k:np.zeros_like(v) for k,v in model.params.items()}
        for cid in splits['train']:
            loss,grads=model.gradient(tensors[cid],*targets[cid],*weights,label_smoothing=.04);total+=loss
            for key in gradient:gradient[key]+=grads[key]/len(splits['train'])
        norm=np.sqrt(sum(float((g*g).sum()) for g in gradient.values()))
        for key,value in model.params.items():
            g=gradient[key]*min(1,5/max(1e-12,norm))+1e-4*value
            first[key]=.9*first[key]+.1*g;second[key]=.999*second[key]+.001*g*g
            value-=.012*(first[key]/(1-.9**epoch))/(np.sqrt(second[key]/(1-.999**epoch))+1e-8)
        r,l=evaluate(splits['validation'],False);valid=r['nll']+.7*l['nll'];history.append({'epoch':epoch,'train_loss':total/len(splits['train']),'validation_loss':valid})
        if valid<best_loss:best_loss=valid;best_epoch=epoch;best={k:v.copy() for k,v in model.params.items()}
        if epoch==1 or epoch%10==0:progress(f'人员角色层级训练 {epoch}/{epochs}，验证损失 {valid:.4f}')
    for key,value in best.items():model.params[key][...]=value
    for head,target_at in [('role',1),('level',2)]:
        logits=[];labels=[]
        for cid in splits['validation']:
            r,l,_=model.forward(tensors[cid]);logits.extend((r if head=='role' else l)[targets[cid][0]]);labels.extend(targets[cid][target_at])
        logits=np.array(logits);labels=np.array(labels);temperature=float(min(np.linspace(.5,3,51),key=lambda t:-np.log(np.maximum(softmax(logits/t)[np.arange(len(labels)),labels],1e-12)).mean()))
        setattr(model,head+'_temperature',temperature)
    scores={name:dict(zip(('roles','levels'),evaluate(ids))) for name,ids in splits.items()}
    model.metadata={'model_id':datetime.now(TZ).strftime('ROLE_%Y%m%d_%H%M%S_%f'),'trained':True,'origin':dataset.manifest['origin'],'dataset_fingerprint':dataset.fingerprint,'epochs':epochs,'selected_epoch':best_epoch,'architecture':'两层时序关系图卷积 + 人员节点角色/层级双分类头 + 允许组合约束','metrics':scores,'history':history,'splits':splits,'split_time_ranges':ranges,'split_entities':{n:sorted(items) for n,items in assets.items()},'graph_fingerprints':{n:[graph_fingerprint(graphs[c]) for c in ids] for n,ids in splits.items()},'limitations':'合成标签用于流程验证。角色、层级和上下层候选不构成犯罪、指挥或控制关系认定；真实任务须经核实的人员标签和外部验证。'}
    model.metadata.update(input_reference=flow_reference(graphs,splits['train']),actor_reference=actor_reference(graphs,tensors,splits),graph_fingerprint_version=2,label_smoothing=.04)
    model.metadata['validation_groups']=validation_groups
    for name,ids in splits.items():
        total=eligible=correct=0
        for cid in ids:
            truth={r['actor_id']:(r['role_label'],r['level_label']) for r in by_case[cid]}
            for row in model.predict(dataset,graphs[cid])['predictions']:
                if row['actor_id'] not in truth:continue
                total+=1
                if row['prediction_available']:eligible+=1;correct+=(row['role'],row['level'])==truth[row['actor_id']]
        scores[name]['selective']={'n':total,'eligible':eligible,'coverage':eligible/total,'joint_accuracy_on_eligible':correct/eligible if eligible else None,'interpretation':'同时报告暂不分类比例；不能只看剩余高分样本准确率'}
    model.save(output);progress('角色与层级模型已完成训练和独立测试并保存');return model
