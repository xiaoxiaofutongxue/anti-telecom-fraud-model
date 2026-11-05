"""Source transcription evaluation. These scores never measure criminality or case classification."""
from collections import Counter,defaultdict
from decimal import Decimal,InvalidOperation
import hashlib,json
from pathlib import Path


def _number(value):
    n=Decimal(str(value))
    if not n.is_finite():raise ValueError('对照值必须为有限数值')
    return format(n.normalize(),'f')


def evaluate_reference(corpus,reference):
    corpus.validate()
    if reference.get('kind')!='narrative_source_reference' or reference.get('schema_version')!=1:raise ValueError('不支持的原文对照清单')
    checks=reference.get('checks',[])
    if not checks or len(checks)>5000:raise ValueError('原文对照清单需包含1至5000项检查')
    source_map={c['case_id']:c['sha256'] for c in corpus.cases}
    if not reference.get('sources') or any(source_map.get(s.get('case_id'))!=s.get('sha256') for s in reference['sources']):raise ValueError('对照清单与原件指纹不一致，不能复用旧答案')
    lookup={(r['case_id'],r['sheet'],r['position']):r for r in corpus.records}
    results=[];metrics=defaultdict(lambda:Counter(tp=0,fp=0,fn=0))
    for check in checks:
        if check.get('case_id') not in {s['case_id'] for s in reference['sources']}:raise ValueError('对照项引用了未声明来源')
        row=lookup.get((check['case_id'],check['sheet'],check['row']));feature=check['feature'];field=check.get('field','');expected=check['expected'];actual=None
        if feature=='amount':
            expected=[(_number(v),c) for v,c in expected]
            actual=[(_number(a['value']),a['currency']) for a in (row or {}).get('amounts',[]) if a['value'] is not None and a['field']==field]
        elif feature in {'coordinate','domain','wallet_fragment'}:
            actual=[]
            for a in (row or {}).get('assets',[]):
                if a['kind']!=feature:continue
                if a.get('field')!=field and a['raw'] not in (row or {}).get('fields',{}).get(field,''):continue
                value=a['value']
                if feature=='coordinate':value=','.join(f'{Decimal(v):.6f}' for v in value.split(','))
                actual.append(value)
        elif feature=='field':actual=(row or {}).get('fields',{}).get(field)
        elif feature=='flows':actual=sorted((row or {}).get('flows',[]));expected=sorted(expected)
        elif feature=='open_end':actual=(row or {}).get('time',{}).get('open_end')
        elif feature=='source_context':
            text=(row or {}).get('source_citation','');actual=[v for v in expected if v in text]
        else:raise ValueError('不支持的对照类型：'+str(feature))
        if feature in {'amount','coordinate','domain','wallet_fragment'}:
            a,b=Counter(actual),Counter(expected);tp=sum((a&b).values());fp=sum((a-b).values());fn=sum((b-a).values());metrics[feature].update(tp=tp,fp=fp,fn=fn);passed=a==b
        else:passed=actual==expected
        results.append({**check,'actual':actual,'passed':passed,'locator':check['sheet']+' / '+str(check['row'])})
    for m in metrics.values():
        m['precision']=m['tp']/(m['tp']+m['fp']) if m['tp']+m['fp'] else None
        m['recall']=m['tp']/(m['tp']+m['fn']) if m['tp']+m['fn'] else None
    return {'version':'1.7.0','evaluation_kind':'source_transcription_consistency','scope':reference.get('evaluation_scope','原文转录核对'),'review_status':reference.get('review_status','unknown'),'corpus_fingerprint':corpus.fingerprint,'reference_sha256':hashlib.sha256(json.dumps(reference,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),'passed':sum(r['passed'] for r in results),'failed':sum(not r['passed'] for r in results),'checks':results,'extraction_metrics':dict(metrics),'classification_accuracy':None,'classification_validated':False,'limitations':['仅评价对照清单指定单元格，不是全部文书召回率','原文转录正确不表示原文事实已核实','不计算诈骗、个人角色或团伙控制的真实识别准确率']}


def load_reference(path):
    path=Path(path)
    if path.stat().st_size>10*1024*1024:raise ValueError('原文对照清单过大')
    return json.loads(path.read_text(encoding='utf-8-sig'))
