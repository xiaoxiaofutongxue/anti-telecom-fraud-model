"""提供方声明的案件/人员分组只用于隔离验证，不作为神经网络输入。"""
from pathlib import Path
import csv,hashlib,io,json

FIELDS=['case_id','group_id','group_kind','confirmed','source_id']


def read_group_rows(root,manifest,tables):
    root=Path(root).resolve();value=manifest.get('evaluation_groups')
    if not isinstance(value,str) or Path(value).is_absolute() or Path(value).drive:raise ValueError('评测分组文件须为数据集内相对路径')
    path=(root/value).resolve()
    if not path.is_relative_to(root) or not path.is_file():raise ValueError('评测分组文件缺失或越界')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=manifest.get('evaluation_groups_sha256'):raise ValueError('评测分组文件指纹不匹配')
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'),newline=''))
    if reader.fieldnames!=FIELDS:raise ValueError('评测分组列名无效')
    rows=[];seen=set();cases={r['case_id'] for r in tables['cases']};sources={(r['case_id'],r['source_id']) for r in tables['sources']}
    for row in reader:
        if None in row or any(v is None for v in row.values()):raise ValueError('评测分组字段数量错误')
        row={k:v.strip() for k,v in row.items()};key=(row['case_id'],row['group_kind'],row['group_id'])
        if not row['group_id'] or len(row['group_id'])>128 or row['group_kind'] not in {'case_group','person','organization','source_batch'} or row['confirmed'].lower() not in {'true','1'} or row['case_id'] not in cases or (row['case_id'],row['source_id']) not in sources or key in seen:raise ValueError('评测分组必须有明确已确认分组、来源、有效案件且不能重复')
        rows.append(row);seen.add(key)
    return {'rows':rows,'raw':raw}


def group_tokens(dataset,case_ids=None,required=False):
    if not dataset.manifest.get('evaluation_groups'):
        if required:raise ValueError('真实训练/外部实证需要evaluation_groups.csv及清单指纹，先核实关联案件/人员分组')
        return {}
    result=read_group_rows(dataset.root,dataset.manifest,dataset.tables);ids=set(case_ids or [r['case_id'] for r in dataset.tables['cases']]);rows=[r for r in result['rows'] if r['case_id'] in ids]
    if required and {r['case_id'] for r in rows if r['group_kind']=='case_group'}!=ids:raise ValueError('每个实证案件至少需要一个经核实case_group；同团伙/关联案件使用同一分组')
    output={cid:set() for cid in ids}
    for row in rows:output[row['case_id']].add(hashlib.sha256(json.dumps([row['group_kind'],row['group_id']],ensure_ascii=False).encode()).hexdigest())
    return output


def check_group_splits(dataset,splits):
    ids=[c for cases in splits.values() for c in cases];groups=group_tokens(dataset,ids,required=not dataset.synthetic);seen={};result={}
    for name,cases in splits.items():
        tokens=set().union(*(groups.get(cid,set()) for cid in cases))
        for token in tokens:
            if token in seen:raise ValueError('案件或已知人员/组织分组跨训练、验证、测试集合，请先按关联分组合并案件或调整时间范围')
            seen[token]=name
        result[name]=sorted(tokens)
    return result
