"""固定训练/验证方案后训练1.3；独立种子评估不用于调参与校准。"""
from pathlib import Path
import json
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from fourflow.extended_data import create_extended_dataset
from fourflow.core import load_dataset
from fourflow.model import TemporalHeteroGNN,train_model,resolve_model_directory
from fourflow.roles import RoleHierarchyModel,train_roles
from fourflow.evaluation import evaluate_models


def main():
    train_dir=ROOT/'data/stress_training_synthetic';holdout_dir=ROOT/'data/stress_holdout_synthetic'
    for path,count,seed,ns in [(train_dir,96,20261028,'TRN13'),(holdout_dir,24,619,'EVAL13')]:
        if not path.exists():create_extended_dataset(path,count,seed,namespace=ns,varied=True)
        manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
        if manifest['origin']!='synthetic' or manifest.get('generator')!={'seed':seed,'varied':True,'namespace':ns,'interpretation':'随机生成机制与业务标签均为合成设定；未标注背景主体不作为负类'}:raise ValueError('固定训练/评估目录定义发生变化，停止')
    training=load_dataset(train_dir);holdout=load_dataset(holdout_dir);old_fund=TemporalHeteroGNN.load(resolve_model_directory(ROOT))
    pointer=Path(json.loads((ROOT/'runtime/models/roles_latest.json').read_text())['path']);old_role=RoleHierarchyModel.load(pointer if pointer.is_absolute() else ROOT/pointer)
    output=ROOT/'runtime/upgrade_1_3';output.mkdir(parents=True,exist_ok=True)
    (output/'before.json').write_text(json.dumps(evaluate_models(holdout,old_fund,old_role),ensure_ascii=False,indent=2),encoding='utf-8')
    fund_dir=ROOT/'runtime/models/funds_1_3';role_dir=ROOT/'runtime/models/roles_1_3'
    if fund_dir.exists() or role_dir.exists():raise ValueError('1.3模型目录已存在，保留旧版本，不自动覆盖或反复调参')
    fund=train_model(training,fund_dir,70,print);roles=train_roles(training,role_dir,100,print)
    validation_ok=fund.metadata['metrics']['validation']['f1']>=.8 and min(roles.metadata['metrics']['validation'][key]['macro_f1'] for key in ('roles','levels'))>=.75 and roles.metadata['metrics']['validation']['selective']['coverage']>=.65
    after=evaluate_models(holdout,fund,roles);(output/'after.json').write_text(json.dumps(after,ensure_ascii=False,indent=2),encoding='utf-8')
    record={'train_seed':20261028,'holdout_seed':619,'train_cases':96,'holdout_cases':24,'fund_epochs':70,'role_epochs':100,'validation_acceptance':{'fund_f1_min':.8,'role_and_level_macro_f1_min':.75,'role_coverage_min':.65},'validation_accepted':validation_ok,'holdout_used_for_training_or_selection':False,'note':'不同种子、不同资产与网络域的合成评估；仍来自相关生成机制，不是现实外部验证。1.2权重比较使用本次修正后的运行代码。'}
    if validation_ok:
        (ROOT/'runtime/models/latest.json').write_text(json.dumps({'path':fund_dir.relative_to(ROOT).as_posix()}),encoding='utf-8')
        (ROOT/'runtime/models/roles_latest.json').write_text(json.dumps({'path':role_dir.relative_to(ROOT).as_posix()}),encoding='utf-8')
    (output/'training_record.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'accepted':validation_ok,'fund_validation':fund.metadata['metrics']['validation'],'role_validation':roles.metadata['metrics']['validation'],'holdout_fund':after['fund'],'holdout_selective':after['selective']},ensure_ascii=False))


if __name__=='__main__':main()
