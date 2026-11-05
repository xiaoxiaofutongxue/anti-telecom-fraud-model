from __future__ import annotations

import csv
import io
import hashlib
import json
import ipaddress
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

import networkx as nx

TZ = timezone(timedelta(hours=8))
FLOWS = {"funds": "资金流", "goods": "产业流", "information": "信息流", "people": "人员流"}
KINDS = {"actor": "主体", "wallet": "链上地址", "account": "法币账户", "order": "产业订单", "place": "地点", "vehicle": "运输工具", "device": "通信载体", "exchange": "兑换事件", "ip": "IP地址"}
SERVICE_TYPES = {"telecom_equipment":"通信设备", "account_service":"账户服务", "software_service":"软件服务", "logistics":"物流服务", "other":"其他", "unspecified":"未分类"}
SCHEMA = {
    "cases": ["case_id", "title", "region", "case_type"],
    "sources": ["case_id", "source_id", "title", "reference"],
    "actors": ["case_id", "actor_id", "alias", "actor_type", "role", "role_verified", "source_id"],
    "funds": ["case_id", "tx_id", "from_id", "to_id", "from_kind", "to_kind", "chain", "timestamp", "amount", "currency", "order_id", "exchange_id", "source_id"],
    "shipments": ["case_id", "shipment_id", "order_id", "from_place", "to_place", "sender_id", "receiver_id", "carrier_id", "vehicle_id", "timestamp", "goods_label", "service_type", "source_id"],
    "network_events": ["case_id", "access_id", "actor_id", "account_id", "account_kind", "chain", "ip", "device_id", "timestamp", "event_type", "source_port", "network_scope", "source_id"],
    "communications": ["case_id", "event_id", "from_actor", "to_actor", "timestamp", "channel", "device_id", "summary", "source_id"],
    "identity_links": ["case_id", "link_id", "actor_id", "entity_kind", "entity_id", "chain", "timestamp", "verified", "verification_note", "source_id"],
    "relations": ["case_id", "relation_id", "from_actor", "to_actor", "relation", "timestamp", "verified", "source_id"],
    "case_labels": ["case_id", "label", "confirmed", "label_source"],
    "person_labels": ["case_id","actor_id","role_label","level_label","confirmed","label_source","timestamp"],
}
IDS = {"cases": "case_id", "sources": "source_id", "actors": "actor_id", "funds": "tx_id", "shipments": "shipment_id", "network_events":"access_id", "communications": "event_id", "identity_links": "link_id", "relations": "relation_id", "case_labels": "case_id","person_labels":"actor_id"}
OPTIONAL_COLUMNS = {"shipments": {"service_type"}}
TIME_TABLES = {"funds", "shipments", "network_events", "communications", "identity_links", "relations","person_labels"}


def normalize_ip(value):
    value=value.strip()
    if '%' in value:
        raise ValueError("IP 中不能附带接口区域标识，请单独填写 network_scope")
    address=ipaddress.ip_address(value)
    if isinstance(address,ipaddress.IPv6Address) and address.ipv4_mapped:
        address=address.ipv4_mapped
    return str(address)


def ip_scope(value, network_scope):
    address=ipaddress.ip_address(normalize_ip(value))
    if address.is_global and not address.is_multicast:return "global"
    if not network_scope.strip():
        raise ValueError("非公网 IP 必须填写 network_scope，防止跨网络误合并")
    if network_scope.strip().lower()=='global':raise ValueError('非公网地址不能使用保留网络域 global')
    return network_scope.strip()


def parse_time(value: str) -> datetime:
    if isinstance(value,datetime):return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
    if not isinstance(value,str):raise ValueError('时间必须为ISO日期字符串')
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)


def money(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation,TypeError,ValueError) as exc:
        raise ValueError(f"无效金额：{value}") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError("交易金额必须为有限正数")
    if len(result.as_tuple().digits)>1000 or abs(result.as_tuple().exponent)>1000:raise ValueError('金额超出支持精度：最多1000位有效数字，指数绝对值不超过1000')
    return result


def is_true(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "是"}


@dataclass
class Dataset:
    root: Path
    manifest: dict
    tables: dict[str, list[dict]]
    fingerprint: str
    warnings: list[str] = field(default_factory=list)

    @property
    def synthetic(self):
        return self.manifest["origin"] == "synthetic"

    def rows(self, table, case_id):
        return [r for r in self.tables.get(table, []) if r["case_id"] == case_id]

    def snapshot_rows(self,table,case_id,as_of=None):
        cutoff=parse_time(as_of) if as_of else None
        return [r for r in self.rows(table,case_id) if not cutoff or '_time' not in r or r['_time']<=cutoff]

    def source(self, case_id, source_id):
        return next((r for r in self.rows("sources", case_id) if r["source_id"] == source_id), None)

    def evidence_file(self, case_id, source_id):
        source = self.source(case_id, source_id)
        if not source or not source["reference"] or source['reference'].startswith('ref:'):
            return None
        candidate = (self.root / source["reference"]).resolve()
        if not candidate.is_relative_to(self.root.resolve()):
            raise ValueError("来源文件必须位于数据集文件夹内")
        if candidate.is_file() and candidate.suffix.lower() in {".txt", ".csv", ".json", ".pdf", ".docx", ".png", ".jpg", ".jpeg",'.bmp','.tif','.tiff','.webp','.wav','.mp3','.m4a','.flac','.ogg','.aac','.wma','.mp4','.mkv','.avi','.mov','.webm'}:
            return candidate
        return None


def load_dataset(root: str | Path) -> Dataset:
    root = Path(root).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("数据文件夹缺少 manifest.json，请使用系统导出的数据模板")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") not in {1,2} or manifest.get("origin") not in {"real", "synthetic"}:
        raise ValueError("manifest 必须指定 schema_version=1/2、origin=real 或 synthetic")
    if manifest.get("timezone","Asia/Shanghai") != "Asia/Shanghai":
        raise ValueError("无时区日期仅支持 Asia/Shanghai；其他时区请先写入 ISO 日期偏移")
    tables, warnings, digest = {}, [], hashlib.sha256(manifest_path.read_bytes())
    for table, fields in SCHEMA.items():
        path = root / (table + ".csv")
        tables[table] = []
        if not path.is_file():
            if table in {"cases", "sources"}:
                raise ValueError(f"缺少必要数据表：{table}.csv")
            warnings.append(f"未提供 {table}.csv")
            continue
        digest.update(table.encode())
        contents=path.read_bytes();digest.update(contents)
        try:text=contents.decode('utf-8-sig')
        except UnicodeDecodeError as exc:raise ValueError(f'{path.name}不是有效UTF-8文件，请另存为UTF-8 CSV') from exc
        table_digest=hashlib.sha256(contents).hexdigest()
        with io.StringIO(text,newline='') as stream:
            reader = csv.DictReader(stream)
            required_fields=set(fields)-OPTIONAL_COLUMNS.get(table,set())
            if len(reader.fieldnames or [])!=len(set(reader.fieldnames or [])):
                raise ValueError(f"{path.name} 列名重复")
            if not required_fields.issubset(reader.fieldnames or []):
                raise ValueError(f"{path.name} 缺少列：{', '.join(sorted(required_fields - set(reader.fieldnames or [])))}")
            seen = set()
            for raw in reader:
                line=reader.line_num
                if None in raw or any(v is None for v in raw.values()):
                    raise ValueError(f"{path.name}:{reader.line_num} CSV字段数量与表头不一致")
                row = {k: (raw.get(k) or "").strip() for k in fields}
                row["_table"], row["_line"] = table, reader.line_num
                row['_original_line']=reader.line_num;row['_original_sha256']=table_digest;row['_lineage_verified']=True
                if not row["case_id"] or not row[IDS[table]]:
                    raise ValueError(f"{path.name}:{line} 缺少编号")
                if not re.fullmatch(r"[\w-]{1,64}",row["case_id"]):
                    raise ValueError(f"{path.name}:{line} case_id 只能含字母、数字、汉字、下划线或连字符，最长64字符")
                key = (row["case_id"], row[IDS[table]])
                if key in seen:
                    raise ValueError(f"{path.name}:{line} 编号重复：{key}")
                seen.add(key)
                try:
                    if table in TIME_TABLES:
                        row["_time"] = parse_time(row["timestamp"])
                    if table == "funds":
                        row["_amount"] = money(row["amount"])
                        if row["from_kind"] not in {"wallet", "account"} or row["to_kind"] not in {"wallet", "account"}:
                            raise ValueError("from_kind/to_kind 只能是 wallet 或 account")
                        if not all(row[k] for k in ("from_id", "to_id", "currency", "chain")):
                            raise ValueError("缺少转出方、转入方、币种或链/账户域标识")
                        if not re.fullmatch(r'[A-Z][A-Z0-9._-]{0,31}',row['currency']):raise ValueError('currency须为大写币种代码，如CNY或USDT；可先使用清洗功能')
                    if table in {"actors", "identity_links", "relations", "case_labels","person_labels"}:
                        flag = {"actors": "role_verified", "identity_links": "verified", "relations": "verified", "case_labels": "confirmed","person_labels":"confirmed"}[table]
                        if row[flag].lower() not in {"true", "false", "1", "0"}:
                            raise ValueError(f"{flag} 必须明确填写 true/false")
                    if table == "case_labels" and row["label"] not in {"0", "1"}:
                        raise ValueError("label 只能是 0 或 1，含义为经核实的目标异常模式有无")
                    if table == "identity_links" and row["entity_kind"] not in {"wallet", "account"}:
                        raise ValueError("identity_links.entity_kind 只能是 wallet 或 account")
                    if table == "shipments":
                        row["service_type"]=row["service_type"] or "unspecified"
                        if row["service_type"] not in SERVICE_TYPES:raise ValueError("service_type 不是支持的产业类别")
                    if table == "network_events":
                        row["ip"]=normalize_ip(row["ip"])
                        row["_scope"]=ip_scope(row["ip"],row["network_scope"])
                        if not row["actor_id"] and not row["account_id"]:raise ValueError("网络记录至少关联主体或账户")
                        if not row["event_type"]:raise ValueError("网络记录缺少 event_type")
                        if row["account_id"] and (row["account_kind"] not in {"wallet","account"} or not row["chain"]):raise ValueError("账户网络记录须填写 account_kind 和 chain")
                        if row["source_port"] and (not row["source_port"].isdigit() or not 1<=int(row["source_port"])<=65535):raise ValueError("source_port 须为1至65535或留空")
                except (ValueError, KeyError) as exc:
                    raise ValueError(f"{path.name}:{line} {exc}") from exc
                tables[table].append(row)
    cases = {r["case_id"] for r in tables["cases"]}
    if not cases:
        raise ValueError("cases.csv 不能为空")
    source_ids = {(r["case_id"], r["source_id"]) for r in tables["sources"]}
    actors = {(r["case_id"], r["actor_id"]) for r in tables["actors"]}
    for table, rows in tables.items():
        for r in rows:
            if r["case_id"] not in cases:
                raise ValueError(f"{table}.csv:{r['_line']} 引用了不存在的 case_id")
            if "source_id" in r and (r["case_id"], r["source_id"]) not in source_ids:
                raise ValueError(f"{table}.csv:{r['_line']} 缺少可追溯的 source_id")
            if table in {"case_labels","person_labels"} and (r["case_id"], r["label_source"]) not in source_ids:
                raise ValueError("标签缺少来源记录")
            fields = {"person_labels":["actor_id"],"network_events":["actor_id"],"identity_links": ["actor_id"], "relations": ["from_actor", "to_actor"], "communications": ["from_actor", "to_actor"], "shipments": ["sender_id", "receiver_id", "carrier_id"]}.get(table, [])
            for key in fields:
                if r[key] and (r["case_id"], r[key]) not in actors:
                    raise ValueError(f"{table}.csv:{r['_line']} 主体不存在：{r[key]}")
            required = {"person_labels":["actor_id","role_label","level_label"],"shipments": ["order_id", "from_place", "to_place"], "communications": ["from_actor", "to_actor", "channel"], "relations": ["from_actor", "to_actor", "relation"], "identity_links": ["actor_id", "entity_id", "chain"]}.get(table, [])
            if any(not r[k] for k in required):
                raise ValueError(f"{table}.csv:{r['_line']} 缺少必要关联字段")
    for source in tables["sources"]:
        reference=source["reference"]
        if not source['title'] or not reference or (reference.startswith('ref:') and not reference[4:].strip()):raise ValueError(f"sources.csv:{source['_line']} 来源标题和材料引用不能为空；外部引用须在ref:后填写具体依据")
        if reference.startswith("ref:"):
            continue
        if Path(reference).is_absolute() or Path(reference).drive:raise ValueError('来源文件必须使用数据集内的相对路径')
        candidate=(root/reference).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("来源文件路径越出了数据集目录")
        if candidate.is_file():
            proof_digest=hashlib.sha256()
            with candidate.open("rb") as proof:
                for chunk in iter(lambda:proof.read(1024*1024),b""):
                    proof_digest.update(chunk)
            source["_sha256"]=proof_digest.hexdigest()
            digest.update(proof_digest.digest())
        else:
            warnings.append(f"来源 {source['case_id']}/{source['source_id']} 尚未附带本地文件")
    intake_provenance=None
    provenance_path=root/"cleaning_report.json"
    if manifest.get('cleaning')=='cleaning_report.json' and not provenance_path.is_file():raise ValueError('清洗溯源文件 cleaning_report.json 缺失，请恢复后导入')
    if manifest.get("cleaning")=="cleaning_report.json" and provenance_path.is_file():
        provenance=json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance.get('valid') is not True:raise ValueError('清洗结果尚未通过完整校验')
        if provenance.get('parent_intake_sha256'):
            parent=root/'lineage/intake_report.json'
            if not parent.is_file() or hashlib.sha256(parent.read_bytes()).hexdigest()!=provenance['parent_intake_sha256']:raise ValueError('上游接入溯源报告缺失或变化')
            intake_provenance=json.loads(parent.read_text(encoding='utf-8'));digest.update(parent.read_bytes())
        digest.update(provenance_path.read_bytes())
        for table,rows in tables.items():
            current_path=root/(table+'.csv')
            matches=bool(current_path.is_file() and provenance.get('output_sha256',{}).get(table+'.csv')==hashlib.sha256(current_path.read_bytes()).hexdigest())
            if rows and not matches:warnings.append(f'{table}.csv 与清洗校验记录不匹配或缺少输出校验，原始行号不再视为可验证')
            for row in rows:
                original=provenance.get("origins",{}).get(table,{}).get(row[IDS[table]]+"|"+row["case_id"],{})
                row["_original_line"]=original.get("line") if matches else None
                row["_original_sha256"]=original.get("sha256","") if matches else ''
                row['_original_sheet']=original.get('sheet','') if matches else ''
                row['_lineage_verified']=matches and isinstance(row['_original_line'],int) and row['_original_line']>=1 and bool(re.fullmatch(r'[0-9a-f]{64}',row['_original_sha256']))
    if manifest.get('intake'):
        intake_path=(root/manifest['intake']).resolve()
        if not intake_path.is_relative_to(root) or not intake_path.is_file():raise ValueError('表格接入溯源报告缺失或路径越界')
        intake=json.loads(intake_path.read_text(encoding='utf-8'))
        if intake.get('valid') is not True:raise ValueError('表格接入草稿有未解决问题，请查看intake_report.json')
        intake_provenance=intake
        digest.update(intake_path.read_bytes())
        for table,rows in tables.items():
            if (root/(table+'.csv')).is_file() and intake.get('output_sha256',{}).get(table+'.csv')!=hashlib.sha256((root/(table+'.csv')).read_bytes()).hexdigest():raise ValueError('接入后的规范表变化，请重新接入或清洗并保留来源')
            for row in rows:
                original=intake.get('origins',{}).get(table,{}).get(row[IDS[table]]+'|'+row['case_id'])
                if original:row.update(_original_line=original['line'],_original_sha256=original['sha256'],_original_sheet=original.get('sheet',''),_lineage_verified=True)
                elif table not in {'cases','sources'}:row.update(_original_line=None,_original_sha256='',_lineage_verified=False)
    if intake_provenance is not None:
        declared={entry['sha256'] for entry in intake_provenance.get('inputs',[])}
        declared_ids={'IMP_'+hashlib.sha256((entry['sha256']+entry.get('sheet','')).encode()).hexdigest()[:12] for entry in intake_provenance.get('inputs',[])}
        source_digests={(r['case_id'],r['source_id']):r.get('_sha256') for r in tables['sources']}
        if not declared or any(value not in declared for key,value in source_digests.items() if key[1] in declared_ids):raise ValueError('接入原件缺失或指纹变化，不能沿用原始来源行号')
        for table,rows in tables.items():
            if table in {'cases','sources'}:continue
            for row in rows:
                source=row.get('source_id') or row.get('label_source')
                inherited=row[IDS[table]]+'|'+row['case_id'] in intake_provenance.get('origins',{}).get(table,{})
                if inherited and row.get('_lineage_verified') and (not isinstance(row.get('_original_line'),int) or row['_original_line']<1 or row.get('_original_sha256')!=source_digests.get((row['case_id'],source))):raise ValueError('接入原件与逐行溯源不一致，请重新核对')
    if manifest.get('evaluation_groups'):
        from .validation_groups import read_group_rows
        grouped=read_group_rows(root,manifest,tables)
        digest.update(grouped['raw'])
    if manifest.get('configuration'):
        path=(root/manifest['configuration']).resolve()
        if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=manifest.get('configuration_sha256'):raise ValueError('任务配置留痕文件缺失、越界或指纹不匹配')
        digest.update(path.read_bytes())
    return Dataset(root, manifest, tables, digest.hexdigest(), warnings)


def entity_id(kind, value, chain=""):
    value=quote(str(value),safe='-_.~')
    return f"{kind}:{quote(str(chain),safe='-_.~')}:{value}" if kind in {"wallet", "account", "ip"} else f"{kind}:{value}"


def build_case_graph(dataset: Dataset, case_id: str, as_of=None, flows=None, include_unverified=False):
    if case_id not in {r["case_id"] for r in dataset.tables["cases"]}:
        raise ValueError("案件编号不存在")
    cutoff = parse_time(as_of) if as_of else None
    flows = set(FLOWS) if flows is None else set(flows)
    if not flows.issubset(FLOWS):raise ValueError('未知的四流筛选项')
    graph = nx.MultiDiGraph(case_id=case_id, origin=dataset.manifest["origin"], dataset_fingerprint=dataset.fingerprint,as_of=cutoff.isoformat() if cutoff else "")
    actors = {r["actor_id"]: r for r in dataset.rows("actors", case_id)}

    def node(kind, value, chain=""):
        uid = entity_id(kind, value, chain)
        info = actors.get(value, {}) if kind == "actor" else {}
        graph.add_node(uid, kind=kind, entity_id=value, label=info.get("alias") or value, chain=chain, role=info.get("role", ""), role_verified=is_true(info.get("role_verified", "false")), actor_type=info.get("actor_type", ""))
        return uid

    def edge(u, v, r, flow, relation, suffix="", **attrs):
        if cutoff and r["_time"] > cutoff:
            return
        if flow not in flows:
            return
        if r['_table']=='shipments':
            attrs.setdefault('goods_label',r['goods_label']);attrs.setdefault('service_type',r['service_type'])
        graph.add_edge(u, v, key=f"{r['_table']}:{r[IDS[r['_table']]]}:{suffix}", flow=flow, relation=relation, timestamp=r["_time"].isoformat(), source_id=r["source_id"], record_id=r[IDS[r["_table"]]], table=r["_table"], line=r["_line"], original_line=r.get("_original_line",r["_line"]), original_sha256=r.get("_original_sha256",""),original_sheet=r.get('_original_sheet',''), lineage_verified=r.get('_lineage_verified',True), **attrs)

    for table, flow in [("funds", "funds"), ("shipments", "goods"), ("communications", "information"), ("network_events","information"), ("identity_links", "people"), ("relations", "people")]:
        if flow not in flows:
            continue
        for r in dataset.rows(table, case_id):
            if cutoff and r["_time"] > cutoff:
                continue
            if table in {"identity_links", "relations"} and not is_true(r["verified"]) and not include_unverified:
                continue
            if table == "funds":
                u, v = node(r["from_kind"], r["from_id"], r["chain"]), node(r["to_kind"], r["to_id"], r["chain"])
                edge(u, v, r, flow, "transfer", amount=r["_amount"], currency=r["currency"], exchange_id=r["exchange_id"], verified="recorded")
                if r["order_id"]:
                    edge(u, node("order", r["order_id"]), r, flow, "payment", "payment", amount=r["_amount"], currency=r["currency"], verified="recorded")
                if r["exchange_id"]:
                    event=node("exchange",r["exchange_id"])
                    edge(u,event,r,flow,"exchange_in","exchange_in",currency=r["currency"],verified="recorded")
                    edge(event,v,r,flow,"exchange_out","exchange_out",currency=r["currency"],verified="recorded")
            elif table == "shipments":
                order = node("order", r["order_id"])
                edge(node("place", r["from_place"]), order, r, flow, "dispatch", "dispatch", verified="recorded",goods_label=r["goods_label"],service_type=r["service_type"])
                edge(order, node("place", r["to_place"]), r, flow, "deliver", "deliver", verified="recorded")
                for field, rel in [("sender_id", "sender"), ("receiver_id", "receiver"), ("carrier_id", "carrier")]:
                    if r[field]:
                        edge(node("actor", r[field]), order, r, flow, rel, field, verified="recorded")
                if r["vehicle_id"]:
                    edge(node("vehicle", r["vehicle_id"]), order, r, flow, "transport", "vehicle", verified="recorded")
            elif table == "network_events":
                ip=node("ip",r["ip"],r["_scope"])
                attrs={"verified":"recorded","event_type":r["event_type"],"source_port":r["source_port"],"network_scope":r["_scope"]}
                if r["actor_id"]:edge(node("actor",r["actor_id"]),ip,r,flow,"observed_ip","actor",**attrs)
                if r["account_id"]:edge(node(r["account_kind"],r["account_id"],r["chain"]),ip,r,flow,"account_access","account",**attrs)
                if r["device_id"]:edge(node("device",r["device_id"]),ip,r,flow,"device_network","device",**attrs)
            elif table == "communications":
                u, v = node("actor", r["from_actor"]), node("actor", r["to_actor"])
                edge(u, v, r, flow, "communicate", channel=r["channel"], summary=r["summary"], verified="recorded")
                if r["device_id"]:
                    edge(u, node("device", r["device_id"]), r, flow, "uses_device", "device", verified="recorded")
            elif table == "identity_links":
                edge(node("actor", r["actor_id"]), node(r["entity_kind"], r["entity_id"], r["chain"]), r, flow, "identity", verified="verified" if is_true(r["verified"]) else "pending", verification_note=r["verification_note"])
            else:
                edge(node("actor", r["from_actor"]), node("actor", r["to_actor"]), r, flow, "actor_relation", relation_text=r["relation"], verified="verified" if is_true(r["verified"]) else "pending")
    return graph


def temporal_paths(graph, start, target, max_hops=6, limit=3, currency=None, chronological=True):
    if start not in graph or target not in graph:
        raise ValueError("起点或终点不在当前图谱中")
    if not 1 <= max_hops <= 12:
        raise ValueError("路径深度应为 1—12")
    if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=20:raise ValueError('路径数量须为1至20')
    queue, paths, expanded = deque([(start, [start], [], None)]), [], 0
    while queue and len(paths) < limit:
        current, nodes, edges, last_time = queue.popleft()
        expanded += 1
        if expanded > 50000:
            break
        if current == target:
            paths.append({"nodes": nodes, "edges": edges})
            continue
        if len(edges) >= max_hops:
            continue
        for u, v, key, data in sorted(graph.out_edges(current, keys=True, data=True), key=lambda e: e[3]["timestamp"]):
            if v in nodes or data.get("verified") == "pending":
                continue
            if currency and (data["relation"] != "transfer" or data.get("currency") != currency):
                continue
            when = parse_time(data["timestamp"])
            if chronological and last_time and when < last_time:
                continue
            queue.append((v, nodes + [v], edges + [(u, v, key)], when))
    return {"paths": paths, "truncated": bool(queue), "expanded": expanded, "chronological": chronological}


def safe_json(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, Path)):
        return str(value)
    raise TypeError(type(value).__name__)
