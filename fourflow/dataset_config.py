"""在新副本中配置监督任务及提供方声明的隔离分组，不生成任何标签。"""
from pathlib import Path
from datetime import datetime
import csv,hashlib,io,json,shutil
from .core import load_dataset,TZ
from .model import label_definition
from .roles import role_task
from .validation_groups import FIELDS
from .io_utils import atomic_json


def group_template(dataset):
    stream=io.StringIO(newline='');writer=csv.writer(stream);writer.writerow(FIELDS)
    for case in dataset.tables['cases']:
        source=next((r['source_id'] for r in dataset.tables['sources'] if r['case_id']==case['case_id']),'')
        writer.writerow([case['case_id'],'','case_group','false',source])
    return stream.getvalue()


def configure_copy(dataset,destination,task_config,group_csv,reviewer,note):
    if not reviewer.strip() or not note.strip():raise ValueError('请填写配置人及标签/分组依据')
    if not isinstance(task_config,dict) or not set(task_config).issubset({'label_definition','role_task','control_task'}):raise ValueError('任务配置字段无效')
    if task_config.get('label_definition',{}).get('task_id'):label_definition(task_config)
    if task_config.get('role_task',{}).get('task_id'):role_task(task_config)
    destination=Path(destination).resolve();source=dataset.root.resolve()
    if destination==source or destination.is_relative_to(source) or source.is_relative_to(destination) or (destination.exists() and any(destination.iterdir())):raise ValueError('任务配置只能写入与原数据互不包含的新空目录')
    if load_dataset(source).fingerprint!=dataset.fingerprint:raise ValueError('原数据已经变化，请重新载入')
    for p in source.rglob('*'):
        if p.is_symlink() or not p.resolve().is_relative_to(source):raise ValueError('数据目录含越界链接，停止复制')
    shutil.copytree(source,destination);manifest=dict(dataset.manifest)
    for key in ('label_definition','role_task'):
        if key in task_config:manifest[key]=task_config[key]
    if task_config.get('control_task',{}).get('task_id'):
        from .advanced_data import CONTROL_FIELDS
        advanced=dict(manifest.get('advanced',{}));advanced.update(version=1,control_task=task_config['control_task'])
        for key,name in [('control_file','control_labels.csv'),('media_file','multimodal_links.json')]:advanced.setdefault(key,name)
        for key in ('control_file','media_file'):
            path=(destination/advanced[key]).resolve()
            if not path.is_relative_to(destination):raise ValueError('高级配置文件路径越界')
            if not path.exists():
                if key=='control_file':
                    with path.open('w',encoding='utf-8-sig',newline='') as f:csv.writer(f).writerow(CONTROL_FIELDS)
                else:path.write_text('[]',encoding='utf-8')
        advanced['sha256']={advanced[key]:hashlib.sha256((destination/advanced[key]).read_bytes()).hexdigest() for key in ('control_file','media_file')};manifest['advanced']=advanced
    if group_csv.strip():
        reader=csv.DictReader(io.StringIO(group_csv,newline=''))
        if reader.fieldnames!=FIELDS:raise ValueError('分组列名须保持模板格式')
        rows=list(reader)
        with (destination/'evaluation_groups.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
        manifest.update(evaluation_groups='evaluation_groups.csv',evaluation_groups_sha256=hashlib.sha256((destination/'evaluation_groups.csv').read_bytes()).hexdigest())
    record={'created_at':datetime.now(TZ).isoformat(),'parent_dataset_fingerprint':dataset.fingerprint,'reviewer':reviewer.strip(),'note':note.strip(),'task_config':task_config,'scope':'仅配置类别含义和提供方确认的关联分组；未创建或修改案件、人员、控制监督标签。'}
    atomic_json(destination/'configuration_record.json',record);manifest.update(configuration='configuration_record.json',configuration_sha256=hashlib.sha256((destination/'configuration_record.json').read_bytes()).hexdigest());atomic_json(destination/'manifest.json',manifest)
    configured=load_dataset(destination)
    if not configured.synthetic:
        from .validation_groups import group_tokens
        group_tokens(configured,required=True)
    return configured
