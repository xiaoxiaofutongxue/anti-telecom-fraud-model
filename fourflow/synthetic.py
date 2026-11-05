"""可重复生成的合成数据；不用于证明真实案件识别能力。"""
import csv
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from .core import SCHEMA, TZ


def create_dataset(root, count=3, seed=42):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "evidence").mkdir(exist_ok=True)
    rng = random.Random(seed)
    tables = {table: [] for table in SCHEMA}
    manifest = {"schema_version": 2, "origin": "synthetic", "title": "反电诈四流合成验证数据", "timezone": "Asia/Shanghai", "description": "全部主体、交易、通信、IP、运输和标签由程序生成；confirmed 仅表示合成设定已标注，不代表真实核实。", "generator_seed": seed}
    manifest['label_definition']={'task_id':'synthetic_fund_pattern_v1','positive':'合成短时拆分、归集及回流组合模式','negative':'生成器设定的非该组合网络；不表示已认定合法'}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for i in range(count):
        case = f"C{i+1:03d}"
        anomalous = i % 2 == 0
        base = datetime(2026, 1, 1, 8, tzinfo=TZ) + timedelta(days=i * 3)
        time = lambda minutes: (base + timedelta(minutes=minutes)).isoformat()
        source = "S001"
        def add(table, **values):
            tables[table].append({"case_id": case, "source_id": source, **values})
        add("cases", title=f"反电诈关联记录 {i+1:03d}", region=f"区域{chr(65+i%3)}", case_type="反电诈资金与产业关联")
        add("sources", source_id=source, title="合成记录说明", reference=f"evidence/{case}.txt")
        for j in range(4):
            add("actors", actor_id=f"A{j+1}", alias=f"主体{chr(65+j)}", actor_type="organization" if j==3 else "person", role="记录中的协调角色" if j==0 else "", role_verified="true" if j==0 else "false")
        wallets = [f"{case}-W{j:02d}" for j in range(8)]
        if anomalous:
            routes = [(0,1,1000,0),(1,2,200,3),(1,3,210,4),(1,4,190,5),(1,5,180,6),(2,6,180,9),(3,6,190,10),(4,6,170,11),(6,1,450,13),(5,7,160,14)]
        else:
            routes = [(0,1,1000,0),(1,2,200,120),(1,3,210,300),(2,4,190,450),(3,5,180,700),(4,6,180,900),(5,6,190,1100),(2,5,170,1300),(6,7,450,1500),(3,7,160,1700)]
        for j,(u,v,amount,minute) in enumerate(routes):
            if not anomalous and i%4==1:
                minute=j*3
            minute=int(minute*rng.uniform(.8,1.3)+rng.uniform(0,2))
            add("funds", tx_id=f"T{j+1:03d}", from_id=wallets[u], to_id=wallets[v], from_kind="wallet", to_kind="wallet", chain="TRON", timestamp=time(minute), amount=f"{amount*rng.uniform(.92,1.08):.2f}", currency="USDT", order_id="O001" if j==0 else "", exchange_id="")
        for j in range(rng.randint(0,3)):
            u,v=sorted(rng.sample(range(8),2))
            add("funds",tx_id=f"N{j+1}",from_id=wallets[u],to_id=wallets[v],from_kind="wallet",to_kind="wallet",chain="TRON",timestamp=time(rng.randint(20,1700)),amount=f"{rng.uniform(5,80):.2f}",currency="USDT",order_id="",exchange_id="")
        # 法币兑换环节分别记录两种币的实际腿，exchange_id 连接事件，不混加金额。
        add("funds", tx_id="FX001", from_id=wallets[7], to_id="兑换服务地址", from_kind="wallet", to_kind="wallet", chain="TRON", timestamp=time(1800), amount="100.00", currency="USDT", order_id="", exchange_id="EX001")
        add("funds", tx_id="FX002", from_id="服务账户", to_id="收款账户", from_kind="account", to_kind="account", chain="银行域-合成", timestamp=time(1810), amount="700.00", currency="CNY", order_id="", exchange_id="EX001")
        for j in range(3):
            add("identity_links", link_id=f"I{j+1}", actor_id=f"A{j+1}", entity_kind="wallet", entity_id=wallets[j], chain="TRON", timestamp=time(-60), verified="true", verification_note="合成来源中的已标注对应关系")
        add("identity_links", link_id="I4", actor_id="A4", entity_kind="wallet", entity_id=wallets[6], chain="TRON", timestamp=time(-50), verified="false", verification_note="候选关联，尚未核实")
        add("shipments", shipment_id="G001", order_id="O001", from_place="起运地甲", to_place="收货地乙", sender_id="A1", receiver_id="A2", carrier_id="A4", vehicle_id="V001", timestamp=time(30), goods_label="物品批次-合成")
        add("shipments", shipment_id="G002", order_id="O001", from_place="收货地乙", to_place="中转地丙", sender_id="A2", receiver_id="A3", carrier_id="A4", vehicle_id="V002", timestamp=time(60), goods_label="物品批次-合成")
        for shipment in tables['shipments'][-2:]:
            shipment['service_type']='telecom_equipment';shipment['goods_label']='合成通信设备批次（非违法认定）'
        for j in range(6):
            # 两类均存在共享IP和切换，不将这些现象直接等同于目标标签。
            actor=f'A{j%3+1}';minute=j*4 if (anomalous or i%4==1) else j*90
            ip=f'192.0.2.{1 if j<3 else j-1}'
            add('network_events',access_id=f'NET{j+1:03d}',actor_id=actor,account_id=wallets[j%3],account_kind='wallet',chain='TRON',ip=ip,device_id=f'D{j%3+1}',timestamp=time(minute),event_type='合成访问观测',source_port=str(30000+j),network_scope='synthetic-'+case)
        for j,ip in enumerate(['2001:db8::1','2001:db8::2','2001:db8::3']):
            add('network_events',access_id=f'NETV{j+1}',actor_id='A1',account_id=wallets[0],account_kind='wallet',chain='TRON',ip=ip,device_id='D1',timestamp=time(8+j*3),event_type='合成IPv6访问',source_port='',network_scope='synthetic-'+case)
        for j,(a,b) in enumerate([(1,2),(1,3),(2,3),(1,4)]):
            add("communications", event_id=f"M{j+1}", from_actor=f"A{a}", to_actor=f"A{b}", timestamp=time(j*4+1), channel="即时通信", device_id=f"D{a}", summary="合成事件摘要，仅验证关联检索")
        add("relations", relation_id="R1", from_actor="A1", to_actor="A2", relation="记录中的协作关系", timestamp=time(-30), verified="true")
        add("relations", relation_id="R2", from_actor="A1", to_actor="A3", relation="候选协作关系", timestamp=time(-25), verified="false")
        tables["case_labels"].append({"case_id":case, "label":int(anomalous), "confirmed":"true", "label_source":source})
        (root / "evidence" / f"{case}.txt").write_text(
            f"数据集来源：synthetic\n案件编号：{case}\n全部记录均由随机种子 {seed} 程序生成。\n"
            "本文件提供可追溯来源引用的流程验证，不包含真实证据。\n"
            f"目标模式合成标签：{int(anomalous)}。正类为短时拆分、归集及回流组合；负类为较慢的非回流网络。\n"
            "标签定义不包含法律认定。身份对应、角色及地点也均为虚构设定。\n", encoding="utf-8")
    for table, fields in SCHEMA.items():
        with (root / f"{table}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(tables[table])
    return root


def create_templates(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps({"schema_version":2,"origin":"real","title":"请填写数据集名称","timezone":"Asia/Shanghai","description":"请填写来源和脱敏方式","label_definition":{"task_id":"","positive":"","negative":""},"role_task":{"task_id":"","roles":["协调","资金处理","技术支持","运输执行"],"levels":["上游","中间层","执行层"],"compatible_pairs":[["协调","上游"],["资金处理","中间层"],["技术支持","中间层"],["运输执行","执行层"]]}}, ensure_ascii=False, indent=2), encoding="utf-8")
    for table, fields in SCHEMA.items():
        with (root / f"{table}.csv").open("w",encoding="utf-8-sig",newline="") as stream:
            csv.writer(stream).writerow(fields)
    return root
