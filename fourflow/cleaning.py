"""保守清洗：原始数据不改写；冲突、缺失关键值和格式错误隔离，修订逐项留痕。"""
from __future__ import annotations

import csv
import io
import hashlib
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

from .core import SCHEMA, IDS, OPTIONAL_COLUMNS, TIME_TABLES, SERVICE_TYPES, parse_time, money, normalize_ip, ip_scope, load_dataset


def clean_dataset(source, destination):
    source, destination=Path(source).resolve(),Path(destination).resolve()
    if source==destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("清洗输出必须使用与原始数据互不包含的新目录")
    if destination.exists() and any(destination.iterdir()):raise ValueError("清洗输出目录必须为空")
    manifest_path=source/"manifest.json"
    manifest=json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("origin") not in {"real","synthetic"} or manifest.get("schema_version") not in {1,2}:raise ValueError("请先明确 manifest 的版本和真实/合成来源")
    if manifest.get("timezone","Asia/Shanghai")!="Asia/Shanghai":raise ValueError("请先确认时区，清洗不猜测时区")
    upstream={};intake_path=None
    if manifest.get('intake'):
        original=load_dataset(source)
        intake_path=source/manifest.pop('intake')
        upstream=json.loads(intake_path.read_text(encoding='utf-8'))
    if manifest.get('cleaning')=='cleaning_report.json':
        upstream_path=source/'cleaning_report.json'
        if not upstream_path.is_file():raise ValueError('原清洗溯源文件缺失，不能假装是首次清洗')
        upstream=json.loads(upstream_path.read_text(encoding='utf-8'))
        if upstream.get('parent_intake_sha256'):
            load_dataset(source)
            intake_path=source/'lineage/intake_report.json'
    destination.mkdir(parents=True,exist_ok=True)
    tables={t:[] for t in SCHEMA}; audit=[]; rejected=[]; fingerprints={}; counts={}; origins={};invalid_keys=set();ignored_columns={}

    def reject(table,row,reason):
        rejected.append({"table":table,"original_line":row["_line"],"record_id":row.get(IDS[table],""),"reason":reason,"raw":row["_raw"]})
        invalid_keys.add((table,row.get('case_id',''),row.get(IDS[table],'')))

    for table,fields in SCHEMA.items():
        path=source/(table+".csv")
        if not path.exists():
            if table in {"cases","sources"}:raise ValueError(f"缺少 {table}.csv")
            continue
        contents=path.read_bytes();fingerprints[path.name]=hashlib.sha256(contents).hexdigest()
        grouped=defaultdict(list); total=0
        with io.StringIO(contents.decode('utf-8-sig'),newline='') as stream:
            reader=csv.DictReader(stream)
            headers=[(f or "").strip() for f in reader.fieldnames or []]
            if len(headers)!=len(set(headers)):raise ValueError(f"{path.name} 列名重复")
            ignored_columns[table]=sorted(set(headers)-set(fields))
            if not (set(fields)-OPTIONAL_COLUMNS.get(table,set())).issubset(headers):raise ValueError(f"{path.name} 缺少必要列，请按模板对应列名")
            reader.fieldnames=headers
            for raw in reader:
                total+=1
                row={k:(raw.get(k) or "").strip() for k in fields}
                row.update(_line=reader.line_num,_raw=raw)
                edits=[]
                try:
                    if None in raw or any(v is None for v in raw.values()):raise ValueError("字段数量与表头不符")
                    if not re.fullmatch(r"[\w-]{1,64}",row["case_id"]) or not row[IDS[table]]:raise ValueError("编号缺失或 case_id 格式错误")
                    for k in fields:
                        if raw.get(k,"")!=row[k]:edits.append(k+" 去除首尾空白")
                    for flag in {"actors":["role_verified"],"identity_links":["verified"],"relations":["verified"],"case_labels":["confirmed"],"person_labels":["confirmed"]}.get(table,[]):
                        mapping={"true":"true","false":"false","1":"true","0":"false","是":"true","否":"false"}
                        if row[flag].lower() not in mapping:raise ValueError(flag+" 必须明确填写，不能补猜")
                        new=mapping[row[flag].lower()]
                        if row[flag]!=new:edits.append(flag+" 统一布尔格式")
                        row[flag]=new
                    if table in TIME_TABLES:
                        value=parse_time(row["timestamp"]).isoformat()
                        if value!=row["timestamp"]:edits.append("时间统一为带+08:00偏移的ISO格式")
                        row["timestamp"]=value
                    if table=="funds":
                        amount=row["amount"]
                        if ',' in amount:
                            if not re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?",amount):raise ValueError("金额分组不明确，拒绝猜测")
                            amount=amount.replace(',','');edits.append("移除规范千位分隔符")
                        row["amount"]=str(money(amount))
                        if row["currency"]!=row["currency"].upper():edits.append("币种统一大写")
                        row["currency"]=row["currency"].upper()
                        if not re.fullmatch(r'[A-Z][A-Z0-9._-]{0,31}',row['currency']):raise ValueError('币种代码无效，不猜测币种')
                        if row["from_kind"] not in {"wallet","account"} or row["to_kind"] not in {"wallet","account"}:raise ValueError("无效账户类型")
                        if not all(row[k] for k in ["from_id","to_id","chain","currency"]):raise ValueError("缺少资金必要字段")
                    if table=="network_events":
                        value=normalize_ip(row["ip"])
                        if value!=row["ip"]:edits.append("IP规范化，IPv4映射地址统一")
                        row["ip"]=value;ip_scope(value,row["network_scope"])
                        if not row["actor_id"] and not row["account_id"]:raise ValueError("网络事件缺少主体或账户锚点")
                        if row["account_id"] and (row["account_kind"] not in {"wallet","account"} or not row["chain"]):raise ValueError("账户域信息缺失")
                        if not row["event_type"]:raise ValueError("网络事件类型缺失")
                        if row["source_port"] and (not row["source_port"].isdigit() or not 1<=int(row["source_port"])<=65535):raise ValueError("端口无效")
                    if table=="shipments":
                        row["service_type"]=row["service_type"] or "unspecified"
                        if row["service_type"] not in SERVICE_TYPES:raise ValueError("无效产业类别")
                    if table=="case_labels" and row["label"] not in {"0","1"}:raise ValueError("label仅支持0或1")
                    if table=='sources' and (not row['title'] or not row['reference'] or (row['reference'].startswith('ref:') and not row['reference'][4:].strip())):raise ValueError('来源标题和材料引用不能为空；不能用空来源通过校验')
                    if table=="identity_links" and row["entity_kind"] not in {"wallet","account"}:raise ValueError("无效身份关联类型")
                    required={"person_labels":["actor_id","role_label","level_label"],"shipments":["order_id","from_place","to_place"],"communications":["from_actor","to_actor","channel"],"identity_links":["actor_id","entity_id","chain"],"relations":["from_actor","to_actor","relation"]}.get(table,[])
                    if any(not row[k] for k in required):raise ValueError("必要关联字段缺失，不自动填补")
                    row["_edits"]=edits
                    grouped[(row["case_id"],row[IDS[table]])].append(row)
                except (ValueError,TypeError) as exc:reject(table,row,str(exc))
        for key,rows in grouped.items():
            variants={tuple(r[k] for k in fields) for r in rows}
            if len(variants)>1 or (table,key[0],key[1]) in invalid_keys:
                for row in rows:reject(table,row,"同编号内容冲突，整组隔离，不任意保留首条")
                continue
            tables[table].append(rows[0])
            for row in rows:
                if row["_edits"]:audit.append({"table":table,"original_line":row["_line"],"record_id":row[IDS[table]],"changes":row["_edits"]})
            for row in rows[1:]:audit.append({"table":table,"original_line":row["_line"],"record_id":row[IDS[table]],"changes":["规范化后完全相同的重复记录移除"],"kept_original_line":rows[0]["_line"]})
        counts[table]={"input":total}
    # 引用完整性按依赖顺序过滤，不让隔离的主体或材料继续参与推理。
    cases={r["case_id"] for r in tables["cases"]}
    for table in SCHEMA:
        keep=[]
        for row in tables[table]:
            if row["case_id"] not in cases:reject(table,row,"案件编号不存在或已隔离");continue
            if table=="sources":
                ref=row["reference"]
                path=(source/ref).resolve()
                if ref and not ref.startswith("ref:") and not path.is_relative_to(source):reject(table,row,"来源文件越出数据集目录");continue
                if ref and not ref.startswith('ref:') and (Path(ref).is_absolute() or Path(ref).drive):reject(table,row,'来源文件必须为数据集内相对路径');continue
                reserved={t+'.csv' for t in SCHEMA}|{'manifest.json','cleaning_report.json'}
                if ref and not ref.startswith('ref:') and path.relative_to(source).as_posix() in reserved:reject(table,row,"来源文件与清洗输出保留文件重名，请先放入独立材料子目录");continue
            keep.append(row)
        tables[table]=keep
    source_ids={(r["case_id"],r["source_id"]) for r in tables["sources"]}
    for table in SCHEMA:
        tables[table]=[r for r in tables[table] if _reference_check(table,r,source_ids,reject)]
    actors={(r["case_id"],r["actor_id"]) for r in tables["actors"]}
    for table in SCHEMA:
        keep=[]
        for row in tables[table]:
            refs={"person_labels":["actor_id"],"network_events":["actor_id"],"identity_links":["actor_id"],"relations":["from_actor","to_actor"],"communications":["from_actor","to_actor"],"shipments":["sender_id","receiver_id","carrier_id"]}.get(table,[])
            if any(row[k] and (row["case_id"],row[k]) not in actors for k in refs):reject(table,row,"主体不存在或已隔离");continue
            keep.append(row)
        tables[table]=keep
    for row in tables["sources"]:
        ref=row["reference"]
        if ref and not ref.startswith("ref:"):
            path=(source/ref).resolve()
            if path.is_file():
                target=destination/ref;target.parent.mkdir(parents=True,exist_ok=True)
                # 只复制来源文件内容，不打开或执行。
                shutil.copyfile(path,target)
    manifest.update(schema_version=2,cleaning="cleaning_report.json")
    if manifest.get('evaluation_groups'):
        value=manifest['evaluation_groups'];group_path=(source/value).resolve()
        if not group_path.is_relative_to(source) or not group_path.is_file() or hashlib.sha256(group_path.read_bytes()).hexdigest()!=manifest.get('evaluation_groups_sha256'):raise ValueError('评测分组文件缺失、越界或指纹不一致')
        target=destination/value;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(group_path,target)
    if manifest.get('configuration'):
        value=manifest['configuration'];path=(source/value).resolve()
        if not path.is_relative_to(source) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=manifest.get('configuration_sha256'):raise ValueError('任务配置留痕文件缺失或指纹不匹配')
        target=destination/value;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
    (destination/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    for table,fields in SCHEMA.items():
        with (destination/(table+".csv")).open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(tables[table])
        counts.setdefault(table,{"input":0})["kept"]=len(tables[table])
        origins[table]={}
        for row in tables[table]:
            key=row[IDS[table]]+'|'+row['case_id'];previous={}
            if upstream.get('valid') is True and upstream.get('output_sha256',{}).get(table+'.csv')==fingerprints.get(table+'.csv'):
                previous=upstream.get('origins',{}).get(table,{}).get(key,{})
            stage={'line':row['_line'],'sha256':fingerprints.get(table+'.csv','')}
            origins[table][key]={'line':previous.get('line',stage['line']),'sha256':previous.get('sha256',stage['sha256']),'sheet':previous.get('sheet',''),'stages':previous.get('stages',[])+[stage]}
    outputs={t+'.csv':hashlib.sha256((destination/(t+'.csv')).read_bytes()).hexdigest() for t in SCHEMA}
    report={"valid":True,"source_directory":str(source),"counts":counts,"changes":audit,"quarantined":rejected,"input_sha256":fingerprints,"output_sha256":outputs,"origins":origins,"ignored_columns":ignored_columns,"policy":"不补造主体、金额、时间、IP或标签；不以数值极端为由删除可能重要的侦查记录。未建模的额外列留在原始文件，不作为推理输入。"}
    if intake_path is not None:
        lineage=destination/'lineage';lineage.mkdir(exist_ok=True);shutil.copyfile(intake_path,lineage/'intake_report.json');report['parent_intake_sha256']=hashlib.sha256(intake_path.read_bytes()).hexdigest()
    (destination/'cleaning_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    try:load_dataset(destination)
    except ValueError as exc:report.update(valid=False,validation_error=str(exc))
    (destination/'cleaning_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def _reference_check(table,row,source_ids,reject):
    source=row.get('source_id') if table!='sources' else None
    if table in {'case_labels','person_labels'}:source=row['label_source']
    if source is not None and (row['case_id'],source) not in source_ids:
        reject(table,row,"材料来源不存在或已隔离");return False
    return True
