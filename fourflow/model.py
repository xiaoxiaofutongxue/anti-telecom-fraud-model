"""NumPy 实现的两层时序异构图卷积；所有卷积矩阵均参与反向传播。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import numpy as np

from .core import FLOWS, KINDS, TZ, build_case_graph, parse_time, safe_json, is_true
from .model_quality import calibration_summary,flow_reference,validate_reference

RELATIONS = ["transfer","payment","exchange_in","exchange_out","dispatch","deliver","sender","receiver","carrier","transport","communicate","uses_device","identity","actor_relation","observed_ip","account_access","device_network"]
CHANNELS = RELATIONS + ["reverse_"+r for r in RELATIONS]
FEATURE_NAMES = ["type_"+k for k in KINDS]+["log_in_degree","log_out_degree"]+["log_"+f+"_degree" for f in FLOWS]+["age_30days","log_span_hours"]+[f"text_hash_{i}" for i in range(8)]+["relative_fund_in","relative_fund_out","log_distinct_ips","log_network_events"]
ENCODER_VERSION=3


def label_definition(manifest):
    definition=manifest.get('label_definition',{})
    fields=('task_id','positive','negative')
    if not isinstance(definition,dict) or any(not isinstance(definition.get(k),str) or not definition[k].strip() for k in fields):
        raise ValueError('训练或评分前，manifest.json须明确label_definition中的task_id及正负标签含义')
    return {k:definition[k].strip() for k in fields}


def resolve_model_directory(root):
    root=Path(root)
    pointer=root/"runtime"/"models"/"latest.json"
    if pointer.is_file():
        try:
            candidate=Path(json.loads(pointer.read_text(encoding="utf-8"))["path"])
            if not candidate.is_absolute():candidate=root/candidate
            if (candidate/"model_card.json").is_file():return candidate
        except (ValueError,KeyError,TypeError):pass
    return root/"runtime"/"models"/"current"


def graph_fingerprint(graph):
    content={"fingerprint_version":2,"encoder_version":ENCODER_VERSION,"as_of":graph.graph.get('as_of',''),"nodes":sorted((n,d["kind"]) for n,d in graph.nodes(data=True)),"edges":sorted((u,v,k,d["relation"],d["flow"],d.get('verified',''),d.get('effective_from',''),d["timestamp"],str(d.get("amount","")),d.get("currency",""),d.get("summary",""),d.get("event_type",""),d.get("network_scope","")) for u,v,k,d in graph.edges(keys=True,data=True))}
    return hashlib.sha256(json.dumps(content,ensure_ascii=False).encode()).hexdigest()


def encode_graph(graph, disabled_flow=None, half_life_hours=24):
    if len(graph)>256:
        raise ValueError("当前本地模型支持每个样本最多 256 个节点；请按事件窗口拆分。图谱检索不受该模型上限约束。")
    nodes=sorted(graph.nodes())
    if not nodes:
        raise ValueError("空图不能评分")
    index={n:i for i,n in enumerate(nodes)}
    observed=[e for e in graph.edges(keys=True,data=True) if e[3].get('verified')!='pending']
    edges=[e for e in observed if e[3]['flow']!=disabled_flow]
    # 删除单一流时保持同一参考时刻，否则会把其他流突然变新误当作归因。
    latest=parse_time(graph.graph['as_of']) if graph.graph.get('as_of') else max((parse_time(d['timestamp']) for u,v,k,d in observed),default=datetime(2026,1,1,tzinfo=TZ))
    x=np.zeros((len(nodes),len(FEATURE_NAMES)),dtype=np.float64)
    matrices={}
    times={n:[] for n in nodes}
    currency_values={}
    for u,v,k,d in edges:
        if d['relation']=='transfer':currency_values.setdefault(d['currency'],[]).append(float(d['amount'].ln()))
    currency_medians={c:float(np.median(v)) for c,v in currency_values.items()}
    ip_neighbours={n:set() for n in nodes};network_records={n:set() for n in nodes}
    kinds=list(KINDS); flows=list(FLOWS)
    for n in nodes:
        x[index[n],kinds.index(graph.nodes[n]["kind"])]=1
    for u,v,key,data in edges:
        a,b=index[u],index[v]
        base=len(kinds)
        x[b,base]+=1; x[a,base+1]+=1
        flow_index=base+2+flows.index(data["flow"])
        x[a,flow_index]+=1; x[b,flow_index]+=1
        when=parse_time(data["timestamp"])
        times[u].append(when); times[v].append(when)
        if data['relation']=='transfer':
            relative=np.clip(float(data['amount'].ln())-currency_medians[data['currency']],-10,10)
            x[b,-4]+=relative;x[a,-3]+=relative
        if data['table']=='network_events':
            network_records[u].add(data['record_id']);network_records[v].add(data['record_id'])
            if graph.nodes[v]['kind']=='ip':ip_neighbours[u].add(v)
        if data["relation"]=="communicate" and data.get("summary"):
            text="".join(data["summary"].split())[:2048]
            grams=[text[i:i+2] for i in range(max(0,len(text)-1))]
            for gram in grams:
                digest=hashlib.sha256(gram.encode("utf-8")).digest()
                column=len(kinds)+8+digest[0]%8
                value=(1 if digest[1]%2 else -1)/max(1,len(grams))
                x[a,column]+=value;x[b,column]+=value
        age=max(0,(latest-when).total_seconds()/3600)
        weight=np.exp(-np.log(2)*age/half_life_hours)
        for relation,target,source in [(data["relation"],b,a),("reverse_"+data["relation"],a,b)]:
            if relation not in matrices:
                matrices[relation]=np.zeros((len(nodes),len(nodes)))
            matrices[relation][target,source]+=weight
    base=len(kinds)
    x[:,base:base+6]=np.log1p(x[:,base:base+6])
    for node,values in times.items():
        x[index[node],-2]=np.log1p(len(ip_neighbours[node]));x[index[node],-1]=np.log1p(len(network_records[node]))
        if values:
            x[index[node],base+6]=min(1,(latest-max(values)).total_seconds()/(30*86400))
            x[index[node],base+7]=np.log1p((max(values)-min(values)).total_seconds()/3600)
    # 按非零入邻居数归一化，保留衰减大小，避免单邻居权重被约掉。
    for relation,matrix in matrices.items():
        matrices[relation]=matrix/np.maximum(1,(matrix>0).sum(axis=1,keepdims=True))
    return x,matrices


def sigmoid(value):
    return 1/(1+np.exp(-np.clip(value,-40,40)))


class TemporalHeteroGNN:
    def __init__(self,hidden=12,seed=42):
        rng=np.random.default_rng(seed)
        f=len(FEATURE_NAMES)
        self.params={"self1":rng.normal(0,.15,(f,hidden)),"bias1":np.full(hidden,.05),"self2":rng.normal(0,.15,(hidden,hidden)),"bias2":np.full(hidden,.05),"out":rng.normal(0,.15,hidden),"out_bias":np.array(0.)}
        for relation in CHANNELS:
            self.params["r1_"+relation]=rng.normal(0,.06,(f,hidden))
            self.params["r2_"+relation]=rng.normal(0,.06,(hidden,hidden))
        self.mean=np.zeros(f); self.scale=np.ones(f); self.temperature=1.; self.metadata={}

    def forward(self,tensor):
        raw,adj=tensor
        x=(raw-self.mean)/self.scale
        p=self.params
        ax={r:a@x for r,a in adj.items()}
        z1=x@p["self1"]+p["bias1"]
        for r,values in ax.items(): z1+=values@p["r1_"+r]
        h1=np.maximum(z1,0)
        ah={r:a@h1 for r,a in adj.items()}
        z2=h1@p["self2"]+p["bias2"]
        for r,values in ah.items(): z2+=values@p["r2_"+r]
        h2=np.maximum(z2,0); pooled=h2.mean(axis=0)
        logit=float(pooled@p["out"]+p["out_bias"])
        return logit,(x,adj,ax,z1,h1,ah,z2,pooled)

    def gradient(self,tensor,label):
        logit,cache=self.forward(tensor)
        x,adj,ax,z1,h1,ah,z2,pooled=cache
        p=self.params; derivative=float(sigmoid(logit)-label)
        grads={name:np.zeros_like(value) for name,value in p.items()}
        grads["out"]=pooled*derivative; grads["out_bias"]=np.array(derivative)
        dz2=np.tile(p["out"]*derivative/len(x),(len(x),1))*(z2>0)
        grads["self2"]=h1.T@dz2; grads["bias2"]=dz2.sum(axis=0)
        dh1=dz2@p["self2"].T
        for r,a in adj.items():
            grads["r2_"+r]=ah[r].T@dz2
            dh1+=a.T@(dz2@p["r2_"+r].T)
        dz1=dh1*(z1>0)
        grads["self1"]=x.T@dz1; grads["bias1"]=dz1.sum(axis=0)
        for r in adj: grads["r1_"+r]=ax[r].T@dz1
        loss=float(np.logaddexp(0,logit)-label*logit)
        return loss,grads

    def probability(self,graph,disabled_flow=None):
        logit,_=self.forward(encode_graph(graph,disabled_flow))
        if not np.isfinite(logit):raise ValueError('模型输出非有限数值，请复核输入尺度或重新训练；不输出NaN/无穷分数')
        return float(sigmoid(logit/self.temperature))

    def save(self,root):
        root=Path(root); root.mkdir(parents=True,exist_ok=True)
        self.metadata.setdefault('encoder_version',ENCODER_VERSION)
        np.savez_compressed(root/"weights.npz",**self.params,feature_mean=self.mean,feature_scale=self.scale,temperature=np.array(self.temperature))
        self.metadata["weights_sha256"]=hashlib.sha256((root/"weights.npz").read_bytes()).hexdigest()
        (root/"model_card.json").write_text(json.dumps(self.metadata,ensure_ascii=False,indent=2),encoding="utf-8")

    @classmethod
    def load(cls,root):
        root=Path(root)
        metadata=json.loads((root/"model_card.json").read_text(encoding="utf-8"))
        validate_reference(metadata,len(FEATURE_NAMES),FLOWS)
        if metadata.get("feature_names")!=FEATURE_NAMES or not 1<=int(metadata.get("hidden",0))<=128:
            raise ValueError("模型特征版本不兼容，请重新训练或选择匹配版本")
        if metadata.get('encoder_version')!=ENCODER_VERSION:raise ValueError('模型编码版本已更新，请使用新版本重新训练')
        if metadata.get('origin') not in {'real','synthetic'} or not isinstance(metadata.get('model_id'),str) or not metadata['model_id'].strip():raise ValueError('模型卡缺少有效的来源声明或模型编号')
        if metadata.get('trained'):
            required={'architecture','engine','epochs','selected_epoch','temperature','metrics','baseline_test','limitations','splits','label_definition'}
            if not required.issubset(metadata):raise ValueError('训练模型卡不完整，不能作为正式模型载入')
            metrics_required={'n','precision','recall','f1','accuracy','brier'}
            if any(not metrics_required.issubset(metadata['metrics'].get(name,{})) for name in ('train','validation','test')) or not metrics_required.issubset(metadata['baseline_test']):raise ValueError('模型卡的训练评估字段不完整')
        if metadata.get('relation_channels',CHANNELS)!=CHANNELS:raise ValueError('模型关系通道版本不兼容')
        if hashlib.sha256((root/"weights.npz").read_bytes()).hexdigest()!=metadata["weights_sha256"]:
            raise ValueError("模型权重校验失败")
        model=cls(metadata["hidden"])
        with np.load(root/"weights.npz",allow_pickle=False) as archive:
            for key in model.params:
                if archive[key].shape!=model.params[key].shape or not np.isfinite(archive[key]).all():
                    raise ValueError("模型参数形状或数值无效")
                model.params[key]=archive[key].copy()
            model.mean=archive["feature_mean"].copy(); model.scale=archive["feature_scale"].copy(); model.temperature=float(archive["temperature"])
            if model.mean.shape!=(len(FEATURE_NAMES),) or model.scale.shape!=model.mean.shape or not np.isfinite(model.mean).all() or not np.isfinite(model.scale).all() or (model.scale<=0).any() or not np.isfinite(model.temperature) or model.temperature<=0:
                raise ValueError("模型归一化或温度参数无效")
        model.metadata=metadata
        return model

    def assess(self,dataset,graph):
        if graph.graph.get('dataset_fingerprint')!=dataset.fingerprint:return {'available':False,'reason':'分析图与当前数据集快照不一致，请重新分析'}
        if not 0<len(graph)<=256 or not any(d.get('verified')!='pending' for _,_,_,d in graph.edges(keys=True,data=True)):
            return {'available':False,'reason':'当前图为空、没有有效关联或超过本地模型的256节点上限'}
        if self.metadata.get("origin")=="synthetic" and not dataset.synthetic:
            return {"available":False,"reason":"当前为合成验证模型，不能对真实数据输出研判分数。请使用经核实的真实标签重新训练。"}
        if not self.metadata.get('trained'):return {'available':False,'reason':'当前权重未完成训练，不能输出分析分数'}
        try:
            if label_definition(dataset.manifest)!=label_definition(self.metadata):return {'available':False,'reason':'导入数据的预测任务或正负标签含义与模型不一致，不能混用评分'}
        except ValueError as exc:return {'available':False,'reason':str(exc)}
        observed_flows={d['flow'] for _,_,_,d in graph.edges(keys=True,data=True) if d.get('verified')!='pending'}
        required=set(self.metadata.get('input_reference',{}).get('required_flows',[]))
        missing=sorted(required-observed_flows)
        if missing:return {'available':False,'reason':'当前输入缺少训练样本共同具备的'+ '、'.join(FLOWS[f] for f in missing)+'；不强行输出资金模型分数','missing_flows':missing}
        score=self.probability(graph)
        raw,_=encode_graph(graph)
        shift=np.abs((raw.mean(axis=0)-self.mean)/self.scale)
        ablations={flow:round(score-self.probability(graph,flow),6) for flow in FLOWS}
        digest=graph_fingerprint(graph)
        overlap=next((name for name,items in self.metadata.get("graph_fingerprints",{}).items() if digest in items),None)
        overlap_kind='exact_graph' if overlap else None
        entities={n for n,d in graph.nodes(data=True) if d['kind'] in {'wallet','account','ip'} and not (dataset.synthetic and d.get('entity_id') in {'兑换服务地址','服务账户','收款账户'})}
        related=[name for name,items in self.metadata.get('split_entities',{}).items() if entities.intersection(items)]
        if not overlap and related:overlap=related[0];overlap_kind='shared_entity'
        return {"available":True,"probability":round(score,6),"graph_fingerprint":digest,"model_id":self.metadata["model_id"],"origin":self.metadata["origin"],"flow_ablation_deltas":ablations,"known_split":overlap,"overlap_kind":overlap_kind,"related_splits":related,"max_standardized_feature_shift":round(float(shift.max()),4),"shifted_features":[FEATURE_NAMES[i] for i in range(len(shift)) if shift[i]>3],"interpretation":"目标异常模式分数，非违法概率。删除单流关系和特征贡献时保留节点及同一时钟，分数变化只表示敏感性，不构成因果证据。特征偏离是训练节点统计量启发式提示，不是因果偏差估计。"}


def metrics(labels,scores):
    y=np.asarray(labels); p=np.asarray(scores); prediction=p>=.5
    tp=int(((y==1)&prediction).sum()); fp=int(((y==0)&prediction).sum()); fn=int(((y==1)&~prediction).sum()); tn=int(((y==0)&~prediction).sum())
    precision=tp/max(1,tp+fp); recall=tp/max(1,tp+fn)
    positive=p[y==1]; negative=p[y==0]
    auc=float(((positive[:,None]>negative).sum()+.5*(positive[:,None]==negative).sum())/(len(positive)*len(negative))) if len(positive) and len(negative) else None
    return {"n":len(y),"precision":round(precision,4),"recall":round(recall,4),"f1":round(2*precision*recall/max(1e-12,precision+recall),4),"accuracy":round((tp+tn)/len(y),4),"roc_auc":round(auc,4) if auc is not None else None,"brier":round(float(np.mean((p-y)**2)),6),"confusion":{"tp":tp,"fp":fp,"fn":fn,"tn":tn},'calibration':calibration_summary(y,p)}


def train_model(dataset,output,epochs=70,progress=None):
    if not isinstance(epochs,int) or not 1<=epochs<=500:raise ValueError('训练轮数须为1至500的整数')
    progress=progress or (lambda message:None)
    labels={r["case_id"]:int(r["label"]) for r in dataset.tables["case_labels"] if is_true(r["confirmed"])}
    if len(labels)<20:
        raise ValueError("训练至少需要 20 个有来源且经核实标注的完整案件样本，并包含正负两类。不能把未标注样本视为负类。")
    definition=label_definition(dataset.manifest)
    graphs={cid:build_case_graph(dataset,cid) for cid in labels}
    if any(not graph.number_of_edges() for graph in graphs.values()):raise ValueError('已标注案件含空图，不能用空样本训练')
    ordered=sorted(labels,key=lambda cid:min((d["timestamp"] for u,v,k,d in graphs[cid].edges(keys=True,data=True)),default=""))
    cut1,cut2=int(len(ordered)*.6),int(len(ordered)*.8)
    splits={"train":ordered[:cut1],"validation":ordered[cut1:cut2],"test":ordered[cut2:]}
    from .validation_groups import check_group_splits
    validation_groups=check_group_splits(dataset,splits)
    for name,ids in splits.items():
        if {labels[cid] for cid in ids}!={0,1}:
            raise ValueError(f"按案件时间切分后的 {name} 集必须同时包含两类；请补充标注案件。")
    time_ranges={name:{'start':min(d['timestamp'] for cid in ids for _,_,_,d in graphs[cid].edges(keys=True,data=True)),'end':max(d['timestamp'] for cid in ids for _,_,_,d in graphs[cid].edges(keys=True,data=True))} for name,ids in splits.items()}
    if time_ranges['train']['end']>=time_ranges['validation']['start'] or time_ranges['validation']['end']>=time_ranges['test']['start']:
        raise ValueError('训练、验证、测试的案件事件时间重叠，不能声称时间留出；请按关联案件和完整事件区间重新分组')
    # 共用主体/账户不能跨集合。发现重合时停止，而不是静默泄漏。
    seen={}
    for name,ids in splits.items():
        for cid in ids:
            for node,data in graphs[cid].nodes(data=True):
                if data["kind"] not in {"wallet","account","ip"}: continue
                # 常见服务端标识必须由数据提供者以实际标识区分；合成共享服务也会被排除。
                raw=data["entity_id"]
                if dataset.synthetic and raw in {"兑换服务地址","服务账户","收款账户"}: continue
                if node in seen and seen[node]!=name:
                    raise ValueError("不同切分集合存在共享地址/账户或IP（含公网IP及同域内网IP），请先按关联案件分组后构建不重合的数据集。")
                seen[node]=name
    tensors={cid:encode_graph(graphs[cid]) for cid in labels}
    model=TemporalHeteroGNN()
    train_x=np.concatenate([tensors[cid][0] for cid in splits["train"]])
    model.mean=train_x.mean(axis=0); model.scale=np.maximum(train_x.std(axis=0),.1)
    first={k:np.zeros_like(v) for k,v in model.params.items()}; second={k:np.zeros_like(v) for k,v in model.params.items()}
    best,best_loss,best_epoch=None,float("inf"),0
    history=[]
    for epoch in range(1,epochs+1):
        gradients={k:np.zeros_like(v) for k,v in model.params.items()}; total=0
        for cid in splits["train"]:
            loss,grad=model.gradient(tensors[cid],labels[cid]); total+=loss
            for key in gradients: gradients[key]+=grad[key]/len(splits["train"])
        norm=np.sqrt(sum(float(np.sum(g*g)) for g in gradients.values()))
        for key,param in model.params.items():
            grad=gradients[key]*min(1,5/max(1e-12,norm))+1e-4*param
            first[key]=.9*first[key]+.1*grad; second[key]=.999*second[key]+.001*grad**2
            model.params[key]-=.015*(first[key]/(1-.9**epoch))/(np.sqrt(second[key]/(1-.999**epoch))+1e-8)
        validation_logits=[model.forward(tensors[cid])[0] for cid in splits["validation"]]
        validation_loss=float(np.mean([np.logaddexp(0,z)-labels[cid]*z for cid,z in zip(splits["validation"],validation_logits)]))
        history.append({"epoch":epoch,"train_loss":total/len(splits["train"]),"validation_loss":validation_loss})
        if validation_loss<best_loss:
            best_loss=validation_loss; best={k:v.copy() for k,v in model.params.items()}; best_epoch=epoch
        if epoch%10==0 or epoch==1:
            progress(f"训练 {epoch}/{epochs}，训练损失 {total/len(splits['train']):.4f}，验证损失 {validation_loss:.4f}")
    model.params=best
    logits=np.array([model.forward(tensors[cid])[0] for cid in splits["validation"]]); y=np.array([labels[cid] for cid in splits["validation"]])
    temperatures=np.unique(np.r_[1.,np.linspace(.5,5,91)])
    model.temperature=float(min(temperatures,key=lambda t:np.mean(np.logaddexp(0,logits/t)-y*logits/t)))
    evaluation={name:metrics([labels[cid] for cid in ids],[model.probability(graphs[cid]) for cid in ids]) for name,ids in splits.items()}
    # 训练集拟合的逻辑回归基准，使用图级平均节点特征，同样不接触测试标签。
    vectors={cid:((tensors[cid][0]-model.mean)/model.scale).mean(axis=0) for cid in labels}
    w=np.zeros(len(FEATURE_NAMES)); bias=0.
    train_ids=splits["train"]; xx=np.stack([vectors[cid] for cid in train_ids]); yy=np.array([labels[cid] for cid in train_ids])
    for _ in range(300):
        delta=sigmoid(xx@w+bias)-yy
        w-=.06*(xx.T@delta/len(yy)+1e-4*w); bias-=.06*float(delta.mean())
    baseline=metrics([labels[cid] for cid in splits["test"]],[float(sigmoid(vectors[cid]@w+bias)) for cid in splits["test"]])
    model.metadata={"model_id":datetime.now(TZ).strftime("THGNN_%Y%m%d_%H%M%S"),"architecture":"2-layer temporal relation-specific graph convolution + mean pooling + sigmoid", "engine":"NumPy float64 / CPU; convolution matrices trained by backpropagation and Adam", "hidden":12,"epochs":epochs,"selected_epoch":best_epoch,"origin":dataset.manifest["origin"],"dataset_fingerprint":dataset.fingerprint,"feature_names":FEATURE_NAMES,"relation_channels":CHANNELS,"half_life_hours":24,"temperature":model.temperature,"splits":splits,"graph_fingerprints":{name:[graph_fingerprint(graphs[cid]) for cid in ids] for name,ids in splits.items()},"metrics":evaluation,"baseline_test":baseline,"history":history,"limitations":"合成数据指标仅验证该生成机制下的流程。未证明跨地区、跨案型泛化能力；本图级分类器不执行队列因果估计或法律认定，因果研究须在独立模块另行运行。没有标签来源的样本不参与监督训练。"}
    model.metadata.update(feature_version=2, feature_count=len(FEATURE_NAMES), relation_count=len(CHANNELS), target_definition="经核实标签定义的目标异常模式；合成数据为短时资金拆分、归集、回流组合，非电诈或违法认定", amount_policy="交易对数金额减去当前图内同币种中位数；不跨币种累加金额", network_policy="网络观测不自动合并主体；非公网按域隔离，公网IP与账户不得跨训练切分")
    model.metadata.update(split_time_ranges=time_ranges,encoder_version=ENCODER_VERSION,network_policy='网络观测不合并主体；地址/账户与同域IP均不跨切分；内网按来源网络域隔离')
    model.metadata['trained']=True
    model.metadata['validation_groups']=validation_groups
    model.metadata['input_reference']=flow_reference(graphs,splits['train'])
    model.metadata['graph_fingerprint_version']=2
    model.metadata['label_definition']=definition
    model.metadata['split_entities']={name:sorted({n for cid in ids for n,d in graphs[cid].nodes(data=True) if d['kind'] in {'wallet','account','ip'} and not (dataset.synthetic and d.get('entity_id') in {'兑换服务地址','服务账户','收款账户'})}) for name,ids in splits.items()}
    model.save(output)
    progress("训练完成，已保存模型卡、权重、独立测试集指标和基准比较")
    return model
