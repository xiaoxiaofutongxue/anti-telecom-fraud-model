"""Create an explicitly synthetic CSV demonstrating exact order joins and counterexamples."""
from pathlib import Path
import csv,json,sys

ROOT=Path(__file__).resolve().parents[1]


def create_demo(destination):
    destination=Path(destination)
    if destination.exists() and any(destination.iterdir()):raise ValueError('演示输出须使用新的空目录')
    destination.mkdir(parents=True,exist_ok=True)
    rows=[
        dict(from_account='DEMO-AC1',to_account='DEMO-AC2',amount='500.50',currency='USD',chain='合成银行域',order_id='DEMO-O1',actor_id='DEMO-A1',timestamp='2026-02-01T08:00:00+08:00'),
        dict(actor_id='DEMO-A1',ip='8.8.8.8',device_id='DEMO-D1',account_id='DEMO-AC1',chain='合成银行域',timestamp='2026-02-01T08:05:00+08:00'),
        dict(actor_id='DEMO-A2',ip='8.8.8.8',device_id='DEMO-D2',timestamp='2026-02-01T08:07:00+08:00'),
        dict(actor_id='DEMO-A1',person_name='合成人员甲',职责='合成收货活动记录',timestamp='2026-02-01T08:10:00+08:00'),
        dict(from_actor='DEMO-A1',to_actor='DEMO-SUPPLIER',summary='合成订单DEMO-O1交付沟通',timestamp='2026-02-01T08:12:00+08:00'),
        dict(order_id='DEMO-O1',goods_label='合成通信设备',receiver_id='DEMO-A1',shipment_id='DEMO-SH1',timestamp='2026-02-01T08:30:00+08:00'),
        dict(from_account='DEMO-AC9',to_account='DEMO-AC8',amount='500.50',currency='USD',chain='合成银行域',order_id='DEMO-O10',actor_id='DEMO-A9',timestamp='2026-02-01T08:01:00+08:00'),
        dict(actor_id='DEMO-A1',ip='2606:4700:4700::1111'),
        dict(actor_id='DEMO-A1',ip='999.1.1.1',timestamp='2026-02-01T08:06:00+08:00'),
    ]
    for index,row in enumerate(rows,1):row.update(record_id='DEMO-'+str(index),source_citation='合成演示¶'+str(index),说明='所有活动均为合成，IP仅作格式示例，未访问这些地址')
    headers=list(dict.fromkeys(k for row in rows for k in row))
    with (destination/'四流关联演示.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=headers);writer.writeheader();writer.writerows(rows)
    (destination/'manifest.json').write_text(json.dumps({'origin':'synthetic','title':'四流关联反例演示','description':'合成活动；同订单和共同标识时窗关联。O10同金额不连接到O1，共用IP不自动并人。'},ensure_ascii=False,indent=2),encoding='utf-8')
    return destination


if __name__=='__main__':print(create_demo(Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'data/narrative_demo_synthetic'))
