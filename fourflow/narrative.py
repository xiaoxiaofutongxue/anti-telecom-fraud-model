"""文书型四流资料接入、可复核解析与资料检索；不套用合成图分类权重。"""
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime,date
from collections import Counter
import csv,hashlib,io,json,re,shutil,tempfile,zipfile
from decimal import Decimal
from . import __version__
from .core import TZ
from .io_utils import atomic_json
from .narrative_extract import enrich,FLOW_NAMES
from .narrative_fields import known_header

VERSION=__version__
LIMIT_RECORDS=10000
SUPPORTED={'.xlsx','.csv','.tsv','.txt','.md','.docx'}
HEADERS={'序号','时间','日期','空间节点','事件/侦查细节','涉及流向','起诉书段落','宣誓书段落','来源文书','证据来源','段落','犯罪阶段','金额/规模','流转路径(来源→中介→去向)','人员/角色','产业/企业','虚拟货币地址','数量(BTC)','币种','地址','域名','App名称','资产','园区/地点','用途/功能','侦查细节(证据)','关联域名','注册时间','处置/时间','关联资金','资产/工具','园区/位置','园区名称','坐标','位置/坐标','GPS坐标'}
HEADERS.update({'收款方','付款人/日期','金额(美元)','金额（美元）','付款机构','名称','说明','关联主体','币种/网络','地址/节点','注册商/注册局','状态/处置','侦查发现','处置/发现','数量/规模','实体/节点','角色/功能','关联流向','指标','数值','起诉书依据(段落)','宣誓书依据(段落)'})


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _relative(root,value):
    if not isinstance(value,str) or Path(value).is_absolute() or Path(value).drive:raise ValueError('资料路径须为工作区内的相对路径')
    path=(root/value).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.is_symlink():raise ValueError('资料文件缺失或路径越界')
    return path


def _cell(value):
    if value is None:return ''
    if isinstance(value,(datetime,date)):return value.isoformat()
    return str(value).strip()


def _kind(sheet,fields):
    for word,kind in [('资金流','fund_summary'),('人员流','person'),('信息流','information'),('产业回流','return'),('产业流','industry'),('联动矩阵','matrix'),('时空','event'),('清单','asset'),('证据索引','evidence_index'),('总览','overview')]:
        if word in sheet:return kind
    return 'event' if any(k in fields for k in ('时间','日期','事件','内容')) else 'paragraph'


def _record(case,sheet,position,fields,columns=None,section='',formula=False,source_context=None,kind=None):
    if max((len(str(v)) for v in fields.values()),default=0)>20000:raise ValueError('单条文字超过20000字，请按段落拆分')
    text='\n'.join(k+'：'+v for k,v in fields.items() if v)
    rid='R_'+hashlib.sha256((case['case_id']+'|'+sheet+'|'+str(position)+'|'+text).encode()).hexdigest()[:18]
    record={'id':rid,'case_id':case['case_id'],'source_id':case['source_id'],'sheet':sheet,'position':position,'locator':sheet+' / '+str(position),'fields':fields,'column_map':columns or {},'section':section,'text':text,'kind':kind or _kind(sheet,fields),'formula_present':formula,'source_context':source_context or {}}
    enrich(record)
    if formula:record['issues'].append('含公式，仅保留表达式，未计算或读取缓存结果')
    return record


def read_document(path,case):
    """保留全部非空内容行。结构行另记，表头可在任意前置位置及多段表中出现。"""
    path=Path(path);records=[];structure=[];sheet_info=[]
    if path.stat().st_size>50*1024*1024:raise ValueError('文字资料文件最多50MB，请拆分较大的资料')
    def add(record):
        records.append(record)
        if len(records)>LIMIT_RECORDS:raise ValueError('单个文件超过10000条内容，请拆分后接入')
    if path.suffix.lower() in {'.xlsx','.docx'}:
        with zipfile.ZipFile(path) as z:
            if len(z.infolist())>3000 or sum(r.file_size for r in z.infolist())>150*1024*1024:raise ValueError('资料展开体积过大')
    if path.suffix.lower()=='.xlsx':
        from openpyxl import load_workbook
        from openpyxl.utils import get_column_letter
        book=load_workbook(path,read_only=True,data_only=False,keep_links=False)
        try:
            if len(book.sheetnames)>100:raise ValueError('单文件工作表过多，请拆分')
            for sheet in book:
                if sheet.max_column>200 or sheet.max_row>50001:raise ValueError('工作表超过200列/50000行，请清除多余格式或拆分')
                headers=None;section='';count=0;header_rows=[];source_context={}
                for cells in sheet:
                    values=[_cell(c.value) for c in cells]
                    if not any(values):continue
                    active=[v for v in values if v];formula=any(c.data_type=='f' for c in cells)
                    if cells[0].row<=3 and len(active)==1 and re.search('来源|出处',active[0]):source_context={'text':active[0],'locator':sheet.title+' / '+str(cells[0].row),'position':cells[0].row}
                    is_header=len(active)>=2 and not formula and (sum(v in HEADERS or known_header(v) for v in active)>=2 or ((active[0] in HEADERS or known_header(active[0])) and all(len(v)<35 for v in active)))
                    if is_header:
                        headers=values;header_rows.append(cells[0].row);structure.append({'sheet':sheet.title,'row':cells[0].row,'kind':'header','values':values});continue
                    if len(active)==1 and (cells[0].row<=2 or re.match(r'^[一二三四五六七八九十]+[、．.]',active[0])):
                        section=active[0];structure.append({'sheet':sheet.title,'row':cells[0].row,'kind':'heading','text':section})
                        # 方法/核心结论/来源也属于资料内容，不能作为指令或丢弃。
                        if cells[0].row==1:continue
                    fields={};columns={}
                    for i,value in enumerate(values):
                        if not value:continue
                        key=headers[i] if headers and i<len(headers) and headers[i] else get_column_letter(i+1)
                        if key in fields:key+=' ('+get_column_letter(i+1)+')'
                        fields[key]=value;columns[key]=get_column_letter(i+1)+str(cells[0].row)
                    record=_record(case,sheet.title,cells[0].row,fields,columns,section,formula,dict(source_context),'note' if len(active)==1 else None)
                    record['range']='A'+str(cells[0].row)+':'+get_column_letter(len(cells))+str(cells[0].row)
                    add(record);count+=1
                sheet_info.append({'name':sheet.title,'header_rows':header_rows,'records':count,'rows':sheet.max_row,'columns':sheet.max_column})
        finally:book.close()
    elif path.suffix.lower() in {'.csv','.tsv'}:
        raw=path.read_bytes()
        for encoding in ('utf-8-sig','gb18030'):
            try:text=raw.decode(encoding);break
            except UnicodeDecodeError:continue
        else:raise ValueError('文本编码无法识别，请另存UTF-8')
        try:delimiter=csv.Sniffer().sniff(text[:8192],delimiters=',\t;').delimiter
        except csv.Error:delimiter='\t' if path.suffix.lower()=='.tsv' else ','
        reader=csv.reader(io.StringIO(text,newline=''),delimiter=delimiter);headers=next(reader,[])
        if not headers or len(headers)>200 or any(not h.strip() for h in headers) or len(set(h.strip() for h in headers))!=len(headers):raise ValueError('CSV/TSV表头不能为空、重复或超过200列')
        structure.append({'sheet':'数据表','row':1,'kind':'header','values':headers})
        count=0;last_end=reader.line_num
        for values in reader:
            begin=last_end+1;last_end=reader.line_num
            if not any(v.strip() for v in values):continue
            if len(values)!=len(headers):raise ValueError('CSV/TSV第'+str(reader.line_num)+'行列数与表头不符')
            fields={h.strip():v.strip() for h,v in zip(headers,values) if v.strip()}
            record=_record(case,'数据表',begin,fields,{h.strip():h.strip()+' / '+str(begin)+'-'+str(reader.line_num) for h in headers},formula=any(v.lstrip().startswith('=') for v in values));record['range']='CSV/TSV物理行 '+str(begin)+'-'+str(reader.line_num);add(record);count+=1
        sheet_info.append({'name':'数据表','header_rows':[1],'records':count,'rows':reader.line_num,'columns':len(headers)})
    elif path.suffix.lower()=='.docx':
        from docx import Document
        doc=Document(path)
        for i,p in enumerate(doc.paragraphs,1):
            if p.text.strip():add(_record(case,'正文','段落'+str(i),{'文字':p.text.strip()}))
        for i,table in enumerate(doc.tables,1):
            headers=None
            for j,row in enumerate(table.rows,1):
                values=[c.text.strip() for c in row.cells]
                if not any(values):continue
                if j==1 and sum(v in HEADERS for v in values)>=2:headers=values;structure.append({'table':i,'row':j,'kind':'header','values':values});continue
                fields={}
                for k,v in enumerate(values):
                    if not v:continue
                    key=headers[k] if headers and headers[k] else '列'+str(k+1)
                    if key in fields:key+=' ['+str(k+1)+']'
                    fields[key]=v
                add(_record(case,'表格'+str(i),'行'+str(j),fields))
    else:
        raw=path.read_bytes()
        for encoding in ('utf-8-sig','gb18030'):
            try:text=raw.decode(encoding);break
            except UnicodeDecodeError:continue
        else:raise ValueError('文本编码无法识别，请另存UTF-8')
        for i,line in enumerate(text.splitlines(),1):
            if line.strip():add(_record(case,'正文',i,{'文字':line.strip()}))
    if not records:raise ValueError('资料中没有可读取的文字内容')
    return records,{'sheets':sheet_info,'structure':structure}


@dataclass
class NarrativeCorpus:
    root:Path
    manifest:dict
    records:list
    fingerprint:str
    @property
    def cases(self):return self.manifest['cases']
    def rows(self,case_id=None):return [r for r in self.records if case_id is None or r['case_id']==case_id]
    def source_file(self,case_id):
        case=next(c for c in self.cases if c['case_id']==case_id);return _relative(self.root,case['file'])
    def validate(self):
        current=load_corpus(self.root)
        if current.fingerprint!=self.fingerprint:raise ValueError('案件文本或原件在载入后变化，请重新载入')
        return True


def load_corpus(root):
    root=Path(root).resolve();path=root/'narrative_manifest.json';raw=path.read_bytes();manifest=json.loads(raw)
    if not isinstance(manifest,dict):raise ValueError('案件文本清单格式无效')
    if manifest.get('kind')!='narrative_fourflow' or manifest.get('schema_version')!=1 or manifest.get('origin') not in {'real','synthetic'}:raise ValueError('不是支持的案件文本资料目录')
    records_path=_relative(root,'records.json');data=records_path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=manifest.get('records_sha256'):raise ValueError('案件文本解析记录已变化，请重新接入')
    records=json.loads(data)
    if not isinstance(records,list) or not records or len(records)>50000:raise ValueError('案件文本记录数量无效')
    cases=manifest.get('cases',[])
    if not isinstance(cases,list) or not 1<=len(cases)<=30:raise ValueError('案件文本来源数量无效')
    if any(not isinstance(c,dict) or not re.fullmatch(r'[a-f0-9]{64}',str(c.get('sha256',''))) or c.get('case_id')!='DOC_'+c['sha256'][:16] or c.get('source_id')!='SRC_'+c['sha256'][:16] for c in cases):raise ValueError('案件文本来源编号/指纹格式无效')
    lookup={c['case_id']:c for c in cases}
    if not cases or len(lookup)!=len(cases):raise ValueError('案件文本编号缺失或重复')
    for case in cases:
        if sha(_relative(root,case['file']))!=case['sha256']:raise ValueError('案件文本原件缺失或被替换')
    ids=set()
    for r in records:
        if not isinstance(r,dict) or not re.fullmatch(r'R_[a-f0-9]{18}',str(r.get('id',''))):raise ValueError('案件文本记录编号格式无效')
        if r['case_id'] not in lookup or r['id'] in ids or r['source_id']!=lookup[r['case_id']]['source_id']:raise ValueError('案件文本记录的来源或编号不一致')
        ids.add(r['id'])
    return NarrativeCorpus(root,manifest,records,hashlib.sha256(raw+data).hexdigest())


def import_documents(paths,destination,origin='real',progress=None):
    progress=progress or (lambda text:None);destination=Path(destination).resolve();paths=[Path(p).resolve() for p in paths]
    if not paths or len(paths)>30 or origin not in {'real','synthetic'}:raise ValueError('一次选择1至30个案件资料文件，并明确来源类型')
    if destination.exists():raise ValueError('案件文本只保存到新的目录，不能覆盖已有资料')
    if any(p.suffix.lower() not in SUPPORTED or not p.is_file() or p.is_symlink() for p in paths):raise ValueError('支持本地XLSX、CSV、TSV、DOCX、TXT、MD资料文件')
    if any(destination==p or destination.is_relative_to(p.parent) for p in paths):raise ValueError('资料副本需保存到独立于原件文件夹的新目录')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='narrative_stage_',dir=destination.parent) as tmp:
        staged=Path(tmp)/'data';(staged/'sources').mkdir(parents=True);cases=[];records=[];seen=set();duplicates=[]
        for i,path in enumerate(paths,1):
            progress(f'读取案件资料 {i}/{len(paths)}：{path.name}');digest=sha(path)
            if digest in seen:duplicates.append(path.name);continue
            seen.add(digest);ref='sources/'+digest[:24]+path.suffix.lower();copy=staged/ref;shutil.copyfile(path,copy)
            if sha(copy)!=digest or sha(path)!=digest:raise ValueError('原件在接入过程中变化，请重新选择')
            case={'case_id':'DOC_'+digest[:16],'source_id':'SRC_'+digest[:16],'title':path.stem,'original_name':path.name,'sha256':digest,'file':ref,'source_status':'用户提供的资料，未外部核对文书真实性'}
            parsed,structure=read_document(copy,case);case.update(structure);cases.append(case);records.extend(parsed)
            if len(records)>50000:raise ValueError('本批超过50000条文字内容，请分批接入')
        atomic_json(staged/'records.json',records)
        manifest={'kind':'narrative_fourflow','schema_version':1,'version':VERSION,'origin':origin,'created_at':datetime.now(TZ).isoformat(),'cases':cases,'records_sha256':sha(staged/'records.json'),'duplicate_inputs_skipped':duplicates,'method':'按列标签和本地文本规则提取；不训练分类器，不联网，不将正文作为可执行指令','scope':'文书陈述、表内分析及提及线索，仍需原始材料与人工核验。'}
        atomic_json(staged/'narrative_manifest.json',manifest);load_corpus(staged);staged.rename(destination)
    return load_corpus(destination)


def search(corpus,query,case_id=None,limit=30):
    if not isinstance(query,str) or not query.strip():return []
    corpus.validate();rows=corpus.rows(case_id)
    if not rows:return []
    terms=query.strip();bound=max(1,min(100,int(limit)))
    if re.fullmatch(r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?',terms):
        value=Decimal(terms.replace(',',''));pattern=r'(?<![\d.,])'+re.escape(terms)+r'(?![\d.,])';hits=[]
        for row in rows:
            if row.get('formula_present'):continue
            matches=[a for a in row['amounts'] if a['value'] is not None and Decimal(a['value'])==value]
            if matches or re.search(pattern,row['text']):hits.append({'record':row,'similarity':1.0,'match_mode':'numeric_exact','scope':'数值相等或原文完整数字匹配，不代表同一笔支付或事实确认'})
        return hits[:bound]
    if re.fullmatch(r'(?:TV|FV|CC|Worker|Company|INDIVIDUAL|Domain)[ -]?\d+',terms,re.I):
        pattern=r'(?<![A-Za-z0-9_-])'+re.escape(terms)+r'(?![A-Za-z0-9_-])'
        return [{'record':r,'similarity':1.0,'match_mode':'identifier_exact','scope':'原文完整编号匹配，编号仍按案件及文书隔离'} for r in rows if not r.get('formula_present') and re.search(pattern,r['text'],re.I)][:bound]
    from sklearn.feature_extraction.text import TfidfVectorizer
    terms=query.strip();expansion={'资金回流':'回流 再投资 复建','园区迁移':'迁移 迁址 园区','钱包':'钱包 地址 BTC USDT USDC','产业':'产业 企业 设备 服务'}
    terms+=' '+' '.join(v for k,v in expansion.items() if k in query)
    vectorizer=TfidfVectorizer(analyzer='char',ngram_range=(2,4),max_features=60000,sublinear_tf=True)
    try:matrix=vectorizer.fit_transform([r['text'] for r in rows]);scores=(matrix@vectorizer.transform([terms]).T).toarray().ravel()
    except ValueError:return []
    ranked=sorted(((float(score),r) for score,r in zip(scores,rows) if score>0),key=lambda p:(-p[0],p[1]['id']))[:bound]
    return [{'record':r,'similarity':score,'scope':'文字相似度，只用于检索排序'} for score,r in ranked]
