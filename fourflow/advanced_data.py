"""双图研究用合成监督与原始媒体。标签是显式生成设定，绝非真实核实。"""
from pathlib import Path
from datetime import timedelta
import csv,hashlib,json,random,wave
import numpy as np
from .core import SCHEMA,load_dataset,parse_time
from .extended_data import create_extended_dataset

CONTROL_FIELDS=['case_id','from_actor','to_actor','label','confirmed','label_source','timestamp']


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def create_advanced_dataset(root,count=60,seed=414,namespace='ADV14',region=None,case_type=None,shift=0):
    """媒体内容仅来自观测摘要和随机背景，绝不写入角色/资金/控制标签。"""
    from PIL import Image,ImageDraw
    import cv2
    root=create_extended_dataset(root,count,seed,namespace=namespace,varied=True)
    data=load_dataset(root);rng=random.Random(seed+711)
    tables={name:[{k:r.get(k,'') for k in fields} for r in data.tables[name]] for name,fields in SCHEMA.items()}
    control=[];media=[];(root/'raw_media').mkdir()
    for ci,case in enumerate(tables['cases']):
        cid=case['case_id'];case['region']=region or ['合成区域甲','合成区域乙'][ci%2]
        case['case_type']=case_type or ['合成场景甲','合成场景乙'][(ci//2)%2]
        labels=[r for r in tables['person_labels'] if r['case_id']==cid]
        actors=[r['actor_id'] for r in labels];coordinator=next(r['actor_id'] for r in labels if r['role_label']=='协调')
        middle=[r['actor_id'] for r in labels if r['level_label']=='中间层']
        bottom=[r['actor_id'] for r in labels if r['level_label']=='执行层']
        rng.shuffle(middle);rng.shuffle(bottom)
        positives={(coordinator,a) for a in middle}|{(middle[0],a) for a in bottom}
        base=min(parse_time(r['timestamp']) for r in tables['funds'] if r['case_id']==cid)
        # 可观测的定向往来只是输入；明确控制标签单独保存，不进入图。
        for j,(a,b) in enumerate(sorted(positives)):
            for k in range(2+rng.randrange(2)):
                tables['communications'].append({'case_id':cid,'event_id':f'J{j}_{k}','from_actor':a,'to_actor':b,'timestamp':(base+timedelta(minutes=35+j*8+k)).isoformat(),'channel':'合成协作通信','device_id':'D_'+a,'summary':'合成沟通：安排已发送，待核对来源','source_id':'S001'})
        label_time=(base+timedelta(minutes=1860)).isoformat()
        for a in actors:
            for b in actors:
                if a!=b:control.append(dict(zip(CONTROL_FIELDS,[cid,a,b,str(int((a,b) in positives)),'true','S001',label_time])))
        # 每案三个独立原始文件，锚定同一条已有通信。所有区域都有三个模态。
        event=next(r for r in tables['communications'] if r['case_id']==cid)
        observed=[r for r in tables['funds'] if r['case_id']==cid]
        minutes=[(parse_time(r['timestamp'])-base).total_seconds()/60 for r in observed[:10]]
        pixels=Image.new('RGB',(96,64),(225+int(shift)*3,230,235));draw=ImageDraw.Draw(pixels)
        for j,t in enumerate(minutes):
            h=int(5+45*min(1,t/1700));draw.rectangle((4+j*9,58-h,9+j*9,58),fill=(40+shift*5,80+rng.randrange(80),160))
        draw.line((2,10,93,10),fill=(40,40,40),width=1+shift%2)
        image=root/'raw_media'/f'{cid}.png';pixels.save(image)
        length=4096;rate=8000;t=np.arange(length)/rate
        frequency=160+min(500,float(np.median(minutes)))+shift*15
        waveform=.4*np.sin(2*np.pi*frequency*t)+.08*np.random.default_rng(seed+ci).normal(size=length)
        audio=root/'raw_media'/f'{cid}.wav'
        with wave.open(str(audio),'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(rate);f.writeframes((np.clip(waveform,-1,1)*32767).astype('<i2').tobytes())
        video=root/'raw_media'/f'{cid}.mp4';writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'mp4v'),4,(96,64))
        if not writer.isOpened():raise ValueError('合成视频编码器不可用')
        frame=cv2.cvtColor(np.asarray(pixels),cv2.COLOR_RGB2BGR)
        for j in range(8):writer.write(np.roll(frame,j*(shift+1),axis=1))
        writer.release()
        for j,path in enumerate((image,audio,video)):
            sid='RAW'+str(j);reference=path.relative_to(root).as_posix()
            tables['sources'].append({'case_id':cid,'source_id':sid,'title':'合成原始媒体与通信的已设定对应','reference':reference})
            media.append({'case_id':cid,'table':'communications','record_id':event['event_id'],'source_id':sid,'path':reference,'sha256':sha(path),'confirmed':True,'reviewer':'合成生成器，非真实核验','available_at':event['timestamp']})
    for name,fields in SCHEMA.items():
        with (root/(name+'.csv')).open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(tables[name])
    with (root/'control_labels.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=CONTROL_FIELDS);writer.writeheader();writer.writerows(control)
    (root/'multimodal_links.json').write_text(json.dumps(media,ensure_ascii=False,indent=2),encoding='utf-8')
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    manifest['advanced']={'version':1,'control_task':{'task_id':'synthetic_directed_control_v1','positive':'生成器设定的定向控制联系','negative':'明确标注的非该定向联系，未知不能当负类'},'media_file':'multimodal_links.json','control_file':'control_labels.csv','sha256':{n:sha(root/n) for n in ('control_labels.csv','multimodal_links.json')}}
    manifest['generator']['raw_media']='观测时间绘图、观测时间调制音、平移视频；无人物身份和真实视听语义'
    manifest['generator']['domain_shift']=shift
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return root
