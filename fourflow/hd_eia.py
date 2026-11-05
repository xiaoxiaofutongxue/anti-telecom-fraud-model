"""HD-EIA工程实现：异构去重、事件/实体对齐、时间及人工媒体锚点校验。

原材料没有公式定义；这里的名称指本项目明确记录的实现，不冒充外部论文复现。
"""
from pathlib import Path
import csv,hashlib,json
import numpy as np
import torch
from .core import FLOWS,entity_id,parse_time,is_true
from .model import encode_graph,graph_fingerprint
from .trajectory import behavior_graph
from .investigation import unique_records
from .roles import role_task
from .advanced_data import CONTROL_FIELDS

MEDIA_SHAPES={'image':(3,32,32),'audio':(1,4096),'video':(4,3,32,32)}
EVENT_FEATURES=10


def mask_untrained_modalities(item,trained):
    result=dict(item);allowed=set(trained)
    result['media']={k:(v if k in allowed else v[:0]) for k,v in item['media'].items()};result['media_event']={k:(v if k in allowed else v[:0]) for k,v in item['media_event'].items()}
    return result


def local_path(root,value):
    if not isinstance(value,str):raise ValueError('研究材料路径必须为相对路径字符串')
    p=Path(value)
    if p.is_absolute() or p.drive or not value:raise ValueError('研究材料必须使用数据集内相对路径')
    path=(Path(root)/p).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file():raise ValueError('研究材料不存在或路径越界')
    return path


def file_sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def auxiliary(dataset):
    config=dataset.manifest.get('advanced')
    if not isinstance(config,dict) or config.get('version')!=1:raise ValueError('此数据未提供1.4研究配置，请载入双图合成样例或填写高级模板')
    task=config.get('control_task',{})
    if any(not isinstance(task.get(k),str) or not task[k].strip() for k in ('task_id','positive','negative')):raise ValueError('控制关系须明确任务与正负标签含义')
    files={}
    for key in ('media_file','control_file'):
        path=local_path(dataset.root,config.get(key,''));digest=file_sha(path)
        if config.get('sha256',{}).get(config[key])!=digest:raise ValueError('研究辅助文件指纹变化，须重新核验并更新清单')
        files[key]=path
    media=json.loads(files['media_file'].read_text(encoding='utf-8-sig'))
    if not isinstance(media,list):raise ValueError('多模态对应须为列表')
    seen=set()
    for row in media:
        if not isinstance(row,dict) or row.get('confirmed') is not True or not str(row.get('reviewer','')).strip():raise ValueError('原始媒体进入联合编码前须有明确人工对应和核验人')
        cid=row.get('case_id');table=row.get('table');rid=row.get('record_id')
        if table not in {'funds','shipments','network_events','communications','relations','identity_links'}:raise ValueError('媒体锚点不是支持的事件表')
        from .core import IDS
        target=next((r for r in dataset.tables[table] if r['case_id']==cid and r[IDS[table]]==rid),None)
        source=dataset.source(cid,row.get('source_id'))
        path=local_path(dataset.root,row.get('path',''))
        if source is None or source['reference']!=row['path'] or source.get('_sha256')!=row.get('sha256') or file_sha(path)!=row.get('sha256'):raise ValueError('原始媒体内容或对应来源指纹不匹配')
        if target is None or parse_time(row.get('available_at',''))<target['_time']:raise ValueError('媒体对应事件不存在或可用时间早于事件')
        key=(cid,str(path))
        if key in seen:raise ValueError('同一媒体重复登记，不能虚增模态支持')
        seen.add(key)
    with files['control_file'].open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if reader.fieldnames!=CONTROL_FIELDS:raise ValueError('控制标签表头不匹配高级模板')
        labels=list(reader)
    seen=set();actors={(r['case_id'],r['actor_id']) for r in dataset.tables['actors']}
    for r in labels:
        key=(r['case_id'],r['from_actor'],r['to_actor'])
        if key in seen or r['from_actor']==r['to_actor'] or any((r['case_id'],r[k]) not in actors for k in ('from_actor','to_actor')):raise ValueError('控制标签重复、自环或主体不存在')
        if r['label'] not in {'0','1'} or not is_true(r['confirmed']) or dataset.source(r['case_id'],r['label_source']) is None:raise ValueError('控制标签必须是有来源的已确认0/1，未知不能当作负类')
        parse_time(r['timestamp']);seen.add(key)
    fingerprint=hashlib.sha256((dataset.fingerprint+''.join(file_sha(p) for p in files.values())).encode()).hexdigest()
    return {'config':config,'media':media,'controls':labels,'fingerprint':fingerprint}


def decode_raw(path):
    """固定上限的像素/波形输入；完全本地，不调用OCR或ASR代替原始编码。"""
    from PIL import Image,ImageOps
    path=Path(path)
    if path.stat().st_size>128*1024*1024:raise ValueError('联合编码单材料上限128MB，请分段')
    suffix=path.suffix.lower();detail={'path':str(path),'truncated':False}
    if suffix in {'.png','.jpg','.jpeg','.webp','.bmp'}:
        with Image.open(path) as im:
            if im.width*im.height>40_000_000:raise ValueError('图像超过4000万像素')
            array=np.asarray(ImageOps.exif_transpose(im).convert('RGB').resize((32,32)),dtype=np.float32).transpose(2,0,1)/255
        return 'image',array,detail
    import av
    if suffix in {'.wav','.mp3','.m4a','.flac','.ogg'}:
        values=[];total=0
        with av.open(str(path)) as container:
            if not container.streams.audio:raise ValueError('音频缺少音轨')
            resampler=av.AudioResampler(format='fltp',layout='mono',rate=8000)
            for frame in container.decode(audio=0):
                for converted in resampler.resample(frame):
                    a=converted.to_ndarray().ravel();values.append(a);total+=len(a)
                if total>=4096:detail['truncated']=True;break
        array=np.concatenate(values) if values else np.zeros(0,dtype=np.float32)
        if not len(array):raise ValueError('音频无可解码采样')
        detail.update(sample_rate=8000,samples_used=min(len(array),4096),scope='前0.512秒波形，仅轻量联合编码；长音轨由独立ASR模块处理')
        return 'audio',np.pad(array[:4096],(0,max(0,4096-len(array))))[None].astype(np.float32),detail
    if suffix in {'.mp4','.avi','.mkv','.mov','.webm'}:
        frames=[]
        with av.open(str(path)) as container:
            if not container.streams.video:raise ValueError('视频无画面')
            stream=container.streams.video[0];duration=float(stream.duration*stream.time_base) if stream.duration else None
            if duration is None or duration>3600:raise ValueError('视频时长未知或超过1小时，请先分段')
            for moment in np.linspace(0,max(0,duration-.05),4):
                container.seek(int(moment/stream.time_base),stream=stream,backward=True)
                frame=None
                for decoded in container.decode(stream):
                    frame=decoded
                    if decoded.time is not None and decoded.time>=moment:break
                if frame is None:raise ValueError('视频抽样解码失败')
                frames.append(np.asarray(frame.to_image().convert('RGB').resize((32,32)),dtype=np.float32).transpose(2,0,1)/255)
        detail.update(truncated=True,scope='全时段4帧采样，视频音轨须另登记音频对应，未采样画面不等于不存在')
        return 'video',np.stack(frames),detail
    raise ValueError('联合原始编码支持图片、音频及视频；PDF/DOCX继续使用独立材料识别')


def prepare(dataset,graph,aux=None,raw_cache=None):
    aux=aux or auxiliary(dataset);raw_cache=raw_cache if raw_cache is not None else {}
    if graph.graph.get('dataset_fingerprint')!=dataset.fingerprint:raise ValueError('双图输入快照与数据不一致')
    if not 0<len(graph)<=256:raise ValueError('双图研究模型支持1至256实体')
    records=unique_records(graph);trajectory=behavior_graph(graph,max_events=256)
    if not trajectory['available'] or not trajectory['nodes']:raise ValueError('双图研究需要1至256个有效事件；请拆分时间窗')
    nodes=sorted(graph);index={n:i for i,n in enumerate(nodes)};events=sorted(trajectory['nodes'],key=lambda r:(r['timestamp'],r['id']));event_index={r['id']:i for i,r in enumerate(events)}
    n,m=len(nodes),len(events);x,_=encode_graph(graph);flows=list(FLOWS);a=np.zeros((8,n,n),np.float32);incidence=np.zeros((n,m),np.float32)
    latest=parse_time(graph.graph['as_of']) if graph.graph.get('as_of') else max(parse_time(r['timestamp']) for r in events);first=min(parse_time(r['timestamp']) for r in events)
    event_x=np.zeros((m,EVENT_FEATURES),np.float32);event_text=[];support={node:set() for node in nodes};contact={}
    for i,event in enumerate(events):
        entry=records[(event['table'],event['record_id'])];d=entry['data'];anchors=event['entities'];age=max(0,(latest-parse_time(d['timestamp'])).total_seconds()/3600)
        event_x[i,flows.index(d['flow'])]=1;event_x[i,4:]=[np.log1p(len(anchors)),min(age/48,4),np.log1p(max(0,(parse_time(d['timestamp'])-first).total_seconds()/3600)),float(d.get('verified')=='confirmed'),float(d.get('lineage_verified',True)),0]
        event_text.append(str(d.get('summary',''))[:256].encode('utf-8')[:128])
        for node in anchors:incidence[index[node],i]=1;support[node].add((d['table'],d['record_id']))
        actors=[node for node in anchors if graph.nodes[node]['kind']=='actor']
        for u in actors:
            for v in actors:
                if u!=v:contact.setdefault((index[u],index[v]),[]).append({'table':d['table'],'record_id':d['record_id'],'source_id':d['source_id'],'timestamp':d['timestamp'],'csv_line':d['line']})
        for u,v,k in entry['edges']:
            weight=np.exp(-np.log(2)*age/24);f=flows.index(d['flow']);a[f,index[v],index[u]]+=weight;a[f+4,index[u],index[v]]+=weight
    for i in range(8):a[i]/=np.maximum(1,(a[i]>0).sum(axis=1,keepdims=True))
    b=np.zeros((2,m,m),np.float32)
    for edge in trajectory['edges']:
        i,j=event_index[edge['from']],event_index[edge['to']];w=np.exp(-float(edge['gap_hours'])/24);b[0,j,i]=w;b[1,i,j]=w
    b/=np.maximum(1,(b>0).sum(axis=2,keepdims=True))
    text=np.zeros((m,128),np.int64)
    for i,value in enumerate(event_text):text[i,:len(value)]=np.frombuffer(value,dtype=np.uint8).astype(np.int64)+1
    media={k:[] for k in MEDIA_SHAPES};media_event={k:[] for k in MEDIA_SHAPES};media_details=[];cid=graph.graph['case_id'];cutoff=parse_time(graph.graph['as_of']) if graph.graph.get('as_of') else None
    for row in aux['media']:
        key=str(row['table'])+':'+str(row['record_id'])
        if row['case_id']!=cid or key not in event_index or (cutoff and parse_time(row['available_at'])>cutoff):continue
        if len(media_details)>=24:raise ValueError('联合模型每案最多24份已核验原始媒体，请拆分窗口')
        path=local_path(dataset.root,row['path'])
        # 每次推理再次校验文件，不能让缓存掩盖被替换的材料。
        if file_sha(path)!=row['sha256']:raise ValueError('媒体文件在载入后变化，须重新导入')
        if row['sha256'] not in raw_cache:raw_cache[row['sha256']]=decode_raw(path)
        kind,array,detail=raw_cache[row['sha256']];media[kind].append(array);media_event[kind].append(event_index[key]);media_details.append({**row,'kind':kind,'sampling':detail})
    actor_nodes=[i for i,node in enumerate(nodes) if graph.nodes[node]['kind']=='actor'];pairs=[(i,j) for i in actor_nodes for j in actor_nodes if i!=j]
    tensor=lambda arr:torch.from_numpy(np.asarray(arr))
    directed={}
    for _,entry in records.items():
        if entry['data']['relation']=='communicate':
            for u,v,k in entry['edges']:
                if graph.nodes[u]['kind']=='actor' and graph.nodes[v]['kind']=='actor':directed[(index[u],index[v])]=directed.get((index[u],index[v]),0)+1
    pair_features=np.array([[np.log1p(directed.get((i,j),0)),np.log1p(directed.get((j,i),0)),np.log1p(len(contact.get((i,j),[])))] for i,j in pairs],np.float32).reshape(-1,3)
    item={'x':tensor(x.astype(np.float32)),'a':tensor(a),'event_x':tensor(event_x),'b':tensor(b),'incidence':tensor(incidence),'text':tensor(text),'media':{k:tensor(np.stack(v) if v else np.zeros((0,*MEDIA_SHAPES[k]),np.float32)) for k,v in media.items()},'media_event':{k:torch.tensor(v,dtype=torch.long) for k,v in media_event.items()},'pairs':torch.tensor(pairs,dtype=torch.long).reshape(-1,2),'pair_features':tensor(pair_features),'nodes':nodes,'events':events,'actor_indices':actor_nodes,'support':support,'contact':contact,'graph_fingerprint':graph_fingerprint(graph),'dataset_fingerprint':dataset.fingerprint,'aux_fingerprint':aux['fingerprint'],'media_details':media_details,'case_id':cid}
    return item


def attach_targets(item,dataset,aux):
    """仅训练/评测调用，prepare及网络forward不读取任何标签表。"""
    cid=item['case_id'];index={n:i for i,n in enumerate(item['nodes'])};task=role_task(dataset.manifest)
    item['role_target']=torch.full((len(index),),-1,dtype=torch.long);item['level_target']=item['role_target'].clone()
    for r in dataset.rows('person_labels',cid):
        node=entity_id('actor',r['actor_id'])
        if is_true(r['confirmed']) and node in index:item['role_target'][index[node]]=task['roles'].index(r['role_label']);item['level_target'][index[node]]=task['levels'].index(r['level_label'])
    item['fund_target']=next((int(r['label']) for r in dataset.rows('case_labels',cid) if is_true(r['confirmed'])),None)
    if item['fund_target'] is None:raise ValueError('联合训练缺少经确认案件标签')
    lookup={(entity_id('actor',r['from_actor']),entity_id('actor',r['to_actor'])):int(r['label']) for r in aux['controls'] if r['case_id']==cid}
    item['control_target']=torch.tensor([lookup.get((item['nodes'][i],item['nodes'][j]),-1) for i,j in item['pairs'].tolist()],dtype=torch.long)
    item['domain']=next((r['region']+' / '+r['case_type'] for r in dataset.tables['cases'] if r['case_id']==cid))
    return item


def quality_audit(dataset):
    aux=auxiliary(dataset);cache={};cases=[]
    from .core import build_case_graph
    for row in dataset.tables['cases']:
        item=prepare(dataset,build_case_graph(dataset,row['case_id']),aux,cache)
        cases.append({'case_id':row['case_id'],'entities':len(item['nodes']),'events':len(item['events']),'raw_modalities':{k:len(v) for k,v in item['media'].items()},'insufficient_actor_nodes':[item['nodes'][i] for i in item['actor_indices'] if len(item['support'][item['nodes'][i]])<2],'sampling_limits':[r['sampling']['scope'] for r in item['media_details'] if 'scope' in r['sampling']]})
    return {'implementation':'HD-EIA工程版：既有严格清洗+异构事件去重+实体事件发生矩阵+人工媒体对齐','dataset_fingerprint':dataset.fingerprint,'aux_fingerprint':aux['fingerprint'],'cases':cases,'scope':'指纹与字段通过只证明输入一致，不证明原始来源事实或人工对应正确'}


def clean_and_validate(source,destination):
    from .cleaning import clean_dataset
    from .core import load_dataset
    import shutil
    source=Path(source).resolve();destination=Path(destination).resolve()
    manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8-sig'));config=manifest.get('advanced',{})
    # 先验证辅助文件在命名范围内，再由原清洗器保留来源材料。
    paths=[local_path(source,config.get(k,'')) for k in ('media_file','control_file')]
    from .core import SCHEMA
    reserved={'manifest.json','cleaning_report.json'}|{name+'.csv' for name in SCHEMA}
    if len(set(paths))!=2 or any(p.name in reserved or p.suffix.lower() not in {'.json','.csv'} for p in paths):raise ValueError('高级辅助清单不能覆盖保留文件')
    result=clean_dataset(source,destination)
    for p in paths:
        target=destination/p.relative_to(source);target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and target.read_bytes()!=p.read_bytes():raise ValueError('高级清单与清洗目标文件冲突')
        shutil.copyfile(p,target)
    audit=quality_audit(load_dataset(destination));(destination/'hd_eia_audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'cleaning':result,'audit':audit,'output':str(destination)}
