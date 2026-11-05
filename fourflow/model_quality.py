"""预测分布的校准摘要和训练输入覆盖参考；均不代替外部效果验证。"""
import numpy as np


def validate_reference(metadata,feature_count,flows):
    reference=metadata.get('input_reference')
    if reference is not None:
        required=reference.get('required_flows') if isinstance(reference,dict) else None
        if not isinstance(required,list) or any(not isinstance(f,str) or f not in flows for f in required) or len(required)!=len(set(required)):raise ValueError('模型的输入覆盖参考无效')
    actor=metadata.get('actor_reference')
    if actor is not None:
        try:
            mean=np.asarray(actor['mean'],dtype=float);scale=np.asarray(actor['scale'],dtype=float);threshold=float(actor['threshold'])
            valid=mean.shape==scale.shape==(feature_count,) and np.isfinite(mean).all() and np.isfinite(scale).all() and (scale>0).all() and np.isfinite(threshold) and 1<=threshold<=100
        except (KeyError,TypeError,ValueError):valid=False
        if not valid:raise ValueError('模型的人员输入参考形状、尺度或阈值无效')


def calibration_summary(labels,probabilities,bins=5):
    y=np.asarray(labels,dtype=int);p=np.asarray(probabilities,dtype=float)
    if not len(y):return {'n':0,'bins':[],'ece':None,'interpretation':'无可评估标签'}
    if p.ndim==1:
        confidence=p;correct=y;brier=float(np.mean((p-y)**2));loss=float(-np.mean(y*np.log(np.clip(p,1e-12,1))+(1-y)*np.log(np.clip(1-p,1e-12,1))))
    else:
        confidence=p.max(axis=1);correct=(p.argmax(axis=1)==y).astype(float)
        targets=np.eye(p.shape[1])[y];brier=float(np.mean(((p-targets)**2).sum(axis=1)));loss=float(-np.log(np.maximum(p[np.arange(len(y)),y],1e-12)).mean())
    rows=[];ece=0.
    for i in range(bins):
        low,high=i/bins,(i+1)/bins;selected=(confidence>=low)&((confidence<high) if i<bins-1 else (confidence<=high))
        count=int(selected.sum())
        if not count:continue
        predicted=float(confidence[selected].mean());observed=float(correct[selected].mean());ece+=count/len(y)*abs(predicted-observed)
        rows.append({'lower':low,'upper':high,'n':count,'mean_prediction':predicted,'observed_frequency':observed})
    return {'n':len(y),'brier':brier,'log_loss':loss,'ece':float(ece),'bins':rows,'interpretation':'分箱频率仅描述这组留出标签；Brier同时受区分能力影响，小样本或合成数据不能证明真实概率校准。'}


def flow_reference(graphs,train_ids):
    sets=[{d['flow'] for _,_,_,d in graphs[c].edges(keys=True,data=True) if d.get('verified')!='pending'} for c in train_ids]
    return {'required_flows':sorted(set.intersection(*sets)),'interpretation':'仅取训练案件共同出现的流作为输入覆盖检查；缺流时不把完整输入训练分数强用于残缺快照。'}


def actor_reference(graphs,tensors,splits):
    def rows(ids):
        return np.concatenate([tensors[c][0][[i for i,n in enumerate(sorted(graphs[c])) if graphs[c].nodes[n]['kind']=='actor']] for c in ids])
    train=rows(splits['train']);mean=train.mean(axis=0);scale=np.maximum(train.std(axis=0),.25)
    validation=rows(splits['validation']);scores=np.max(np.abs((validation-mean)/scale),axis=1)
    return {'mean':mean.tolist(),'scale':scale.tolist(),'threshold':max(4.,float(np.quantile(scores,.99))*1.25),'min_support_records':2,'fit':'均值/尺度仅拟合训练人员；阈值由验证人员99%分位数加余量确定，至少4。启发式输入偏离检查，不是已校准的分布外概率。'}
