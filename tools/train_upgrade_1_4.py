"""先选模型并冻结，再生成外部合成域。再次运行必须换新目录，防覆盖。"""
from pathlib import Path
import sys,json,hashlib,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from fourflow.advanced_data import create_advanced_dataset
from fourflow.core import load_dataset
from fourflow.dual_model import train_joint


def main():
    run=ROOT/'runtime/upgrade_1_4';run.mkdir(exist_ok=False);started=time.perf_counter()
    data=load_dataset(create_advanced_dataset(ROOT/'data/advanced_training_synthetic',96,414,'TRAIN14'))
    models=[]
    for robust,label in [(False,'erm'),(True,'dro')]:
        model=train_joint(data,ROOT/'runtime/models'/('joint_1_4_'+label),60,414,print,robust=robust)
        score=min(r['validation_loss'] for r in model.metadata['history']);models.append((score,label,model))
        print(label,'validation',score,'internal test fund',model.metadata['metrics']['test']['fund']['f1'],flush=True)
    _,selected,model=min(models,key=lambda row:(row[0],row[1]));directory=ROOT/'runtime/models'/('joint_1_4_'+selected)
    record={'origin':'synthetic','criterion':'仅固定验证集的复合监督损失最小，平局按候选名称；外部域未生成','candidates':[{'name':label,'validation_loss':score,'model_id':m.metadata['model_id'],'path':str((ROOT/'runtime/models'/('joint_1_4_'+label)).relative_to(ROOT))} for score,label,m in models],'selected':selected,'model_id':model.metadata['model_id'],'weights_sha256':model.metadata['weights_sha256'],'frozen':True,'external_protocol':[{'name':'region','seed':1417,'count':24,'region':'合成未见区域丙','case_type':None,'shift':1},{'name':'type','seed':1423,'count':24,'region':None,'case_type':'合成未见场景丙','shift':2},{'name':'joint','seed':1429,'count':24,'region':'合成未见区域丁','case_type':'合成未见场景丁','shift':3}]}
    (run/'selection_protocol.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'runtime/models/joint_latest.json').write_text(json.dumps({'path':directory.relative_to(ROOT).as_posix()}),encoding='utf-8')
    print('FROZEN',selected,record['weights_sha256'],flush=True)
    # 此后不再训练或选择；只生成评测及单独演示数据。
    for p in record['external_protocol']:create_advanced_dataset(ROOT/'data'/f"advanced_{p['name']}_holdout_synthetic",p['count'],p['seed'],'E14'+p['name'].upper(),p['region'],p['case_type'],p['shift'])
    create_advanced_dataset(ROOT/'data/advanced_sample_synthetic',12,1441,'DEMO14')
    (run/'training_record.json').write_text(json.dumps({'elapsed_seconds':time.perf_counter()-started,'selected':selected,'gradient_evidence':model.metadata['gradient_evidence'],'metrics':model.metadata['metrics']},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Training, freeze, and independent synthetic domain generation complete',flush=True)


if __name__=='__main__':main()
