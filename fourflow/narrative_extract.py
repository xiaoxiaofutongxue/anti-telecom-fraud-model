"""从文字提取可复查的时间/金额/资产提及，不生成交易或身份事实。"""
import calendar,hashlib,ipaddress,re
from datetime import date,datetime,timezone
from decimal import Decimal,InvalidOperation,localcontext
from .narrative_fields import field_view,canonical_header,currency_code

FLOW_NAMES={'funds':'资金流','information':'信息流','people':'人员流','industry':'产业流'}


def _scaled_number(raw,scale):
    value=Decimal(raw.replace(',',''))
    with localcontext() as context:
        context.prec=max(32,len(value.as_tuple().digits)+12)
        return value*{None:Decimal(1),'千':Decimal(1000),'万':Decimal(10000),'亿':Decimal(100000000)}[scale]


def time_hint(raw):
    raw=str(raw or '').strip();out={'raw':raw,'start':None,'end':None,'precision':'unknown','open_end':False,'approximate':bool(re.search(r'约|前后|左右|大约|至少',raw))}
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?',raw):
        try:
            offset=re.search(r'[+-](\d{2}):(\d{2})$',raw)
            if offset and (int(offset[1])>23 or int(offset[2])>59):raise ValueError('时区偏移无效')
            value=datetime.fromisoformat(raw.replace('Z','+00:00'));out.update(start=value.date().isoformat(),end=value.date().isoformat(),precision='time',timezone_known=value.tzinfo is not None,instant=value.astimezone(timezone.utc).isoformat() if value.tzinfo is not None else None)
        except (ValueError,OverflowError):out.update(precision='invalid_time')
        out['scope']='保留原文时刻；明确时区才换算UTC，未声明时区时不作跨时区或分钟窗口比较'
        return out
    pattern=r'(?<![\dA-Za-z])((?:19|20)\d{2})(?:[年.\-/](\d{1,2})(?:[月.\-/](\d{1,2})日?)?)?(?![\dA-Za-z])'
    tokens=[];last_end=0;bounds=[]
    for m in re.finditer(pattern,raw):
        year=int(m[1]);month=int(m[2]) if m[2] else None;day=int(m[3]) if m[3] else None
        try:
            start=date(year,month or 1,day or 1);end=date(year,month or 12,day or (calendar.monthrange(year,month)[1] if month else 31))
        except ValueError:continue
        tokens.append((start,end,'day' if day else 'month' if month else 'year'))
        bounds.append(m.span())
        last_end=m.end()
    if not tokens:return out
    # 多个完整年月只表示原文可见区间，不补齐省略的日期或交易时刻。
    out.update(start=tokens[0][0].isoformat(),end=tokens[-1][1].isoformat(),precision=tokens[0][2] if len(tokens)==1 else 'range')
    if len(tokens)>1 and not (len(tokens)==2 and re.fullmatch(r'\s*[-—–~至到]\s*(?:至少|约)?\s*',raw[bounds[0][1]:bounds[1][0]])):out.update(end=None,precision='multiple_dates')
    if len(tokens)==1 and re.search(r'\d{4}\.\d{1,2}-\d{1,2}(?!\d)',raw):out.update(start=date(tokens[0][0].year,tokens[0][0].month,1).isoformat(),end=None,precision='partial_range',ambiguity='混合分隔符可能表示月区间，不按精确日使用')
    if len(tokens)==1 and re.match(r'^\s*[-—–~至]\s*\d{1,2}(?!\d)',raw[last_end:]):out.update(end=None,precision='partial_range')
    if len(tokens)==1 and re.match(r'^\s*[/、]\s*\d{1,2}(?!\d)',raw[last_end:]):out.update(end=None,precision='partial_range')
    if re.search(r'持续|至今|起|至少|以后|之后',raw) and len(tokens)==1:out.update(end=None,open_end=True)
    if tokens and (re.search(r'(?:至少|\+)\s*$',raw) or re.search(r'[-—–至]\s*至少',raw) or raw.rstrip().endswith('+')):out.update(end=None,open_end=True)
    if len(tokens)>1 and out['end'] and out['end']<out['start']:out.update(start=None,end=None,precision='invalid_order')
    out['scope']='日期边界来自文字精度，仅用于资料排序，不是逐笔交易时间；原文时区未推定'
    return out


def amount_mentions(text,header='',kind='',context=''):
    text=str(text or '');out=[]
    unit_pattern=r'比特币|以太币|美元|人民币|USDT|USDC|BTC|ETH|CNY|USD|EUR|HKD|GBP|JPY|SGD|AUD|元'
    pattern=rf'(?<![A-Za-z0-9_.,])(?P<qual>约|逾|超|超过|至少|大约|近)?\s*(?P<n>-?\d[\d,]*(?:\.\d+)?)\s*(?P<scale>亿|万|千)?\s*(?:枚)?\s*(?P<currency>{unit_pattern})(?![A-Za-z])(?P<plus>\+)?'
    header_currency=re.search(r'[（(](BTC|ETH|USDT|USDC|USD|CNY|EUR|HKD|GBP|JPY|SGD|AUD|美元|人民币|比特币|以太币)[）)]',header,re.I)
    matches=list(re.finditer(pattern,text,re.I))
    # Shared suffixes such as 235,000+20,000+150,000美元 retain separate source spans.
    number=r'-?\d[\d,]*(?:\.\d+)?';term=rf'(?:约|逾|超|超过|至少|大约|近)?\s*{number}\s*(?:亿|万|千)?'
    compound=list(re.finditer(rf'(?<![A-Za-z0-9_.,])(?P<body>{term}(?:\s*[+/／]\s*{term})+)\s*(?P<currency>{unit_pattern})(?![A-Za-z])',text,re.I))
    covered=[]
    for m in re.finditer(rf'(?<![A-Za-z0-9_.,])(?P<low>{number})\s*(?P<ls>亿|万|千)?\s*[-—～~至到]\s*(?P<high>{number})\s*(?P<hs>亿|万|千)?\s*(?P<currency>{unit_pattern})(?![A-Za-z])',text,re.I):
        currency={'美元':'USD','人民币':'CNY','比特币':'BTC','以太币':'ETH','元':'未明确币种(元)'}.get(m['currency'],m['currency'].upper())
        low=_scaled_number(m['low'],m['ls'] or m['hs']);high=_scaled_number(m['high'],m['hs'] or m['ls'])
        out.append({'raw':m[0],'value':None,'lower':format(low,'f'),'upper':format(high,'f'),'currency':currency,'qualifier':'原文区间，非单笔金额','category':'金额区间陈述','overlap_warning':False,'transaction':False,'span':list(m.span()),'unit_source':'原文区间共享币种/量级'})
        covered.append(m.span())
    def category(value,currency):
        if re.search(r'行业|全球年|年收益|GDP|IC3报损',value):return '行业/背景统计'
        if re.search(r'价格|费用|报价|开支|租金|预付|账单',value+header):return '费用/报价陈述'
        if re.search(r'合计|汇总|总额|总述',value+header) or ('合计' in context and '数量' in header):return '汇总口径'
        if re.search(r'余额|持有|自持|沉淀',value) or ('数量' in header and currency in {'BTC','ETH','USDT','USDC'}) or (currency=='BTC' and re.search(r'私钥|自持|资产沉淀',context)):return '资产数量/余额'
        return '金额陈述'
    def append(raw,n,scale,currency,start,end,unit_source,qual=''):
        currency={'美元':'USD','人民币':'CNY','比特币':'BTC','以太币':'ETH'}.get(currency.upper(),currency.upper()) if currency else '未明确币种'
        if currency=='元':currency='未明确币种(元)'
        value=_scaled_number(n,scale)
        out.append({'raw':text[start:end],'value':format(value,'f'),'currency':currency,'qualifier':qual or '原文未注明约数','category':category(text,currency),'overlap_warning':bool(re.search(r'重叠|重复|含部分',text)),'transaction':False,'span':[start,end],'unit_source':unit_source})
    for m in compound:
        if any(start<=m.start()<end for start,end in covered):continue
        covered.append(m.span())
        for n in re.finditer(rf'(?P<qual>约|逾|超|超过|至少|大约|近)?\s*(?P<n>{number})\s*(?P<scale>亿|万|千)?',m['body']):
            start=m.start('body')+n.start();end=m.start('body')+n.end();append(text[start:end].strip(),n['n'],n['scale'],m['currency'],start,end,'同一表达式明示的共享币种',n['qual'] or '')
    for m in matches:
        if any(start<=m.start()<end for start,end in covered):continue
        covered.append(m.span());append(m[0].strip(),m['n'],m['scale'],m['currency'],m.start(),m.end(),'原文明示',m['qual'] or ('至少' if m['plus'] else ''))
    if not matches and not compound and header_currency and re.fullmatch(rf'\s*{number}(?:\s*[/／+]\s*{number})*\s*',text):
        for m in re.finditer(number,text):append(m[0],m[0],None,header_currency[1],m.start(),m.end(),'单元格表头')
    if re.search(r'金额|关联资金',header):
        for m in re.finditer(rf'(?<![A-Za-z0-9_.,])(?P<qual>约|逾|超|至少)?\s*(?P<n>{number})\s*(?P<scale>亿|万|千)',text):
            if any(start<=m.start()<end or m.start()<=start<m.end() for start,end in covered):continue
            append(m[0].strip(),m['n'],m['scale'],None,m.start(),m.end(),'币种未披露',m['qual'] or '')
    # Mixed cells can contain both an explicit number and a different, unquantified amount.
    for m in re.finditer(rf'数[十百千万亿]+(?:枚)?\s*(?P<currency>{unit_pattern})',text,re.I):
        currency={'美元':'USD','人民币':'CNY','比特币':'BTC','以太币':'ETH','元':'未明确币种(元)'}.get(m['currency'],m['currency'].upper())
        out.append({'raw':m[0],'value':None,'currency':currency,'qualifier':'模糊/未披露','category':'未量化陈述','overlap_warning':bool(re.search(r'重叠|重复',text)),'transaction':False,'span':list(m.span()),'unit_source':'币种明示，数值未量化'})
    # Keep unquantified language verbatim, rather than assigning a numeric value.
    if not out and re.search(r'数[十百千万亿]|未披露|未明确|大规模|等额',text) and re.search(unit_pattern+'|金额|规模',text+header,re.I):out.append({'raw':text,'value':None,'currency':None,'qualifier':'模糊/未披露','category':'未量化陈述','overlap_warning':False,'transaction':False,'span':[0,len(text)],'unit_source':'原文未量化'})
    return out


def wallet_validation(value):
    """Base58Check per Bitcoin Core; validation does not establish ownership or activity."""
    if value.startswith(('1','3')):
        try:
            n=0
            for c in value:n=n*58+'123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'.index(c)
            data=b'\0'*(len(value)-len(value.lstrip('1')))+n.to_bytes((n.bit_length()+7)//8,'big')
            valid=len(data)==25 and data[0] in {0,5} and hashlib.sha256(hashlib.sha256(data[:-4]).digest()).digest()[:4]==data[-4:]
        except ValueError:valid=False
        return {'checksum_status':'valid' if valid else 'invalid','validation_scope':'BTC Base58Check，仅校验编码；未查询链上或核实归属'}
    return {'checksum_status':'not_checked','validation_scope':'仅保留文本形状；本地未验证此格式校验码或链上状态'}


def asset_mentions(text):
    text=str(text or '');found=[]
    specs=[('wallet',r'(?<![A-Za-z0-9])(?:0x[a-fA-F0-9]{40}|bc1[a-zA-HJ-NP-Z0-9]{25,87}|[13][A-Za-z0-9]{25,34})(?![A-Za-z0-9])'),('domain',r'(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})\.)+(?:com|net|org|gov|io|app|cn|xyz|vip|top|me|info|biz|co|site|online|cc)(?![A-Za-z0-9_.-])')]
    for kind,pattern in specs:
        for m in re.finditer(pattern,text,re.I):found.append({'kind':kind,'value':m[0].lower() if kind=='domain' or m[0].startswith('0x') else m[0],'raw':m[0],'complete':True,'span':list(m.span()),**(wallet_validation(m[0]) if kind=='wallet' else {})})
    for m in re.finditer(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',text):
        try:found.append({'kind':'ip','value':str(ipaddress.ip_address(m[0])),'raw':m[0],'complete':True})
        except ValueError:pass
    for m in re.finditer(r'(?<![A-Za-z0-9:.])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:.]*(?![A-Za-z0-9:.])',text):
        try:
            address=ipaddress.ip_address(m[0])
            if address.version==6:found.append({'kind':'ip','value':str(address),'raw':m[0],'complete':True,'span':list(m.span())})
        except ValueError:pass
    for m in re.finditer(r'(?<![A-Za-z0-9])(?:0x[a-fA-F0-9]{4,39}|bc1[a-zA-Z0-9]{2,24})(?![A-Za-z0-9])',text):found.append({'kind':'wallet_prefix','value':m[0],'raw':m[0],'complete':False})
    for m in re.finditer(r'\*{2,}[A-Za-z0-9]{3,40}',text):found.append({'kind':'wallet_fragment','value':m[0],'raw':m[0],'complete':False,'fragment_type':'masked_suffix','span':list(m.span())})
    for m in re.finditer(r'(?P<lat>\d{1,2})°(?P<lm>\d{1,2})[′\'](?P<ls>\d{1,2}(?:\.\d+)?)[″\"]?N\s+(?P<lon>\d{1,3})°(?P<om>\d{1,2})[′\'](?P<os>\d{1,2}(?:\.\d+)?)[″\"]?E',text,re.I):
        lat=float(m['lat'])+float(m['lm'])/60+float(m['ls'])/3600;lon=float(m['lon'])+float(m['om'])/60+float(m['os'])/3600
        if lat<=90 and lon<=180 and all(float(m[k])<60 for k in ('lm','ls','om','os')):found.append({'kind':'coordinate','value':f'{lat:.6f},{lon:.6f}','raw':m[0],'complete':True,'span':list(m.span()),'normalization_precision':6})
    for m in re.finditer(r'(?<![\d.])(?P<lat>\d{1,2}\.\d+)\s*(?P<ns>[NS])\s*[,，;； ]+\s*(?P<lon>\d{1,3}\.\d+)\s*(?P<ew>[EW])(?![A-Za-z])',text,re.I):
        lat=float(m['lat'])*(-1 if m['ns'].upper()=='S' else 1);lon=float(m['lon'])*(-1 if m['ew'].upper()=='W' else 1)
        if abs(lat)<=90 and abs(lon)<=180:found.append({'kind':'coordinate','value':f'{lat:.6f},{lon:.6f}','raw':m[0],'complete':True,'span':list(m.span()),'normalization_precision':6})
    for m in re.finditer(r'(?<!\d)(-?\d{1,2}\.\d{4,})\s*[,，]\s*(-?\d{1,3}\.\d{4,})(?!\d)',text):
        if abs(float(m[1]))<=90 and abs(float(m[2]))<=180:found.append({'kind':'coordinate','value':m[1]+','+m[2],'raw':m[0],'complete':True})
    unique={}
    for item in found:unique.setdefault((item['kind'],item['value']),item)
    return list(unique.values())


def flows_for(sheet,text,fields=None):
    fields=field_view(fields or {})['values']
    declared=' '.join(v for k,v in (fields or {}).items() if k in {'涉及流向','关联流向'})
    if '四流' in declared:return list(FLOW_NAMES)
    explicit=[key for key,title in FLOW_NAMES.items() if title in declared]
    if explicit:return explicit
    if '产业回流' in sheet:return ['funds','industry']
    if re.search(r'涉及流向[：:]\s*四流',text):return list(FLOW_NAMES)
    for key,title in FLOW_NAMES.items():
        if title in sheet:return [key]
    inferred=[key for key,title in FLOW_NAMES.items() if title in text];fields=fields or {}
    # These are source-content tags, not a conclusion that a payment actually occurred.
    payer=any(fields.get(k) for k in ('付款人/日期','付款方/日期','付款人','付款方'))
    recipient=any(fields.get(k) for k in ('收款方','收款人'))
    if payer and recipient and 'funds' not in inferred:inferred.append('funds')
    if any(fields.get(k) for k in ('地址','地址/节点','虚拟货币地址')) and any(fields.get(k) for k in ('币种','币种/网络','关联资金','数量(BTC)')) and 'funds' not in inferred:inferred.append('funds')
    if fields.get('域名') and 'information' not in inferred:inferred.append('information')
    if any(fields.get(k) for k in ('IP地址','发送主体','接收主体','通信摘要','访问账户')) and 'information' not in inferred:inferred.append('information')
    if fields.get('付款账户') and fields.get('收款账户') and 'funds' not in inferred:inferred.append('funds')
    if any(fields.get(k) for k in ('人员编号','人员/角色','空间流动')) and 'people' not in inferred:inferred.append('people')
    if fields.get('订单编号') and any(fields.get(k) for k in ('物品名称','运输编号','产业类型')) and 'industry' not in inferred:inferred.append('industry')
    return inferred or ['unclassified']


def enrich(record):
    fields=record['fields'];text=record['text'];kind=record['kind']
    view=field_view(fields);normalized=view['values']
    time_key=next((k for k in ('时间','日期','发生时间','交易时间','付款人/日期','注册时间','处置/时间') if fields.get(k)),None)
    time=str(fields[time_key]) if time_key else normalized.get('时间','')
    if not time and kind in {'paragraph','note'}:time=text
    citation=next((str(v) for k,v in fields.items() if any(word in k for word in ('段落','证据来源','来源文书'))),'');citation_scope='row_field' if citation else 'unknown'
    if not citation:
        citation='、'.join(dict.fromkeys(re.findall(r'(?:[①②③]+\s*)?¶\s*\d+[a-z]?(?:[-—–]\d+[a-z]?)?',text)))
        if citation:citation_scope='row_inline'
    if not citation:
        citation='、'.join(dict.fromkeys(re.findall(r'(?:[①②③]+\s*)?¶\s*\d+[a-z]?(?:[-—–]\d+[a-z]?)?',record.get('source_context',{}).get('text',''))))
        if citation:citation_scope='sheet_context'
    record.update(time=time_hint(time),time_field=time_key,flows=flows_for(record['sheet'],text,fields),amounts=[],assets=[],source_citation=citation,source_citation_scope=citation_scope,issues=[])
    record['field_mapping']={'origins':view['origins'],'conflicts':view['conflicts']}
    if not time and normalized.get('时间'):record.update(time=time_hint(normalized['时间']),time_field=view['origins']['时间'][0])
    record['issues'].extend('字段冲突：'+c['field']+'（'+c['reason']+'）' for c in view['conflicts'])
    if record.get('formula_present'):
        record.update(time=time_hint(''),statement_status='含未计算公式的原行',verified=False);return record
    for header,value in fields.items():
        # Scan all visible fields for explicit monetary units; never treat bare model numbers as amounts.
        scan_header=canonical_header(header)
        code=currency_code(normalized.get('币种'))
        if canonical_header(header)=='金额' and code in {'USD','CNY','EUR','HKD','GBP','JPY','SGD','AUD','USDT','USDC','BTC','ETH'}:scan_header='金额('+code+')'
        record['amounts'].extend({**m,'field':header,'source_column':record.get('column_map',{}).get(header)} for m in amount_mentions(value,scan_header,kind,text))
        record['assets'].extend({**a,'field':header,'source_column':record.get('column_map',{}).get(header)} for a in asset_mentions(value))
    record['assets']=list({(a['kind'],a['value'],a['field']):a for a in record['assets']}.values())
    if record['kind'] not in {'note','overview'} and not record['source_citation']:record['issues'].append('缺少原始文书段落定位')
    if citation_scope=='sheet_context':record['issues'].append('文书定位来自工作表来源说明，非逐行直接引注')
    if record['time']['precision']=='unknown':record['issues'].append('时间未明确，未补造日期')
    elif record['time']['precision']=='invalid_time':record['issues'].append('原文时刻格式无效，未生成排序时刻')
    elif record['time']['precision']=='time' and not record['time'].get('timezone_known'):record['issues'].append('原文时刻缺少时区，未进行跨时区或分钟窗口比较')
    elif record['time']['precision'] not in {'day','time'} or record['time']['open_end'] or record['time']['approximate']:record['issues'].append('时间为区间/粗粒度或有约数限定，不作逐笔时间')
    if any(a['category'] in {'汇总口径','行业/背景统计','资产数量/余额'} for a in record['amounts']):record['issues'].append('金额为汇总/背景/存量，不计入交易总额')
    if any(a['overlap_warning'] for a in record['amounts']):record['issues'].append('原文提示金额口径重叠，需核对')
    if any(not a['complete'] for a in record['assets']):record['issues'].append('钱包仅有前缀/掩码片段，不与完整地址自动绑定')
    if any(a.get('checksum_status')=='invalid' for a in record['assets']):record['issues'].append('钱包地址未通过编码校验，需逐字符核对原始文书；保留原文不自动纠错')
    if any(re.search(r'\d+(?:\.\d+)?(?:[MmKk](?![A-Za-z])|[eE][+-]?\d+)',v) for v in fields.values()):record['issues'].append('数值含英文量级/科学计数写法，需核对原文单位；不从相邻币种自动推定')
    record['statement_status']='表内分析' if record['kind'] in {'fund_summary','industry','return','matrix','person'} or '核心结论' in text else '来源文字陈述'
    record['verified']=False
    return record
