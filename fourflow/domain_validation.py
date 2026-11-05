"""冻结模型后的外部域评测；不训练、不校准、不改变模型指针。"""
from pathlib import Path
from datetime import datetime
import hashlib,html,json
import numpy as np
import torch
from .core import load_dataset,build_case_graph,TZ
from .hd_eia import auxiliary,prepare,attach_targets,mask_untrained_modalities
from .dual_model import evaluate_items


def evaluate_domains(model,datasets,output_root):
    if not datasets:raise ValueError('至少选择一个独立评测数据目录')
    model.network.eval();weights_before={k:v.detach().clone() for k,v in model.network.state_dict().items()};runs=[];training=model.metadata.get('training_domains',{})
    for name,path in datasets.items():
        dataset=load_dataset(path)
        from .empirical import validate_external
        validate_external(dataset,model)
        if model.metadata['origin']=='synthetic' and not dataset.synthetic:raise ValueError('合成联合模型不能作为真实数据评测模型')
        if dataset.fingerprint==model.metadata['dataset_fingerprint']:raise ValueError('当前目录就是联合训练数据，不能作为外部域验证')
        from .model import label_definition
        from .roles import role_task
        if role_task(dataset.manifest)!=model.task or label_definition(dataset.manifest)!=model.metadata['label_definition']:raise ValueError('外部评测任务不一致')
        aux=auxiliary(dataset)
        if aux['config']['control_task']!=model.metadata['control_task']:raise ValueError('外部控制任务不一致')
        cache={};items=[];case_lookup={r['case_id']:r for r in dataset.tables['cases']}
        for cid in case_lookup:items.append(mask_untrained_modalities(attach_targets(prepare(dataset,build_case_graph(dataset,cid),aux,cache),dataset,aux),model.metadata.get('trained_modalities',['image','audio','video'])))
        # 真实评测还要验证人与团伙级隔离；合成固定服务账户有显式例外。
        current_assets=set();current_media={r['sha256'] for r in aux['media']}
        for cid in case_lookup:
            for n,d in build_case_graph(dataset,cid).nodes(data=True):
                if d['kind'] in {'wallet','account','ip'} and not (dataset.synthetic and d['entity_id'] in {'兑换服务地址','服务账户','收款账户'}):current_assets.add(n)
        if current_assets & set(model.metadata.get('training_assets',[])) or current_media & set(model.metadata.get('training_media',[])):raise ValueError('外部域与训练数据共享资产/IP或原始媒体')
        full=evaluate_items(model.network,items,model.temperatures,model.metadata['control_threshold']);groups={}
        for key in ('region','case_type'):
            groups[key]={}
            for value in sorted({r[key] for r in case_lookup.values()}):
                subset=[i for i in items if case_lookup[i['case_id']][key]==value]
                metrics=evaluate_items(model.network,subset,model.temperatures,model.metadata['control_threshold'])
                groups[key][value]={'unseen_in_training':value not in training.get(key,[]),'case_count':len(subset),'fund':metrics['fund'],'roles':metrics['roles'],'levels':metrics['levels'],'control':metrics['control']}
        # 错误与拒判均保留，不能只展示分类头准确率。
        person_errors=[];abstentions=[];eligible=0;correct=0;labelled=0;control_predictions=[]
        for item in items:
            graph=build_case_graph(dataset,item['case_id']);prediction=model.predict(dataset,graph)
            if not prediction.get('available'):raise ValueError('外部评测推理未能产生有效结果：'+prediction.get('reason',''))
            truth={r['actor_id']:r for r in dataset.rows('person_labels',item['case_id']) if r['confirmed'].lower() in {'true','1'}}
            for row in prediction['predictions']:
                known=truth.get(row['actor_id'])
                if known:labelled+=1
                if not row['prediction_available']:abstentions.append({'case_id':item['case_id'],**row});continue
                if known:
                    eligible+=1;match=row['role']==known['role_label'] and row['level']==known['level_label'];correct+=int(match)
                    if not match:person_errors.append({'case_id':item['case_id'],**row,'truth_role':known['role_label'],'truth_level':known['level_label']})
            lookup={(r['from_actor'],r['to_actor']):int(r['label']) for r in aux['controls'] if r['case_id']==item['case_id']}
            selected={(graph.nodes[r['from']]['entity_id'],graph.nodes[r['to']]['entity_id']) for r in prediction['control_candidates']}
            for pair,y in lookup.items():control_predictions.append((y,int(pair in selected)))
        cy=np.array(control_predictions);tp=int(((cy[:,0]==1)&(cy[:,1]==1)).sum());fp=int(((cy[:,0]==0)&(cy[:,1]==1)).sum());fn=int(((cy[:,0]==1)&(cy[:,1]==0)).sum())
        full['selective']={'labelled_persons':labelled,'eligible':eligible,'coverage':eligible/max(1,labelled),'joint_accuracy_on_eligible':correct/max(1,eligible),'abstentions':abstentions,'person_errors':person_errors}
        full['displayed_control_candidates']={'tp':tp,'fp':fp,'fn':fn,'precision':tp/max(1,tp+fp),'recall':tp/max(1,tp+fn),'f1':2*tp/max(1,2*tp+fp+fn),'scope':'加入证据支持、人员拒判和DAG约束后的实际候选；与原控制头指标分别报告'}
        full['fund_errors']=[r for r in full['per_case'] if int(r['fund_score']>=.5)!=r['fund_label']]
        runs.append({'name':name,'origin':dataset.manifest['origin'],'dataset_fingerprint':dataset.fingerprint,'domain_groups':groups,'metrics':full})
    assert all(torch.equal(v,weights_before[k]) for k,v in model.network.state_dict().items())
    result={'version':'1.5.0','model_id':model.metadata['model_id'],'model_weights_sha256':model.metadata['weights_sha256'],'selection':'冻结后评测，未更新权重、温度、阈值或指针','runs':runs,'scope':'合成域偏移压力测试不能证明真实地区、真实案型或真实团伙泛化；须另收集完整独立来源进行验证'}
    directory=Path(output_root)/datetime.now(TZ).strftime('domain_%Y%m%d_%H%M%S_%f');directory.mkdir(parents=True,exist_ok=False)
    (directory/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    e=lambda s:html.escape(str(s),quote=True)
    rows=[]
    for r in runs:
        m=r['metrics'];rows.append('<tr>'+''.join('<td>'+e(v)+'</td>' for v in [r['name'],m['cases'],m['fund']['f1'],f"{m['roles']['macro_f1']:.4f}",f"{m['levels']['macro_f1']:.4f}",f"{m['control']['f1']:.4f}",f"{m['displayed_control_candidates']['f1']:.4f}",f"{m['selective']['coverage']:.2%}",len(m['fund_errors']),len(m['selective']['person_errors'])])+'</tr>')
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>冻结模型外部域验证</title><style>body{font:16px/1.7 Microsoft YaHei UI,sans-serif;max-width:1200px;margin:30px auto;padding:24px;color:#172b44}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:12px;border-bottom:1px solid #dbe3ee;text-align:left}aside{background:#fff0d7;padding:18px}</style><h1>1.5 冻结模型外部域验证</h1><aside>'+e(result['scope'])+'</aside><p>'+e(result['selection'])+'</p><table><tr>'+''.join('<th>'+h+'</th>' for h in ['集合','案件','资金F1','角色宏F1','层级宏F1','控制头F1','实际控制候选F1','人员覆盖率','资金错例','人员错例'])+'</tr>'+''.join(rows)+'</table><p>逐域指标、混淆矩阵、校准分箱、拒判、实际候选控制边和错误均保存在result.json；测试标签不参与选择。</p></html>'
    (directory/'report.html').write_text(page,encoding='utf-8');(directory/'checksums.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()},indent=2),encoding='utf-8')
    return directory,result
