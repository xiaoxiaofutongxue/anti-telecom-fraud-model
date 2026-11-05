"""开发压力集暴露退化后，仅按既定验证集选择资金权重；另留最终验收集。"""
from pathlib import Path
from datetime import datetime
import json,sys
import numpy as np

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from fourflow.core import load_dataset,build_case_graph,TZ
from fourflow.model import TemporalHeteroGNN,metrics,graph_fingerprint
from fourflow.roles import RoleHierarchyModel
from fourflow.model_quality import flow_reference
from fourflow.extended_data import create_extended_dataset
from fourflow.evaluation import evaluate_models


def main():
    output=ROOT/'runtime/upgrade_1_3';before=json.loads((output/'before.json').read_text(encoding='utf-8'));training=load_dataset(ROOT/'data/stress_training_synthetic')
    old_dirs=[p.parent for p in (ROOT/'runtime/models').glob('*/model_card.json') if json.loads(p.read_text(encoding='utf-8')).get('model_id')==before['fund_model_id']]
    if len(old_dirs)!=1:raise ValueError('无法唯一找到开发压力集比较前的资金权重')
    new_dir=ROOT/'runtime/models/funds_1_3';new=TemporalHeteroGNN.load(new_dir);ids=new.metadata['splits']['validation'];graphs={c:build_case_graph(training,c) for c in ids}
    labels={r['case_id']:int(r['label']) for r in training.tables['case_labels']};y=np.array([labels[c] for c in ids]);candidates=[]
    for path in (old_dirs[0],new_dir):
        model=TemporalHeteroGNN.load(path);previous_temperature=model.temperature;logits=np.array([model.forward(__import__('fourflow.model',fromlist=['encode_graph']).encode_graph(graphs[c]))[0] for c in ids])
        model.temperature=float(min(np.linspace(.5,5,91),key=lambda t:np.mean(np.logaddexp(0,logits/t)-y*logits/t)))
        score=metrics(y,[model.probability(graphs[c]) for c in ids]);candidates.append((model,{'path':path.relative_to(ROOT).as_posix(),'parent_model_id':model.metadata['model_id'],'previous_temperature':previous_temperature,'selected_temperature':model.temperature,'validation':score}))
    selected,card=max(candidates,key=lambda pair:(pair[1]['validation']['f1'],-pair[1]['validation']['calibration']['log_loss']))
    original=None
    for directory in ('role_training_synthetic','stress_training_synthetic','training_synthetic'):
        data=load_dataset(ROOT/'data'/directory)
        if data.fingerprint==selected.metadata['dataset_fingerprint']:original=data;break
    if original is None:raise ValueError('所选权重的原始训练快照不可验证，停止自动替换')
    original_graphs={c:build_case_graph(original,c) for ids_ in selected.metadata['splits'].values() for c in ids_};original_labels={r['case_id']:int(r['label']) for r in original.tables['case_labels']}
    selected.metadata['parent_model_id']=selected.metadata['model_id'];selected.metadata['parent_metrics']=selected.metadata['metrics']
    selected.metadata['model_id']=datetime.now(TZ).strftime('THGNN_1_3_%Y%m%d_%H%M%S');selected.metadata['temperature']=selected.temperature
    selected.metadata['metrics']={name:metrics([original_labels[c] for c in group],[selected.probability(original_graphs[c]) for c in group]) for name,group in selected.metadata['splits'].items()}
    selected.metadata['graph_fingerprints']={name:[graph_fingerprint(original_graphs[c]) for c in group] for name,group in selected.metadata['splits'].items()};selected.metadata['graph_fingerprint_version']=2
    selected.metadata['input_reference']=flow_reference(original_graphs,selected.metadata['splits']['train'])
    selected.metadata['deployment_validation']={'dataset_fingerprint':training.fingerprint,'case_ids':ids,'metrics':card['validation'],'selection':'先比较验证F1，再比较验证负对数似然；温度仅由验证标签拟合；开发压力集不作最终外部成绩'}
    path=ROOT/'runtime/models/funds_1_3_selected'
    if path.exists():raise ValueError('已冻结选中权重，不重复选择或覆盖')
    selected.save(path);(ROOT/'runtime/models/latest.json').write_text(json.dumps({'path':path.relative_to(ROOT).as_posix()}),encoding='utf-8')
    protocol={'development_dataset':'data/stress_holdout_synthetic','development_seed':619,'development_interpretation':'已查看并用于发现退化，属于开发压力集，不再称最终独立验收','selection_validation_dataset_fingerprint':training.fingerprint,'validation_case_ids':ids,'criterion':'validation F1 descending, validation NLL ascending','candidates':[r for _,r in candidates],'selected':card['parent_model_id'],'selected_weights_sha256':selected.metadata['weights_sha256'],'final_seed':1703,'final_cases':48,'final_dataset':'data/final_acceptance_synthetic','final_used_for_selection':False}
    (output/'selection_protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2),encoding='utf-8')
    final=ROOT/'data/final_acceptance_synthetic'
    if final.exists():raise ValueError('最终验收集已存在，不重复查看后再选模型')
    create_extended_dataset(final,48,1703,namespace='FINAL13',varied=True)
    pointer=Path(json.loads((ROOT/'runtime/models/roles_latest.json').read_text())['path']);roles=RoleHierarchyModel.load(pointer if pointer.is_absolute() else ROOT/pointer)
    result=evaluate_models(load_dataset(final),selected,roles);(output/'final_acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'selected':card['parent_model_id'],'validation_candidates':[{'parent':r['parent_model_id'],'f1':r['validation']['f1'],'nll':r['validation']['calibration']['log_loss']} for _,r in candidates],'final_fund':result['fund'],'final_roles_macro_f1':result['roles']['macro_f1'],'final_selective':result['selective']},ensure_ascii=False))


if __name__=='__main__':main()
