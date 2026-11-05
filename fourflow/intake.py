"""本地CSV/Excel列映射接入。保留原件，输出新的规范数据及逐行对应。"""
from pathlib import Path
from datetime import datetime,date
import csv,hashlib,io,json,re,zipfile
from .core import SCHEMA,IDS,TZ,load_dataset
from .io_utils import atomic_json

TABLE_LABELS={'funds':'资金流水','communications':'通信记录','network_events':'IP访问','shipments':'产业运输','actors':'主体信息','identity_links':'身份对应','relations':'主体关系','case_labels':'案件标签','person_labels':'人员标签'}
ALIASES={'case_id':['案件编号','案号'],'tx_id':['交易编号','流水号','交易流水号'],'timestamp':['时间','交易时间','发生时间','访问时间','通信时间'],'amount':['金额','交易金额'],'currency':['币种','货币'],'from_id':['转出账户','付款账号','付款账户','转出地址'],'to_id':['转入账户','收款账号','收款账户','转入地址'],'chain':['链','账户域','网络域'],'from_kind':['转出类型'],'to_kind':['转入类型'],'actor_id':['主体编号','人员编号'],'alias':['别名','姓名','主体名称'],'from_actor':['发送主体','发送人编号'],'to_actor':['接收主体','接收人编号'],'event_id':['通信编号','事件编号'],'summary':['通信摘要','摘要','内容'],'channel':['渠道','通信方式'],'device_id':['设备编号','设备'],'ip':['IP','IP地址','访问IP'],'access_id':['访问编号','记录编号'],'network_scope':['IP网络域'],'event_type':['事件类型','访问类型'],'source_port':['源端口'],'account_id':['访问账户'],'account_kind':['访问账户类型'],'order_id':['订单编号','订单号'],'shipment_id':['运输编号'],'from_place':['发货地','起运地'],'to_place':['收货地'],'sender_id':['发货主体'],'receiver_id':['收货主体'],'carrier_id':['承运主体'],'vehicle_id':['车辆编号'],'goods_label':['物品名称'],'service_type':['产业类型'],'label':['目标标签'],'confirmed':['是否确认'],'role_label':['角色标签'],'level_label':['层级标签'],'role':['来源角色'],'role_verified':['角色已核实'],'actor_type':['主体类型'],'link_id':['对应编号'],'entity_id':['实体编号'],'entity_kind':['实体类型'],'verified':['已核实'],'verification_note':['核实依据'],'relation_id':['关系编号'],'relation':['关系说明']}
LIMIT_ROWS=50000


def _cell(value,number_format='',kind=None):
    if value is None:return {'value':'','kind':'empty'}
    if isinstance(value,(datetime,date)):return {'value':value.isoformat(sep=' ') if isinstance(value,datetime) else value.isoformat(),'kind':'date'}
    if isinstance(value,bool):return {'value':'true' if value else 'false','kind':'bool'}
    if isinstance(value,(int,float)):
        text=str(value)
        if re.fullmatch(r'0{2,30}',number_format or '') and float(value).is_integer() and 0<=value<10**15:text=str(int(value)).zfill(len(number_format));return {'value':text,'kind':'padded_identifier'}
        return {'value':text,'kind':'number'}
    return {'value':str(value),'kind':'formula' if kind=='f' else 'text'}


def read_table(path,sheet=None):
    path=Path(path).resolve()
    if not path.is_file() or path.stat().st_size>50*1024*1024:raise ValueError('请选择不超过50MB的CSV/TSV/XLSX文件')
    raw=path.read_bytes();suffix=path.suffix.lower();rows=[];sheets=[];encoding=None
    if suffix in {'.csv','.tsv'}:
        for candidate in ('utf-8-sig','gb18030'):
            try:text=raw.decode(candidate);encoding=candidate;break
            except UnicodeDecodeError:continue
        else:raise ValueError('文本编码无法识别，请另存为UTF-8')
        try:delimiter=csv.Sniffer().sniff(text[:8192],delimiters=',\t;').delimiter
        except csv.Error:delimiter='\t' if suffix=='.tsv' else ','
        iterator=csv.reader(io.StringIO(text,newline=''),delimiter=delimiter)
        header=next(iterator,[])
        while True:
            line=iterator.line_num+1
            try:row=next(iterator)
            except StopIteration:break
            if not any(v.strip() for v in row):continue
            if len(row)!=len(header):raise ValueError(f'第{line}行字段数量与表头不一致')
            rows.append({'line':line,'cells':[_cell(v) for v in row]})
            if len(rows)>LIMIT_ROWS:raise ValueError('单表超过50000行，请分表接入')
    elif suffix=='.xlsx':
        from openpyxl import load_workbook
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist())>2000 or sum(r.file_size for r in archive.infolist())>150*1024*1024:raise ValueError('Excel展开体积过大，请分表接入')
        workbook=load_workbook(io.BytesIO(raw),read_only=True,data_only=False,keep_links=False)
        try:
            sheets=list(workbook.sheetnames);sheet=sheet or sheets[0]
            if sheet not in sheets:raise ValueError('选择的工作表不存在')
            worksheet=workbook[sheet]
            if worksheet.max_column>200 or worksheet.max_row>LIMIT_ROWS+1:raise ValueError('单表最多200列、50000行，请删除多余空白格式或分表')
            iterator=worksheet.iter_rows();first=next(iterator,[])
            if any(c.data_type=='f' for c in first):raise ValueError('表头不能使用公式')
            header=[str(c.value or '') for c in first]
            for line,row in enumerate(iterator,2):
                if all(c.value is None for c in row):continue
                rows.append({'line':line,'cells':[_cell(c.value,c.number_format,c.data_type) for c in row]})
        finally:workbook.close()
    else:raise ValueError('支持.csv、.tsv和.xlsx；旧版.xls请另存为.xlsx')
    header=[str(v).strip() for v in header]
    if not header or len(header)>200 or any(not h for h in header) or len(header)!=len(set(header)):raise ValueError('表头不能为空或重复，且最多200列')
    return {'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'raw_bytes':raw,'headers':header,'rows':rows,'sheet':sheet or '', 'sheets':sheets,'encoding':encoding,'row_count':len(rows)}


def suggest_columns(table,headers):
    if table not in TABLE_LABELS:raise ValueError('未知接入表')
    result={}
    for field in SCHEMA[table]:
        if field in {'source_id','label_source'}:continue
        matches=[h for h in headers if h==field or h in ALIASES.get(field,[])]
        if len(matches)==1:result[field]=matches[0]
    return result


def import_tables(entries,destination,metadata):
    destination=Path(destination).resolve()
    if not entries or (destination.exists() and any(destination.iterdir())):raise ValueError('先添加输入表，并选择新的空输出目录')
    required=('case_id','title','region','case_type','origin')
    if any(not str(metadata.get(k,'')).strip() for k in required) or metadata['origin'] not in {'real','synthetic'}:raise ValueError('请明确案件编号、标题、地区、案型与来源类型')
    if not re.fullmatch(r'[\w-]{1,64}',metadata['case_id']):raise ValueError('案件编号格式无效')
    destination.mkdir(parents=True,exist_ok=True);materials=destination/'_ingest_parent';materials.mkdir()
    tables={k:[] for k in SCHEMA};origins={};source_rows={};errors=[];changes=[];input_log=[];seen_ids={k:set() for k in SCHEMA};actor_seen=set();actor_references={};case_ids=set();case_metadata={}
    for entry in entries:
        table=entry.get('table');columns=entry.get('columns',{});defaults=entry.get('defaults',{})
        if entry.get('origin',metadata['origin'])!=metadata['origin']:raise ValueError('接入队列不能混合真实与合成来源，请分别接入')
        entry_meta={**metadata,**entry.get('case_metadata',{})}
        if table not in TABLE_LABELS or not set(columns).issubset(SCHEMA[table]) or not set(defaults).issubset(SCHEMA[table]):raise ValueError('接入表或列映射无效')
        if {'source_id','label_source'} & (set(columns)|set(defaults)):raise ValueError('来源编号由原件对应生成，不能覆盖')
        snapshot=read_table(entry['path'],entry.get('sheet') or None)
        if snapshot['sha256']!=entry.get('sha256'):raise ValueError('输入文件在预览后变化，请重新预览并确认')
        if any(v not in snapshot['headers'] for v in columns.values()):raise ValueError('映射列不存在')
        reference='_ingest_parent/'+snapshot['sha256'][:16]+Path(entry['path']).suffix.lower();target=destination/reference
        if not target.exists():target.write_bytes(snapshot['raw_bytes'])
        source_id='IMP_'+hashlib.sha256((snapshot['sha256']+snapshot['sheet']).encode()).hexdigest()[:12]
        input_log.append({'file':str(Path(entry['path']).name),'sha256':snapshot['sha256'],'sheet':snapshot['sheet'],'table':table,'columns':columns,'defaults':defaults,'encoding':snapshot['encoding']})
        positions={h:i for i,h in enumerate(snapshot['headers'])}
        for raw in snapshot['rows']:
            row={k:str(defaults.get(k,'')).strip() for k in SCHEMA[table]}
            try:
                for field,header in columns.items():
                    cell=raw['cells'][positions[header]]
                    if cell['kind']=='formula':raise ValueError('映射单元格含公式，请人工核对并另存为值')
                    if (field.endswith('_id') or field in {'from_id','to_id'}) and cell['kind']=='number':raise ValueError('编号/账户列为Excel数值，无法保证前导零或长编号精度，请用文本列或明确零填充格式')
                    row[field]=cell['value'].strip()
                cid=row.get('case_id') or entry_meta['case_id'];row['case_id']=cid
                if not re.fullmatch(r'[\w-]{1,64}',cid):raise ValueError('案件编号格式无效')
                if cid in case_metadata and any(case_metadata[cid][k]!=entry_meta[k] for k in ('title','region','case_type')):raise ValueError('同一案件的标题/地区/案型设置冲突，请统一后接入')
                case_metadata[cid]=entry_meta
                case_ids.add(cid);source_rows[(cid,source_id)]={'case_id':cid,'source_id':source_id,'title':Path(entry['path']).name+(' / '+snapshot['sheet'] if snapshot['sheet'] else ''),'reference':reference}
                if 'source_id' in row:row['source_id']=source_id
                if 'label_source' in row:row['label_source']=source_id
                for flag in ('verified','confirmed','role_verified'):
                    if flag in row and not row[flag]:row[flag]='false'
                identifier=IDS[table]
                if not row[identifier] and table not in {'actors','person_labels','case_labels'}:
                    row[identifier]='IMP_'+hashlib.sha256((source_id+table+str(raw['line'])).encode()).hexdigest()[:16];changes.append({'table':table,'input_line':raw['line'],'action':'生成可追踪记录编号，未生成交易内容'})
                if (cid,row[identifier]) in seen_ids[table]:raise ValueError('同表记录编号重复，先核对重复或冲突')
                seen_ids[table].add((cid,row[identifier]));tables[table].append(row)
                output_line=len(tables[table])+1;origins.setdefault(table,{})[row[identifier]+'|'+cid]={'line':raw['line'],'sha256':snapshot['sha256'],'sheet':snapshot['sheet'],'output_line':output_line}
                if table=='actors':actor_seen.add((cid,row['actor_id']))
                fields={'communications':['from_actor','to_actor'],'network_events':['actor_id'],'identity_links':['actor_id'],'relations':['from_actor','to_actor'],'shipments':['sender_id','receiver_id','carrier_id'],'person_labels':['actor_id']}.get(table,[])
                for field in fields:
                    if row[field]:actor_references.setdefault((cid,row[field]),{'source_id':source_id,'line':raw['line'],'sha256':snapshot['sha256'],'sheet':snapshot['sheet']})
            except ValueError as exc:errors.append({'table':table,'input_line':raw['line'],'sheet':snapshot['sheet'],'reason':str(exc)})
    for (cid,actor),original in actor_references.items():
        if (cid,actor) not in actor_seen:
            tables['actors'].append({'case_id':cid,'actor_id':actor,'alias':actor,'actor_type':'unspecified','role':'','role_verified':'false','source_id':original['source_id']});changes.append({'case_id':cid,'actor_id':actor,'action':'仅登记输入中已出现的主体编号，姓名/类型/角色未知'})
            origins.setdefault('actors',{})[actor+'|'+cid]={'line':original['line'],'sha256':original['sha256'],'sheet':original['sheet'],'derived_registration':True}
    for cid in sorted(case_ids):tables['cases'].append({'case_id':cid,'title':case_metadata[cid]['title']+(' / '+cid if len(case_ids)>1 else ''),'region':case_metadata[cid]['region'],'case_type':case_metadata[cid]['case_type']})
    tables['sources']=list(source_rows.values())
    for table,fields in SCHEMA.items():
        with (destination/(table+'.csv')).open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(tables[table])
    manifest={'schema_version':2,'timezone':'Asia/Shanghai','origin':metadata['origin'],'title':metadata['title'],'intake':'intake_report.json','label_definition':{'task_id':'','positive':'','negative':''},'role_task':{'task_id':'','roles':[],'levels':[],'compatible_pairs':[]}}
    report={'version':1,'created_at':datetime.now(TZ).isoformat(),'origin':metadata['origin'],'valid':not errors,'inputs':input_log,'origins':origins,'changes':changes,'errors':errors,'output_sha256':{t+'.csv':hashlib.sha256((destination/(t+'.csv')).read_bytes()).hexdigest() for t in SCHEMA},'interpretation':'原件与逐行映射保留；新增主体仅沿用输入编号，未推断身份、角色或交易。'}
    atomic_json(destination/'manifest.json',manifest);atomic_json(destination/'intake_report.json',report)
    if not errors:
        try:load_dataset(destination)
        except ValueError as exc:report['valid']=False;report['errors'].append({'reason':str(exc)});atomic_json(destination/'intake_report.json',report)
    return {'output':str(destination),'valid':report['valid'],'errors':report['errors'],'counts':{k:len(v) for k,v in tables.items()},'report':str(destination/'intake_report.json')}
