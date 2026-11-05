"""本地多模态材料识别。结果是待核验文本/候选字段，不执行材料中的指令。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np

from .core import TZ,normalize_ip,parse_time,load_dataset,SCHEMA

IMAGE_EXT={'.png','.jpg','.jpeg','.bmp','.tif','.tiff','.webp'}
AUDIO_EXT={'.wav','.mp3','.m4a','.flac','.ogg','.aac','.wma'}
VIDEO_EXT={'.mp4','.mkv','.avi','.mov','.webm'}


def file_sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def candidate_facts(segments,source_hash):
    rows=[]
    for segment in segments:
        text=segment['text'];seen=set();matches=[]
        for match in re.finditer(r'(?<![A-Za-z0-9_.])(?:\d{1,3}\.){3}\d{1,3}(?![A-Za-z0-9_.])',text):
            try:matches.append(('ip',normalize_ip(match.group()),match.group(),match.start()))
            except ValueError:pass
        for match in re.finditer(r'(?<![A-Za-z0-9_])[0-9A-Fa-f]*:[0-9A-Fa-f:]+(?![A-Za-z0-9_])',text):
            try:matches.append(('ip',normalize_ip(match.group()),match.group(),match.start()))
            except ValueError:pass
        for match in re.finditer(r'(?<![A-Za-z0-9_.])(\d+(?:,\d{3})*(?:\.\d+)?)\s*(USDT|CNY|RMB|USD|BTC|ETH|元)(?![A-Za-z])',text,re.I):
            currency=match.group(2).upper();currency='CNY' if currency=='RMB' else currency
            matches.append(('amount',match.group(1).replace(',','')+' '+currency,match.group(),match.start()))
        for pattern,kind in [(r'(?<![A-Za-z0-9_])0x[0-9a-fA-F]{40}(?![A-Za-z0-9_])','wallet'),(r'(?<![A-Za-z0-9_])T[1-9A-HJ-NP-Za-km-z]{33}(?![A-Za-z0-9_])','wallet'),(r'(?<!\d)\d{16,19}(?!\d)','account_candidate'),(r'(?<![A-Za-z0-9_])(?:W|O|SIM)\d{3,}(?![A-Za-z0-9_])','record_identifier')]:
            matches.extend((kind,m.group(),m.group(),m.start()) for m in re.finditer(pattern,text))
        for kind,value,raw,position in matches:
            if (kind,value) in seen:continue
            seen.add((kind,value));rows.append({'kind':kind,'value':value,'raw':raw,'segment_id':segment['segment_id'],'text_offset':position,'page':segment.get('page',''),'start_seconds':segment.get('start_seconds',''),'box':segment.get('box',''),'source_sha256':source_hash,'confidence':segment.get('confidence'),'status':'候选字段，待人工核验；不能自动绑定身份或构造交易'})
    return rows


class LocalMediaProcessor:
    def __init__(self,project_root):
        self.root=Path(project_root);self.ocr=None;self.speech=None

    def _ocr(self,image):
        from PIL import Image,ImageOps
        if isinstance(image,(str,Path)):
            with Image.open(image) as opened:
                if opened.width*opened.height>40_000_000:raise ValueError('图像超过4000万像素，请先分片')
                image=ImageOps.exif_transpose(opened).convert('RGB')
        if image.width*image.height>40_000_000:raise ValueError('图像超过4000万像素')
        image=ImageOps.exif_transpose(image).convert('RGB')
        if self.ocr is None:
            import rapidocr
            model_dir=Path(rapidocr.__file__).parent/'models'
            paths={'Det':'PP-OCRv6_det_small.onnx','Cls':'ch_ppocr_mobile_v2.0_cls_mobile.onnx','Rec':'PP-OCRv6_rec_small.onnx'}
            if any(not (model_dir/name).is_file() for name in paths.values()):raise ValueError('本地OCR模型不完整，请安装依赖后再处理；识别阶段不联网下载')
            params={key+'.model_path':str(model_dir/name) for key,name in paths.items()}
            params.update({'EngineConfig.onnxruntime.intra_op_num_threads':2,'EngineConfig.onnxruntime.inter_op_num_threads':2})
            self.ocr=rapidocr.RapidOCR(params=params)
        result=self.ocr(np.array(image));rows=[]
        if result.txts:
            for text,score,box in zip(result.txts,result.scores,result.boxes):rows.append({'text':str(text),'confidence':float(score),'box':np.asarray(box).tolist(),'method':'local_ocr','coordinate_space':'image_pixels'})
        return rows

    def _audio(self,path,progress,max_seconds=600):
        import av
        with av.open(str(path)) as container:
            if not container.streams.audio:return [],['该材料无音轨，未执行音频转写']
            duration=float(container.duration/av.time_base) if container.duration else float(container.streams.audio[0].duration*container.streams.audio[0].time_base) if container.streams.audio[0].duration else None
            if duration is None:raise ValueError('无法确定音轨时长，请转换为标准WAV后处理')
            if duration and duration>max_seconds:raise ValueError(f'音轨超过{max_seconds}秒的本地处理上限，请先分段')
        if self.speech is None:
            from faster_whisper import WhisperModel
            directory=self.root/'runtime/models/whisper_base'
            if not all((directory/name).is_file() for name in ('model.bin','config.json','tokenizer.json','vocabulary.txt')):raise ValueError('本地语音权重缺失，请先运行模型准备脚本；处理材料时不自动联网')
            self.speech=WhisperModel(str(directory),device='cpu',compute_type='int8',cpu_threads=4,num_workers=1,local_files_only=True)
        progress('正在本地转写音轨…')
        segments,info=self.speech.transcribe(str(path),beam_size=3,vad_filter=True,condition_on_previous_text=False)
        rows=[]
        for segment in segments:
            if segment.end>max_seconds:raise ValueError('音轨解码时发现超出处理时长，请分段后重试')
            rows.append({'text':segment.text.strip(),'confidence':None,'start_seconds':float(segment.start),'end_seconds':float(segment.end),'avg_logprob':float(segment.avg_logprob),'no_speech_probability':float(segment.no_speech_prob),'language':info.language,'method':'local_asr'})
            progress(f'本地转写至 {segment.end:.1f} 秒')
        uncertain=sum(r['avg_logprob']<-1 or r['no_speech_probability']>.6 for r in rows)
        return rows,([f'{uncertain}个语音片段的识别诊断偏弱，需重点核对，不作事实置信度'] if uncertain else [])

    def extract(self,path,origin,progress=None,max_pages=100,max_video_frames=30,video_interval=10,max_audio_seconds=600,pdf_mode='hybrid'):
        progress=progress or (lambda text:None);path=Path(path).resolve()
        if origin not in {'real','synthetic'}:raise ValueError('须明确材料为真实来源或合成验证')
        if not path.is_file() or path.stat().st_size>500*1024*1024:raise ValueError('请选择不超过500MB的本地材料文件')
        if not 1<=max_pages<=500 or not 1<=max_video_frames<=300 or video_interval<=0 or not 1<=max_audio_seconds<=7200:raise ValueError('页数、帧数、采样间隔或音轨时长限制无效')
        if pdf_mode not in {'auto','hybrid'}:raise ValueError('PDF模式须为auto或hybrid')
        suffix=path.suffix.lower();digest=file_sha(path);segments=[];notes=[];sampling={}
        progress('正在读取本地材料：'+path.name)
        if suffix in IMAGE_EXT:
            from PIL import Image
            with Image.open(path) as image:
                total=getattr(image,'n_frames',1);sampling={'total_frames':total,'processed_frames':min(total,max_pages),'truncated':total>max_pages}
                for index in range(min(total,max_pages)):
                    image.seek(index);segments.extend({**r,**({'page':index+1} if total>1 else {})} for r in self._ocr(image))
                if total>max_pages:notes.append('多帧图像超过处理上限，并非全部页面')
        elif suffix=='.pdf':
            from pypdf import PdfReader
            import pypdfium2 as pdfium
            reader=PdfReader(str(path))
            if reader.is_encrypted:raise ValueError('PDF已加密，请提供经授权解密后的文件')
            total=len(reader.pages);sampling={'total_pages':total,'processed_pages':min(total,max_pages),'truncated':total>max_pages,'pdf_mode':pdf_mode}
            rendered=None
            try:
                for index,page in enumerate(reader.pages[:max_pages]):
                    text=(page.extract_text() or '').strip();progress(f'处理PDF第{index+1}/{min(total,max_pages)}页')
                    if len(text)>=20:segments.append({'text':text,'confidence':None,'page':index+1,'method':'pdf_text','view':'native_text'})
                    has_image=bool(len(page.images)) if pdf_mode=='hybrid' and len(text)>=20 else False
                    if len(text)<20 or has_image:
                        if rendered is None:rendered=pdfium.PdfDocument(str(path))
                        canvas=rendered[index]
                        width,height=canvas.get_size()
                        if width*height*4>40_000_000:canvas.close();raise ValueError('PDF页面尺寸超过本地栅格化上限，请先分片')
                        bitmap=canvas.render(scale=2)
                        try:rows=self._ocr(bitmap.to_pil());segments.extend({**r,'page':index+1,'view':'page_raster','coordinate_space':'rendered_page_pixels_144dpi'} for r in rows)
                        finally:bitmap.close();canvas.close()
            finally:
                if rendered is not None:rendered.close()
            if any(r['method']=='pdf_text' for r in segments):notes.append('PDF图文页保留原文本与页面OCR两个视图，可能重复，不能算作多份独立材料' if pdf_mode=='hybrid' else '快速模式优先原文本，只对少文本页OCR；图文页中的图片文字可能遗漏')
            if total>max_pages:notes.append('PDF页数超过上限，本次只处理前置页；结果不是全文')
        elif suffix=='.docx':
            from docx import Document
            from PIL import Image
            with zipfile.ZipFile(path) as archive:
                if len(archive.infolist())>10000 or sum(x.file_size for x in archive.infolist())>200*1024*1024:raise ValueError('DOCX解压体积或部件数超过处理上限')
                document=Document(str(path))
                for index,paragraph in enumerate(document.paragraphs):
                    if paragraph.text.strip():segments.append({'text':paragraph.text.strip(),'confidence':None,'paragraph':index+1,'method':'docx_text'})
                for t,table in enumerate(document.tables,1):
                    for r,row in enumerate(table.rows,1):segments.append({'text':' | '.join(cell.text for cell in row.cells),'confidence':None,'table':t,'row':r,'method':'docx_table'})
                images=[x for x in archive.infolist() if x.filename.startswith('word/media/') and Path(x.filename).suffix.lower() in IMAGE_EXT]
                for item in images[:40]:
                    if item.file_size>50*1024*1024:notes.append('跳过超大内嵌图片：'+item.filename);continue
                    with Image.open(io.BytesIO(archive.read(item))) as image:segments.extend({**row,'part':item.filename} for row in self._ocr(image))
                if len(images)>40:notes.append('DOCX内嵌图片超过40张，仅识别前40张')
        elif suffix in AUDIO_EXT:segments,extra=self._audio(path,progress,max_audio_seconds);notes+=extra
        elif suffix in VIDEO_EXT:
            import cv2
            from PIL import Image
            capture=cv2.VideoCapture(str(path))
            try:
                fps=capture.get(cv2.CAP_PROP_FPS);count=capture.get(cv2.CAP_PROP_FRAME_COUNT)
                if not capture.isOpened() or fps<=0 or count<=0:raise ValueError('无法读取视频帧或时长')
                duration=count/fps;number=min(max_video_frames,max(1,math.ceil(duration/video_interval)))
                times=np.linspace(0,max(0,duration-1/fps),number);sampling={'duration_seconds':duration,'frame_times':times.tolist(),'sampled_frames':number,'sampled_only':True,'requested_interval':video_interval,'frame_limit_reached':math.ceil(duration/video_interval)>max_video_frames}
                for at in times:
                    capture.set(cv2.CAP_PROP_POS_MSEC,float(at)*1000);ok,frame=capture.read()
                    if not ok:notes.append(f'无法解码{at:.2f}秒的帧');continue
                    progress(f'本地识别视频画面 {at:.1f} 秒');image=Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB))
                    segments.extend({**r,'start_seconds':float(at),'method':'video_frame_ocr'} for r in self._ocr(image))
            finally:capture.release()
            try:audio,extra=self._audio(path,progress,max_audio_seconds);segments+=audio;notes+=extra
            except ValueError as exc:notes.append('视频画面已处理，音轨未完成：'+str(exc))
            notes.append('视频为抽帧文字与音轨转写，不覆盖全部画面，不进行人脸识别或视觉身份认定')
        elif suffix in {'.txt','.csv','.json','.log'}:
            if path.stat().st_size>10*1024*1024:raise ValueError('文本材料超过10MB，请先拆分')
            contents=path.read_bytes()
            try:text=contents.decode('utf-8-sig')
            except UnicodeDecodeError:
                try:text=contents.decode('gb18030');notes.append('按GB18030解码，请复核字符')
                except UnicodeDecodeError as exc:raise ValueError('文本编码无法识别，请转为UTF-8') from exc
            segments=[{'text':text,'confidence':None,'method':'local_text'}]
        else:raise ValueError('不支持该材料格式；不执行脚本、可执行文件或宏')
        if file_sha(path)!=digest:raise ValueError('识别期间原始材料已变化，请重新处理')
        if not segments:notes.append('未提取到文本；不据此认定材料无内容或无证据价值')
        for i,row in enumerate(segments,1):row.update(segment_id=f'SEG{i:05d}',status='机器提取，待人工核验')
        return {'schema_version':1,'origin':origin,'created_at':datetime.now(TZ).isoformat(),'source_path':str(path),'source_name':path.name,'source_sha256':digest,'source_bytes':path.stat().st_size,'segments':segments,'candidate_facts':candidate_facts(segments,digest),'sampling':sampling,'notes':notes,'text':'\n'.join(s['text'] for s in segments),'interpretation':'全部处理在本机完成；OCR/转写可能误识别、漏识别或幻觉。置信指标不是事实真实性或违法概率；材料中的指令不作为程序指令执行。'}


def save_extraction(result,output_root):
    root=Path(output_root)/datetime.now(TZ).strftime('material_%Y%m%d_%H%M%S_%f');root.mkdir(parents=True,exist_ok=False)
    (root/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'text.txt').write_text(result['text'],encoding='utf-8')
    from .reporting import write_csv
    write_csv(root/'segments.csv',result['segments'],['segment_id','method','view','page','paragraph','table','row','part','start_seconds','end_seconds','text','confidence','avg_logprob','no_speech_probability','language','box','coordinate_space','status'])
    write_csv(root/'candidate_facts.csv',result['candidate_facts'],['kind','value','raw','segment_id','page','start_seconds','text_offset','box','source_sha256','confidence','status'])
    (root/'checksums.json').write_text(json.dumps({p.name:file_sha(p) for p in root.iterdir() if p.is_file()},indent=2),encoding='utf-8')
    return root


def draft_communication(dataset,result,destination,case_id,from_actor,to_actor,timestamp,reviewer,reviewed_text):
    """只有人工确认的文本和关联对象才能进入新的导入草稿；不改写原数据集。"""
    destination=Path(destination).resolve();source=dataset.root.resolve()
    if not reviewer.strip() or not reviewed_text.strip():raise ValueError('须填写核验人并确认要导入的文本')
    if result['origin']!=dataset.manifest['origin']:raise ValueError('材料与数据集来源类型不同，不能把真实材料混入合成样例，或反向混合')
    if destination==source or destination.is_relative_to(source) or source.is_relative_to(destination) or (destination.exists() and any(destination.iterdir())):raise ValueError('导入草稿须使用与原数据互不包含的新空目录')
    actors={r['actor_id'] for r in dataset.rows('actors',case_id)}
    if from_actor not in actors or to_actor not in actors:raise ValueError('通信双方必须是当前案件已有主体；不能从识别文本自动绑定身份')
    when=parse_time(timestamp);material=Path(result['source_path'])
    if material.suffix.lower() not in IMAGE_EXT|AUDIO_EXT|VIDEO_EXT|{'.pdf','.docx','.txt','.csv','.json','.log'}:raise ValueError('材料导入不支持该文件格式')
    if file_sha(material)!=result['source_sha256']:raise ValueError('原始材料已变化，请重新识别和核验')
    if load_dataset(source).fingerprint!=dataset.fingerprint:raise ValueError('原数据在载入后变化，请重新载入核对')
    sid='MEDIA_'+result['source_sha256'][:12];event='MEDIA_EVT_'+datetime.now(TZ).strftime('%Y%m%d%H%M%S%f')
    sources=dataset.tables['sources']
    if any(r['case_id']==case_id and r['source_id']==sid for r in sources):raise ValueError('该材料已存在于此案件，请复核后选择已有来源')
    destination.mkdir(parents=True,exist_ok=True)
    # 只复制已导入数据与明确引用的材料，不递归跟随任意子目录或符号链接。
    keep=[source/'manifest.json',*[source/(table+'.csv') for table in SCHEMA],source/'cleaning_report.json'];relocations={}
    for key in ('evaluation_groups','configuration'):
        if dataset.manifest.get(key):keep.append(source/dataset.manifest[key])
    for key in ('media_file','control_file'):
        value=dataset.manifest.get('advanced',{}).get(key)
        if value:keep.append(source/value)
    intake_parent=None
    if dataset.manifest.get('intake'):intake_parent=source/dataset.manifest['intake']
    elif (source/'cleaning_report.json').is_file():
        previous_report=json.loads((source/'cleaning_report.json').read_text(encoding='utf-8'))
        if previous_report.get('parent_intake_sha256'):intake_parent=source/'lineage/intake_report.json'
    if intake_parent is not None:
        lineage=destination/'lineage';lineage.mkdir();shutil.copyfile(intake_parent,lineage/'intake_report.json')
    rewritten={'manifest.json','sources.csv','communications.csv','cleaning_report.json'}
    for row in sources:
        ref=row['reference']
        if ref and not ref.startswith('ref:'):
            original=(source/ref).resolve()
            if original.is_file() and original.is_relative_to(source) and original.relative_to(source).as_posix() in rewritten:
                relative='_import_parent/source_materials/'+file_sha(original)[:16]+'_'+original.name
                target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(original,target)
                relocations[(row['case_id'],row['source_id'])]=relative
            else:keep.append(original)
    for path in keep:
        if not path.is_file():continue
        if not path.resolve().is_relative_to(source):raise ValueError('原数据含越界材料引用')
        relative=path.relative_to(source);target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
    reference='evidence/import_'+result['source_sha256'][:16]+material.suffix.lower();(destination/'evidence').mkdir(exist_ok=True);shutil.copyfile(material,destination/reference)
    rows={table:[{k:r.get(k,'') for k in fields} for r in dataset.tables[table]] for table,fields in SCHEMA.items()}
    for row in rows['sources']:
        if (row['case_id'],row['source_id']) in relocations:row['reference']=relocations[(row['case_id'],row['source_id'])]
    rows['sources'].append({'case_id':case_id,'source_id':sid,'title':'人工核验材料：'+material.name,'reference':reference})
    audit_name='import_audit_'+event+'.json'
    rows['sources'].append({'case_id':case_id,'source_id':'REVIEW_'+event,'title':'材料导入人工确认记录','reference':audit_name})
    rows['communications'].append({'case_id':case_id,'event_id':event,'from_actor':from_actor,'to_actor':to_actor,'timestamp':when.isoformat(),'channel':'人工确认的材料摘要','device_id':'','summary':reviewed_text,'source_id':sid})
    for table in ('sources','communications'):
        parent=destination/'_import_parent';parent.mkdir(exist_ok=True)
        if (source/(table+'.csv')).is_file():shutil.copyfile(source/(table+'.csv'),parent/(table+'.csv'))
        with (destination/(table+'.csv')).open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=SCHEMA[table]);writer.writeheader();writer.writerows(rows[table])
    audit={'parent_dataset_fingerprint':dataset.fingerprint,'material_sha256':result['source_sha256'],'case_id':case_id,'source_id':sid,'event_id':event,'reviewer':reviewer.strip(),'reviewed_at':datetime.now(TZ).isoformat(),'event_time':when.isoformat(),'original_machine_text':result['text'],'confirmed_text':reviewed_text,'preserved_source_references':[{'case_id':key[0],'source_id':key[1],'reference':value} for key,value in relocations.items()],'policy':'人工作出的主体/时间对应，仅增加通信摘要，不自动导入识别候选交易或身份'}
    (destination/audit_name).write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    from .core import IDS
    origins={};outputs={}
    for table in SCHEMA:
        path=destination/(table+'.csv')
        if not path.is_file():continue
        outputs[path.name]=file_sha(path);previous={r[IDS[table]]+'|'+r['case_id']:r for r in dataset.tables[table]};origins[table]={}
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream)
            for row in reader:
                key=row[IDS[table]]+'|'+row['case_id'];old=previous.get(key)
                origins[table][key]={'line':old.get('_original_line') if old else reader.line_num,'sha256':old.get('_original_sha256','') if old else outputs[path.name],'sheet':old.get('_original_sheet','') if old else '','stages':[{'line':reader.line_num,'sha256':outputs[path.name]}]}
    manifest=dict(dataset.manifest);manifest.update(cleaning='cleaning_report.json',derived_import=audit_name,parent_dataset_fingerprint=dataset.fingerprint)
    manifest.pop('intake',None)
    (destination/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    provenance={'valid':True,'operation':'reviewed_material_import','source_directory':str(source),'origins':origins,'output_sha256':outputs,'quarantined':[],'changes':[{'event_id':event,'source_id':sid,'reviewer':reviewer.strip()}],'parent_dataset_fingerprint':dataset.fingerprint}
    if intake_parent is not None:provenance['parent_intake_sha256']=file_sha(intake_parent)
    (destination/'cleaning_report.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding='utf-8')
    load_dataset(destination)
    return destination
