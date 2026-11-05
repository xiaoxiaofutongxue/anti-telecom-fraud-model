"""双图、原始媒体和四监督头端到端联合训练，本项目独立研究实现。"""
from pathlib import Path
from datetime import datetime
import copy,hashlib,json
import networkx as nx
import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from .core import TZ,FLOWS,build_case_graph,parse_time,entity_id
from .model import FEATURE_NAMES,label_definition,metrics as binary_metrics
from .roles import role_task,classification_metrics
from .hd_eia import auxiliary,prepare,attach_targets,EVENT_FEATURES,file_sha,mask_untrained_modalities

VERSION=1
LIMITATIONS=['合成训练不能替代真实案件验证。','控制边是有监督方向候选，不是指挥或违法认定。','原始媒体轻量编码仅证明训练链路；不承诺人脸、说话人或视听语义识别。','时序关联和遮蔽敏感性不是因果干预。']


class FlowLayer(nn.Module):
    def __init__(self,hidden,channels):
        super().__init__();self.self_weight=nn.Linear(hidden,hidden);self.weights=nn.Parameter(torch.randn(channels,hidden,hidden)*.04);self.norm=nn.LayerNorm(hidden)
    def forward(self,x,adj):return F.gelu(self.norm(self.self_weight(x)+torch.einsum('rij,jh,rhk->ik',adj,x,self.weights)))


class RawFusion(nn.Module):
    def __init__(self,h):
        super().__init__()
        self.pixels=nn.Sequential(nn.Conv2d(3,8,3,padding=1),nn.GELU(),nn.AvgPool2d(2),nn.Conv2d(8,h,3,padding=1),nn.GELU(),nn.AdaptiveAvgPool2d(1),nn.Flatten())
        self.wave=nn.Sequential(nn.Conv1d(1,8,17,stride=8,padding=8),nn.GELU(),nn.Conv1d(8,h,9,stride=8,padding=4),nn.GELU(),nn.AdaptiveAvgPool1d(1),nn.Flatten())
        self.video_time=nn.Conv1d(h,h,3,padding=1)
        self.bytes=nn.Embedding(257,h,padding_idx=0);self.text=nn.Linear(h,h)
        self.gate=nn.Linear(h,1);self.norm=nn.LayerNorm(h)
    def forward(self,item,disabled=None):
        t=item['text'];mask=t.ne(0);text=self.text((self.bytes(t)*mask[:,:,None]).sum(1)/mask.sum(1,keepdim=True).clamp_min(1))
        slots=[text];present=[mask.any(1) & (disabled!='text')];m=len(t)
        for kind in ('image','audio','video'):
            raw=item['media'][kind];indices=item['media_event'][kind];values=torch.zeros_like(text);counts=torch.zeros(m)
            if len(raw) and disabled!=kind:
                if kind=='image':encoded=self.pixels(raw)
                elif kind=='audio':encoded=self.wave(raw)
                else:
                    frames=self.pixels(raw.flatten(0,1)).reshape(len(raw),4,-1)
                    encoded=F.gelu(self.video_time(frames.transpose(1,2))).mean(2)
                values=values.index_add(0,indices,encoded);counts=counts.index_add(0,indices,torch.ones(len(indices)))
                values=values/counts.clamp_min(1)[:,None]
            slots.append(values);present.append(counts>0)
        values=torch.stack(slots,1);exists=torch.stack(present,1)
        logits=self.gate(values).squeeze(-1).masked_fill(~exists,-1e4)
        attention=logits.softmax(1)*exists;attention=attention/attention.sum(1,keepdim=True).clamp_min(1e-8)
        fused=self.norm((values*attention[:,:,None]).sum(1))*exists.any(1)[:,None]
        return fused,attention,exists


class DualNetwork(nn.Module):
    def __init__(self,task,hidden=24):
        super().__init__();self.entity=nn.Linear(len(FEATURE_NAMES),hidden);self.event=nn.Linear(EVENT_FEATURES,hidden);self.raw=RawFusion(hidden)
        self.entity_layers=nn.ModuleList([FlowLayer(hidden,8) for _ in range(2)]);self.event_layers=nn.ModuleList([FlowLayer(hidden,2) for _ in range(2)])
        self.to_entity=nn.Linear(hidden,hidden);self.to_event=nn.Linear(hidden,hidden);self.cross_gate=nn.Linear(hidden*2,hidden)
        self.role=nn.Linear(hidden,len(task['roles']));self.level=nn.Linear(hidden,len(task['levels']))
        self.fund=nn.Sequential(nn.Linear(hidden*2,hidden),nn.GELU(),nn.Linear(hidden,1))
        self.control=nn.Sequential(nn.Linear(hidden*4+3,hidden),nn.GELU(),nn.Linear(hidden,1))
        self.register_buffer('feature_mean',torch.zeros(len(FEATURE_NAMES)));self.register_buffer('feature_scale',torch.ones(len(FEATURE_NAMES)))
    def forward(self,item,disable=None):
        x=F.gelu(self.entity((item['x']-self.feature_mean)/self.feature_scale));z=F.gelu(self.event(item['event_x']))
        media,attention,present=self.raw(item,disable if disable in {'image','audio','video','text'} else None)
        if disable!='media':z=z+.3*media
        c=item['incidence'];to_entity=c/c.sum(1,keepdim=True).clamp_min(1);to_event=c.T/c.T.sum(1,keepdim=True).clamp_min(1)
        for e_layer,v_layer in zip(self.entity_layers,self.event_layers):
            e=e_layer(x,item['a']);v=v_layer(z,item['b'])
            if disable=='entity':e=e*0
            if disable=='trajectory':v=v*0
            if disable!='interaction':
                bridge=to_entity@v;gate=self.cross_gate(torch.cat([e,bridge],1)).sigmoid()
                x=e+.3*gate*self.to_entity(bridge);z=v+.3*self.to_event(to_event@e)
            else:x,z=e,v
        pairs=item['pairs'];u,v=(x[pairs[:,0]],x[pairs[:,1]]) if len(pairs) else (x[:0],x[:0])
        pair_input=torch.cat([u,v,u-v,u*v,item['pair_features']],1)
        alignment=F.mse_loss(F.normalize(to_entity@z,dim=1),F.normalize(x,dim=1))
        return {'fund':self.fund(torch.cat([x.mean(0),z.mean(0)])).squeeze(),'role':self.role(x),'level':self.level(x),'control':self.control(pair_input).squeeze(-1),'embedding':torch.cat([x.mean(0),z.mean(0)]),'alignment':alignment,'attention':attention,'present':present}


def loss_parts(out,item):
    valid=item['role_target']>=0
    role=F.cross_entropy(out['role'][valid],item['role_target'][valid],label_smoothing=.03) if valid.any() else out['role'].sum()*0
    level=F.cross_entropy(out['level'][valid],item['level_target'][valid],label_smoothing=.03) if valid.any() else out['level'].sum()*0
    control_valid=item['control_target']>=0;targets=item['control_target'][control_valid].float()
    control=F.binary_cross_entropy_with_logits(out['control'][control_valid],targets,pos_weight=torch.tensor(3.)) if len(targets) else out['control'].sum()*0
    return {'fund':F.binary_cross_entropy_with_logits(out['fund'],torch.tensor(float(item['fund_target']))),'role':role,'level':level,'control':control,'alignment':out['alignment']}


def objective(parts):return parts['fund']+parts['role']+.6*parts['level']+.7*parts['control']+.03*parts['alignment']


def split_cases(dataset,aux):
    cases=[r['case_id'] for r in dataset.tables['cases']]
    if len(cases)<12:raise ValueError('双图联合训练至少需要12个完整案件')
    graphs={c:build_case_graph(dataset,c) for c in cases}
    if any(not g.number_of_edges() for g in graphs.values()):raise ValueError('训练案件不得为空图')
    ordered=sorted(cases,key=lambda c:min(d['timestamp'] for _,_,_,d in graphs[c].edges(keys=True,data=True)))
    a,b=int(len(cases)*.6),int(len(cases)*.8);splits={'train':ordered[:a],'validation':ordered[a:b],'test':ordered[b:]}
    seen={};media_seen={};ranges={};task=role_task(dataset.manifest)
    for name,ids in splits.items():
        times=[];roles=set();levels=set();fund=set();control=set()
        for cid in ids:
            times.extend(parse_time(d['timestamp']) for _,_,_,d in graphs[cid].edges(keys=True,data=True))
            for node,d in graphs[cid].nodes(data=True):
                if d['kind'] not in {'wallet','account','ip'} or (dataset.synthetic and d['entity_id'] in {'兑换服务地址','服务账户','收款账户'}):continue
                if node in seen and seen[node]!=name:raise ValueError('联合训练切分共享资产/IP，须先合并关联案件')
                seen[node]=name
            for r in dataset.rows('person_labels',cid):
                if r['confirmed'].lower() in {'true','1'}:roles.add(r['role_label']);levels.add(r['level_label']);times.append(parse_time(r['timestamp']))
            fund.update(int(r['label']) for r in dataset.rows('case_labels',cid) if r['confirmed'].lower() in {'true','1'})
            for r in aux['controls']:
                if r['case_id']==cid:control.add(int(r['label']));times.append(parse_time(r['timestamp']))
            for r in aux['media']:
                if r['case_id']!=cid:continue
                if r['sha256'] in media_seen and media_seen[r['sha256']]!=name:raise ValueError('同一原始媒体出现在不同集合')
                media_seen[r['sha256']]=name;times.append(parse_time(r['available_at']))
        if roles!=set(task['roles']) or levels!=set(task['levels']) or fund!={0,1} or control!={0,1}:raise ValueError(name+'缺少某类资金、人员或显式控制标签')
        ranges[name]=(min(times),max(times))
    if ranges['train'][1]>=ranges['validation'][0] or ranges['validation'][1]>=ranges['test'][0]:raise ValueError('联合训练的事件/媒体/标签时间跨越集合')
    from .validation_groups import check_group_splits
    check_group_splits(dataset,splits)
    return splits,graphs


def evaluate_items(network,items,temperatures=None,control_threshold=.5):
    network.eval();temperatures=temperatures or {};fund=[];fund_p=[];r_y=[];r_p=[];l_y=[];l_p=[];c_y=[];c_p=[];per_case=[]
    with torch.no_grad():
        for item in items:
            out=network(item)
            if not all(torch.isfinite(out[k]).all() for k in ('fund','role','level','control')):raise ValueError('联合模型输出出现NaN/无穷值')
            p=float((out['fund']/temperatures.get('fund',1)).sigmoid());fund.append(item['fund_target']);fund_p.append(p)
            valid=item['role_target']>=0;r_y.extend(item['role_target'][valid].tolist());l_y.extend(item['level_target'][valid].tolist())
            r_p.extend((out['role'][valid]/temperatures.get('role',1)).softmax(1).tolist());l_p.extend((out['level'][valid]/temperatures.get('level',1)).softmax(1).tolist())
            valid_c=item['control_target']>=0;cp=(out['control'][valid_c]/temperatures.get('control',1)).sigmoid().tolist();c_y.extend(item['control_target'][valid_c].tolist());c_p.extend(cp)
            per_case.append({'case_id':item['case_id'],'domain':item['domain'],'fund_label':item['fund_target'],'fund_score':p})
    rcount=network.role.out_features;lcount=network.level.out_features
    if not fund or not r_y or not c_y:raise ValueError('联合评测须包含经确认的案件、人员及至少一条明确控制标签；未知关系不能当负类')
    # 控制阈值通过平移概率的logit评估，原始校准指标仍用原分数。
    control=binary_metrics(c_y,c_p);pred=np.array(c_p)>=control_threshold;y=np.array(c_y);tp=int(((y==1)&pred).sum());fp=int(((y==0)&pred).sum());fn=int(((y==1)&~pred).sum());tn=int(((y==0)&~pred).sum())
    control.update(precision=tp/max(1,tp+fp),recall=tp/max(1,tp+fn),f1=2*tp/max(1,2*tp+fp+fn),confusion={'tp':tp,'fp':fp,'fn':fn,'tn':tn},threshold=control_threshold)
    return {'cases':len(items),'fund':binary_metrics(fund,fund_p),'roles':classification_metrics(r_y,np.array(r_p),rcount),'levels':classification_metrics(l_y,np.array(l_p),lcount),'control':control,'per_case':per_case}


class DualFlowModel:
    def __init__(self,task,hidden=24):
        self.task=role_task({'role_task':task});self.hidden=hidden;self.network=DualNetwork(task,hidden);self.metadata={};self.temperatures={k:1. for k in ('fund','role','level','control')}
    def save(self,directory):
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True);path=directory/'weights.npz'
        np.savez_compressed(path,**{k:v.detach().cpu().numpy() for k,v in self.network.state_dict().items()})
        self.metadata.update(kind='dual_flow_joint',version=VERSION,hidden=self.hidden,role_task=self.task,temperatures=self.temperatures,weights_sha256=file_sha(path),feature_names=FEATURE_NAMES)
        (directory/'model_card.json').write_text(json.dumps(self.metadata,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    @classmethod
    def load(cls,directory):
        directory=Path(directory);m=json.loads((directory/'model_card.json').read_text(encoding='utf-8'))
        if m.get('kind')!='dual_flow_joint' or m.get('version')!=VERSION or m.get('feature_names')!=FEATURE_NAMES or m.get('origin') not in {'real','synthetic'} or not m.get('trained') or type(m.get('hidden')) is not int or not 8<=m['hidden']<=64:raise ValueError('双图模型卡结构/来源无效')
        if file_sha(directory/'weights.npz')!=m.get('weights_sha256'):raise ValueError('双图模型权重校验失败')
        model=cls(role_task(m),m['hidden']);state=model.network.state_dict()
        with np.load(directory/'weights.npz',allow_pickle=False) as archive:
            if set(archive.files)!=set(state):raise ValueError('双图模型参数集合不一致')
            for key,expected in state.items():
                value=archive[key]
                if value.shape!=tuple(expected.shape) or not np.isfinite(value).all():raise ValueError('双图模型参数形状或数值无效')
                state[key]=torch.from_numpy(value.copy()).to(expected.dtype)
        model.network.load_state_dict(state)
        if (model.network.feature_scale<=0).any():raise ValueError('双图归一化尺度无效')
        model.temperatures=m.get('temperatures',{})
        if set(model.temperatures)!={'fund','role','level','control'} or any(not isinstance(v,(int,float)) or not np.isfinite(v) or not .1<=v<=10 for v in model.temperatures.values()):raise ValueError('双图温度无效')
        if not all(key in m for key in ('model_id','metrics','splits','label_definition','control_task','training_domains')):raise ValueError('双图模型卡缺少训练评估信息')
        threshold=m.get('control_threshold');reference=m.get('input_reference',{})
        if not isinstance(threshold,(int,float)) or not .5<=threshold<=1 or len(reference.get('mean',[]))!=len(FEATURE_NAMES) or len(reference.get('scale',[]))!=len(FEATURE_NAMES) or not np.isfinite(np.array(reference['mean']+reference['scale'])).all() or min(reference['scale'])<=0:raise ValueError('双图输入参考或控制阈值无效')
        if not isinstance(reference.get('threshold'),(int,float)) or not np.isfinite(reference['threshold']) or not 1<=reference['threshold']<=100 or not isinstance(reference.get('required_flows'),list) or not set(reference['required_flows']).issubset(FLOWS):raise ValueError('双图输入参考范围无效')
        modalities=m.get('trained_modalities',['image','audio','video'])
        if not isinstance(modalities,list) or len(modalities)!=len(set(modalities)) or not set(modalities).issubset({'image','audio','video'}):raise ValueError('模型已训练模态声明无效')
        model.metadata=m;model.network.eval();return model
    def predict(self,dataset,graph,explain=False):
        if not self.metadata.get('trained'):return {'available':False,'reason':'联合模型未训练'}
        if self.metadata['origin']=='synthetic' and not dataset.synthetic:return {'available':False,'reason':'合成联合模型不能对真实案件评分，请使用真实标签训练'}
        if role_task(dataset.manifest)!=self.task or label_definition(dataset.manifest)!=self.metadata.get('label_definition'):return {'available':False,'reason':'联合模型的标签任务定义不一致'}
        aux=auxiliary(dataset)
        if aux['config']['control_task']!=self.metadata['control_task']:return {'available':False,'reason':'控制任务定义不一致'}
        original_item=prepare(dataset,graph,aux);trained_modalities=self.metadata.get('trained_modalities',['image','audio','video']);item=mask_untrained_modalities(original_item,trained_modalities);reference=self.metadata['input_reference'];observed={d['flow'] for _,_,_,d in graph.edges(keys=True,data=True) if d.get('verified')!='pending'}
        if not set(reference['required_flows']).issubset(observed):return {'available':False,'reason':'缺少联合训练必备流，暂不评分'}
        self.network.eval()
        with torch.no_grad():out=self.network(item)
        if not all(torch.isfinite(out[k]).all() for k in ('fund','role','level','control')):raise ValueError('联合模型输出数值不稳定')
        p=(out['role']/self.temperatures['role']).softmax(1).numpy();q=(out['level']/self.temperatures['level']).softmax(1).numpy();scores=(out['control']/self.temperatures['control']).sigmoid().tolist()
        allowed=[(self.task['roles'].index(a),self.task['levels'].index(b)) for a,b in self.task['compatible_pairs']];predictions=[];eligible=set()
        for i in item['actor_indices']:
            node=item['nodes'][i];joint=np.array([p[i,a]*q[i,b] for a,b in allowed]);mass=float(joint.sum());joint/=max(mass,1e-12);choice=int(joint.argmax());a,b=allowed[choice];reasons=[]
            shift=float(np.max(np.abs((item['x'][i].numpy()-np.array(reference['mean']))/np.array(reference['scale']))))
            if len(item['support'][node])<2:reasons.append('有效事件不足2条')
            if shift>reference['threshold']:reasons.append('人员输入超出训练/验证参考')
            if joint[choice]<.6 or mass<.2 or (p[i].argmax(),q[i].argmax()) not in allowed:reasons.append('角色与层级不确定或冲突')
            if not reasons:eligible.add(i)
            predictions.append({'node':node,'actor_id':graph.nodes[node]['entity_id'],'alias':graph.nodes[node]['label'],'role':self.task['roles'][a] if not reasons else '未判定','level':self.task['levels'][b] if not reasons else '未判定','candidate_role':self.task['roles'][a],'candidate_level':self.task['levels'][b],'score':float(joint[choice]),'role_distribution':dict(zip(self.task['roles'],map(float,p[i]))),'level_distribution':dict(zip(self.task['levels'],map(float,q[i]))),'prediction_available':not reasons,'reasons':reasons,'feature_shift':shift})
        candidates=[];dag=nx.DiGraph();rejected=[];threshold=self.metadata['control_threshold']
        for (i,j),score in sorted(zip(item['pairs'].tolist(),scores),key=lambda pair:-pair[1]):
            evidence=item['contact'].get((i,j),[]);row={'from':item['nodes'][i],'to':item['nodes'][j],'score':score,'evidence':evidence,'status':'有监督方向候选，待核验，不证明控制'}
            if score<threshold or i not in eligible or j not in eligible or len(evidence)<2:continue
            dag.add_edge(row['from'],row['to'])
            if not nx.is_directed_acyclic_graph(dag):dag.remove_edge(row['from'],row['to']);rejected.append({**row,'reason':'为保持候选DAG而抑制环；不证明真实关系无环'});continue
            candidates.append(row)
        case=next(r for r in dataset.tables['cases'] if r['case_id']==item['case_id']);unseen=[key for key in ('region','case_type') if case[key] not in self.metadata['training_domains'][key]]
        missing_media=[k for k,values in item['media'].items() if k in trained_modalities and not len(values)]
        result={'available':True,'model_id':self.metadata['model_id'],'origin':self.metadata['origin'],'dataset_fingerprint':dataset.fingerprint,'aux_fingerprint':aux['fingerprint'],'graph_fingerprint':item['graph_fingerprint'],'case_id':item['case_id'],'fund_score':float((out['fund']/self.temperatures['fund']).sigmoid()),'predictions':predictions,'control_candidates':candidates,'cycle_suppressed':rejected,'control_threshold':threshold,'media':item['media_details'],'missing_modalities':missing_media,'media_attention':out['attention'].tolist(),'event_count':len(item['events']),'unseen_domain_fields':unseen,'limitations':LIMITATIONS,'scope':'全部分数为候选模式输出，不能作法律、身份或个案因果认定'}
        result['trained_modalities']=trained_modalities;result['ignored_untrained_modalities']=[k for k,v in original_item['media'].items() if len(v) and k not in trained_modalities]
        if explain:
            from .moid_xai import explain_joint
            result['explanation']=explain_joint(self,item,dataset,graph)
        return result


def train_joint(dataset,output,epochs=60,seed=414,progress=None,robust=True):
    if type(epochs) is not int or not 1<=epochs<=300:raise ValueError('联合训练轮数须1至300')
    output=Path(output)
    if output.exists() and any(output.iterdir()):raise ValueError('联合模型输出目录非空，停止覆盖')
    progress=progress or (lambda s:None);torch.set_num_threads(2);torch.manual_seed(seed);np.random.seed(seed)
    aux=auxiliary(dataset);splits,graphs=split_cases(dataset,aux);cache={};items={c:attach_targets(prepare(dataset,g,aux,cache),dataset,aux) for c,g in graphs.items()};model=DualFlowModel(role_task(dataset.manifest));net=model.network
    x=torch.cat([items[c]['x'] for c in splits['train']]);net.feature_mean.copy_(x.mean(0));net.feature_scale.copy_(x.std(0).clamp_min(.1))
    actors=torch.cat([items[c]['x'][items[c]['actor_indices']] for c in splits['train']]);mean=actors.mean(0).numpy();scale=actors.std(0).clamp_min(.1).numpy()
    validation_actors=torch.cat([items[c]['x'][items[c]['actor_indices']] for c in splits['validation']]).numpy();shift=np.max(np.abs((validation_actors-mean)/scale),axis=1)
    reference={'mean':mean.tolist(),'scale':scale.tolist(),'threshold':float(max(5,np.quantile(shift,.99)+.5)),'required_flows':sorted(set.intersection(*[{d['flow'] for _,_,_,d in graphs[c].edges(keys=True,data=True) if d.get('verified')!='pending'} for c in splits['train']]))}
    domains=sorted({items[c]['domain'] for c in splits['train']});weights=torch.ones(len(domains))/len(domains);optimizer=torch.optim.AdamW(net.parameters(),lr=.006,weight_decay=1e-4);history=[];best=None;best_score=float('inf');best_epoch=0;gradient_evidence={}
    training=[items[c] for c in splits['train']]
    active_modalities=[kind for kind in ('image','audio','video') if any(len(i['media'][kind]) for i in training)]
    items={cid:mask_untrained_modalities(item,active_modalities) for cid,item in items.items()};training=[items[c] for c in splits['train']]
    for epoch in range(1,epochs+1):
        net.train();optimizer.zero_grad();group_losses=[]
        for domain in domains:
            losses=[objective(loss_parts(net(item,('image','audio','video','media',None)[(epoch+j)%5]),item)) for j,item in enumerate(training) if item['domain']==domain];group_losses.append(torch.stack(losses).mean())
        losses=torch.stack(group_losses)
        with torch.no_grad():
            if robust:weights=(weights*torch.exp(.02*losses.detach().clamp_max(10))).clamp_min(1e-6);weights/=weights.sum()
        loss=(weights*losses).sum();loss.backward()
        if epoch==1:
            for name,module in [('entity',net.entity_layers),('trajectory',net.event_layers),('raw_image',net.raw.pixels),('raw_audio',net.raw.wave),('raw_video',net.raw.video_time),('interaction',net.cross_gate),('control',net.control)]:gradient_evidence[name]=float(sum((p.grad.abs().sum() for p in module.parameters() if p.grad is not None),torch.tensor(0.)))
        torch.nn.utils.clip_grad_norm_(net.parameters(),5);optimizer.step();net.eval()
        with torch.no_grad():valid=float(torch.stack([objective(loss_parts(net(items[c]),items[c])) for c in splits['validation']]).mean())
        history.append({'epoch':epoch,'train_loss':float(loss.detach()),'validation_loss':valid,'group_weights':dict(zip(domains,weights.tolist()))})
        if not np.isfinite(valid):raise ValueError('联合训练数值不稳定，未发布模型')
        if valid<best_score:best_score=valid;best_epoch=epoch;best=copy.deepcopy(net.state_dict())
        if epoch==1 or epoch%10==0:progress(f'双图联合训练 {epoch}/{epochs}，验证损失 {valid:.4f}')
    net.load_state_dict(best);net.eval();valid_items=[items[c] for c in splits['validation']]
    # 四头温度及控制阈值只使用验证标签；测试集合从未参加选择。
    with torch.no_grad():outputs=[net(i) for i in valid_items]
    for head in model.temperatures:
        candidates=[]
        for temperature in (.5,.7,1.,1.5,2.):
            values=[]
            for item,out in zip(valid_items,outputs):
                if head=='fund':value=F.binary_cross_entropy_with_logits(out[head]/temperature,torch.tensor(float(item['fund_target'])))
                else:
                    target=item[head+'_target'];valid=target>=0
                    if not valid.any():continue
                    value=F.binary_cross_entropy_with_logits(out[head][valid]/temperature,target[valid].float()) if head=='control' else F.cross_entropy(out[head][valid]/temperature,target[valid])
                values.append(float(value))
            candidates.append((float(np.mean(values)),temperature))
        model.temperatures[head]=min(candidates)[1]
    choices=[]
    for threshold in (.5,.6,.7,.8,.9,.95):
        metric=evaluate_items(net,valid_items,model.temperatures,threshold)['control'];choices.append((metric['f1'],metric['precision'],threshold))
    threshold=max(choices)[2]
    cases={r['case_id']:r for r in dataset.tables['cases']};model.metadata={'trained':True,'model_id':'DUAL_'+datetime.now(TZ).strftime('%Y%m%d_%H%M%S_%f'),'origin':dataset.manifest['origin'],'label_definition':label_definition(dataset.manifest),'control_task':aux['config']['control_task'],'input_reference':reference,'dataset_fingerprint':dataset.fingerprint,'aux_fingerprint':aux['fingerprint'],'epochs':epochs,'seed':seed,'selected_epoch':best_epoch,'splits':splits,'training_domains':{k:sorted({cases[c][k] for c in splits['train']}) for k in ('region','case_type')},'group_dro':{'domains':domains,'selection':'仅验证集复合损失选轮次，验证标签拟合温度/阈值，测试只评测'},'history':history,'gradient_evidence':gradient_evidence,'limitations':LIMITATIONS,'architecture':'HD-EIA工程编码 + 实体/事件双图交互 + 原始CNN/波形CNN/视频时序卷积 + 资金/角色/层级/定向控制联合监督','implementation_definition':'本项目自主定义，原材料未提供算法公式，不冒充同名论文完整复现','metrics':{name:evaluate_items(net,[items[c] for c in ids],model.temperatures,threshold) for name,ids in splits.items()},'control_threshold':threshold}
    required_gradients=['entity','trajectory','interaction','control']
    if set(active_modalities)&{'image','video'}:required_gradients.append('raw_image')
    if 'audio' in active_modalities:required_gradients.append('raw_audio')
    if 'video' in active_modalities:required_gradients.append('raw_video')
    if not all(gradient_evidence[k]>0 for k in required_gradients):raise ValueError('某已启用联合模块无有效梯度，停止发布')
    model.metadata['trained_modalities']=active_modalities
    model.metadata['modality_dropout']=True
    from .validation_groups import check_group_splits
    model.metadata['validation_groups']=check_group_splits(dataset,splits)
    model.metadata['optimization']='GroupDRO' if robust else 'ERM uniform-domain baseline'
    model.metadata['training_assets']=sorted({node for g in graphs.values() for node,d in g.nodes(data=True) if d['kind'] in {'wallet','account','ip'} and not (dataset.synthetic and d['entity_id'] in {'兑换服务地址','服务账户','收款账户'})})
    model.metadata['training_media']=sorted({r['sha256'] for r in aux['media']})
    model.save(output);progress('联合模型已保存，测试分数与限制保留在模型卡');return model
