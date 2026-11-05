"""扩展演示数据：角色层级监督标签、已知因果生成机制、原始媒体测试材料。"""
import csv
import json
import random
import re
from pathlib import Path
from datetime import timedelta

import numpy as np

from .core import SCHEMA,parse_time,TZ
from .synthetic import create_dataset

ROLE_TASK={'task_id':'synthetic_person_roles_v1','roles':['协调','资金处理','技术支持','运输执行'],'levels':['上游','中间层','执行层'],'compatible_pairs':[['协调','上游'],['资金处理','中间层'],['技术支持','中间层'],['运输执行','执行层']]}


def create_extended_dataset(root,count=60,seed=107,namespace=None,varied=False):
    root=Path(root)
    if root.exists() and any(root.iterdir()):raise ValueError('合成生成器仅写入新的空目录，避免覆盖已有数据')
    if namespace is not None and not re.fullmatch(r'[A-Z][A-Z0-9_]{0,15}',namespace):raise ValueError('合成命名空间须为大写字母开头、最多16位字母数字下划线')
    root=create_dataset(root,count,seed);tables={}
    for table in SCHEMA:
        with (root/(table+'.csv')).open(encoding='utf-8-sig',newline='') as stream:tables[table]=list(csv.DictReader(stream))
    rng=random.Random(seed+100);role_by_actor={'A1':('协调','上游'),'A2':('资金处理','中间层'),'A3':('资金处理','中间层'),'A4':('运输执行','执行层'),'A5':('技术支持','中间层'),'A6':('运输执行','执行层')}
    for index,case in enumerate(tables['cases']):
        cid=case['case_id'];base=min(parse_time(r['timestamp']) for r in tables['funds'] if r['case_id']==cid)
        def time(minutes):return (base+timedelta(minutes=minutes)).isoformat()
        def add(table,**values):tables[table].append({'case_id':cid,'source_id':'S001',**values})
        for row in tables['actors']:
            if row['case_id']==cid:row.update(role='',role_verified='false')
        add('actors',actor_id='A5',alias='主体E',actor_type='person',role='',role_verified='false')
        add('actors',actor_id='A6',alias='主体F',actor_type='person',role='',role_verified='false')
        for j in range(5+rng.randrange(3)):
            add('network_events',access_id=f'TECH{j}',actor_id='A5',account_id='',account_kind='',chain='',ip=f'198.51.100.{10+j}',device_id='D_TECH',timestamp=time(5+j*2),event_type='合成设备维护会话',source_port=str(41000+j),network_scope='synthetic-'+cid)
        for j in range(3):
            add('communications',event_id=f'TECH_MSG{j}',from_actor='A5',to_actor='A1',timestamp=time(10+j*5),channel='即时通信',device_id='D_TECH',summary='合成记录：会话连接与设备配置已完成')
        add('communications',event_id='COORD_EXTRA',from_actor='A1',to_actor='A6',timestamp=time(12),channel='即时通信',device_id='D1',summary='合成记录：核对本次订单的收发安排')
        add('communications',event_id='FUND_MSG',from_actor='A2',to_actor='A3',timestamp=time(16),channel='即时通信',device_id='D2',summary='合成记录：核对两笔转入记录的时间与币种')
        add('shipments',shipment_id='G_EXTRA',order_id='O002',from_place='合成仓储甲',to_place='合成中转乙',sender_id='A4',receiver_id='A6',carrier_id='A6',vehicle_id='V_EXTRA',timestamp=time(90),goods_label='合成通信设备批次2',service_type='telecom_equipment')
        add('shipments',shipment_id='G_EXTRA2',order_id='O002',from_place='合成中转乙',to_place='合成收货丙',sender_id='A6',receiver_id='A4',carrier_id='A6',vehicle_id='V_EXTRA',timestamp=time(130),goods_label='合成通信设备批次2',service_type='telecom_equipment')
        if varied:
            # 不改变资金标签生成机制；增加与标签无关的活动，弱化角色与固定文本/次数的捷径。
            for row in tables['communications']:
                if row['case_id']==cid:row['summary']=rng.choice(['合成记录：请核对本次安排','合成记录：已收到，请复核','合成记录：等待核对来源'])
            tables['network_events']=[r for r in tables['network_events'] if r['case_id']!=cid or not r['access_id'].startswith('TECH') or r['access_id']=='TECH0' or rng.random()>.35]
            for actor in role_by_actor:
                for j in range(rng.randrange(4)):
                    peer=rng.choice([v for v in role_by_actor if v!=actor])
                    add('communications',event_id=f'BG_{actor}_{j}',from_actor=actor,to_actor=peer,timestamp=time(rng.randrange(2,100)),channel='即时通信',device_id='D_BG_'+actor,summary='合成背景联系：内容不说明资金用途或控制关系')
                for j in range(rng.randrange(3)):
                    add('network_events',access_id=f'BGIP_{actor}_{j}',actor_id=actor,account_id='',account_kind='',chain='',ip=f'203.0.113.{20+int(actor[1:])*3+j}',device_id='D_BG_'+actor,timestamp=time(rng.randrange(1,100)),event_type='合成一般网络观测',source_port=str(45000+j),network_scope='synthetic-'+cid)
            add('actors',actor_id='BYSTANDER',alias='合成未标注背景主体',actor_type='person',role='',role_verified='false')
            add('network_events',access_id='UNKNOWN_ONCE',actor_id='BYSTANDER',account_id='',account_kind='',chain='',ip='203.0.113.249',device_id='D_UNKNOWN',timestamp=time(20),event_type='合成单次观测，角色未知',source_port='',network_scope='synthetic-'+cid)
        shuffled=list(role_by_actor);rng.shuffle(shuffled);mapping=dict(zip(role_by_actor,shuffled))
        actor_fields={'actors':['actor_id'],'shipments':['sender_id','receiver_id','carrier_id'],'communications':['from_actor','to_actor'],'network_events':['actor_id'],'identity_links':['actor_id'],'relations':['from_actor','to_actor']}
        for table,fields in actor_fields.items():
            for row in tables[table]:
                if row['case_id']==cid:
                    for field in fields:
                        if row.get(field) in mapping:row[field]=mapping[row[field]]
        for actor,(role,level) in role_by_actor.items():
            tables['person_labels'].append({'case_id':cid,'actor_id':mapping[actor],'role_label':role,'level_label':level,'confirmed':'true','label_source':'S001','timestamp':time(1840)})
        case['title']='合成角色与资金联合样本 '+cid
        (root/'evidence'/f'{cid}.txt').write_text((root/'evidence'/f'{cid}.txt').read_text(encoding='utf-8')+'\n角色、层级由生成器预设；主体编号每案置换，不代表真实成员或控制关系。\n',encoding='utf-8')
    if namespace:
        for case in tables['cases']:
            old=case['case_id'];new=namespace+'_'+old
            for rows in tables.values():
                for row in rows:
                    if row.get('case_id')!=old:continue
                    row['case_id']=new
                    for field in ('from_id','to_id','account_id','entity_id'):
                        if str(row.get(field,'')).startswith(old+'-'):row[field]=new+row[field][len(old):]
                    if row.get('network_scope')=='synthetic-'+old:row['network_scope']='synthetic-'+new
                    if row.get('reference')==f'evidence/{old}.txt':row['reference']=f'evidence/{new}.txt'
            old_file=root/'evidence'/f'{old}.txt';new_file=root/'evidence'/f'{new}.txt';old_file.rename(new_file)
            new_file.write_text(new_file.read_text(encoding='utf-8').replace(old,new),encoding='utf-8')
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'));manifest['role_task']=ROLE_TASK;manifest['title']='四流角色层级合成训练样本';manifest['generator']={'seed':seed,'varied':varied,'namespace':namespace,'interpretation':'随机生成机制与业务标签均为合成设定；未标注背景主体不作为负类'}
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    for table,fields in SCHEMA.items():
        with (root/(table+'.csv')).open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(tables[table])
    return root


def create_causal_demo(root,count=800,seed=319):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    rng=np.random.default_rng(seed);x=rng.normal(size=(count,2));probability=1/(1+np.exp(-(.7*x[:,0]+.5*x[:,1])))
    treatment=rng.binomial(1,probability);outcome=1.5*treatment+1.2*x[:,0]+.8*x[:,1]+rng.normal(0,.5,count)
    fields=['observation_id','group_id','covariate_time','treatment_time','outcome_time','source_id','prior_activity','network_context','fund_signal','industry_measure']
    with (root/'observations.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for i in range(count):writer.writerow(dict(zip(fields,[f'OBS{i:04d}',f'GROUP{i:04d}','2026-01-01T08:00:00+08:00','2026-01-01T09:00:00+08:00','2026-01-02T09:00:00+08:00','SIM_SOURCE',x[i,0],x[i,1],int(treatment[i]),outcome[i]])))
    with (root/'sources.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['source_id','title','reference']);writer.writeheader();writer.writerow({'source_id':'SIM_SOURCE','title':'已知结构方程合成机制','reference':'generation.txt'})
    (root/'generation.txt').write_text('全部观测为合成数据。T~Bernoulli(sigmoid(0.7X1+0.5X2));Y=1.5T+1.2X1+0.8X2+正态噪声。真实总体ATE=1.5；非真实涉案金额、违法概率或侦查措施效果。',encoding='utf-8')
    design={'schema_version':1,'origin':'synthetic','task_id':'synthetic_known_scm_v1','question':'在明确的合成结构方程中，二元资金信号对后续产业指标的总体平均效应','treatment':'fund_signal','outcome':'industry_measure','adjustment':['prior_activity','network_context'],'dag':{'nodes':['prior_activity','network_context','fund_signal','industry_measure'],'edges':[['prior_activity','fund_signal'],['network_context','fund_signal'],['prior_activity','industry_measure'],['network_context','industry_measure'],['fund_signal','industry_measure']]},'unobserved_nodes':[],'assumptions':{'consistency':True,'no_unmeasured_confounding':True,'positivity':True,'pre_treatment_covariates':True,'independent_groups':True},'acknowledged_by':'合成生成器：仅验证已声明机制','known_ate_for_validation':1.5,'description':'此处总体效应不等于任何人的涉诈因果结论；真实研究必须由分析人员确认变量、因果图和识别假设。'}
    (root/'causal_design.json').write_text(json.dumps(design,ensure_ascii=False,indent=2),encoding='utf-8')
    return root


def create_media_demo(root):
    from PIL import Image,ImageDraw,ImageFont
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    font_path=Path('C:/Windows/Fonts/msyh.ttc')
    font=ImageFont.truetype(str(font_path),36)
    image=Image.new('RGB',(1600,900),'white');draw=ImageDraw.Draw(image)
    lines=['合成多模态测试材料  非真实案件','时间：2026-01-01 08:00:00','IP: 192.0.2.10','SIM001  W001 -> W002  100.00 USDT','通信摘要：请核对订单 O001 的收发时间。','识别结果均为待人工核验内容。']
    for i,line in enumerate(lines):draw.text((65,70+i*115),line,font=font,fill='black')
    image.save(root/'synthetic_record.png');image.save(root/'synthetic_scan.pdf','PDF')
    (root/'synthetic_record.txt').write_text('\n'.join(lines),encoding='utf-8')
    import cv2
    frame=cv2.cvtColor(np.array(image.resize((1280,720))),cv2.COLOR_RGB2BGR)
    writer=cv2.VideoWriter(str(root/'synthetic_silent_video.mp4'),cv2.VideoWriter_fourcc(*'mp4v'),2,(1280,720))
    if not writer.isOpened():raise ValueError('无法创建视频验证样本')
    for _ in range(8):writer.write(frame)
    writer.release()
    (root/'manifest.json').write_text(json.dumps({'origin':'synthetic','description':'图像、扫描PDF、无声视频和文本均由程序生成；同目录合成语音由本机语音合成产生，非真实证据。'},ensure_ascii=False,indent=2),encoding='utf-8')
    return root
