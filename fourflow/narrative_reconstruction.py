"""Reconstruct explicit source statements; incomplete observations never become verified facts."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import hashlib,re
import networkx as nx
from .narrative_fields import field_view,canonical_header
from .analytics import exact_sum

MARKERS='①②③④⑤⑥⑦⑧⑨⑩'
ANONYMOUS=re.compile(r'(?<![A-Za-z0-9_-])(?:TV|FV|CC|F-Victim|Domain|Company|Worker|INDIVIDUAL|Business Partner|USFI)[ -]?\d+|WEBSITE\s*\d+|壳公司\s*\d+',re.I)


def source_scopes(rows):
    """Only an explicit same-document statement permits collapsing document markers."""
    parent={m:m for m in MARKERS};declarations=[]
    def find(a):
        while parent[a]!=a:a=parent[a]
        return a
    for r in rows:
        for m in re.finditer(r'文书\s*(['+MARKERS+r'、及和]+)[^。\n]{0,35}?(?:为同一份|属同一份|同一来源)',r['text']):
            if re.search(r'并非|不是|并不|未证实|尚不能',m[0]):continue
            symbols=[s for s in m[1] if s in MARKERS]
            if not symbols:continue
            for symbol in symbols[1:]:parent[find(symbol)]=find(symbols[0])
            declarations.append({'record_id':r['id'],'statement':m[0],'scope':'来源材料自述的同源关系，未外部认证'})
    groups={m:''.join(s for s in MARKERS if find(s)==find(m)) for m in MARKERS}
    known_units={groups[s] for r in rows for s in re.findall('['+MARKERS+']',r.get('source_citation',''))};lookup={}
    for r in rows:
        markers=set(re.findall('['+MARKERS+']',r.get('source_citation','')))
        units=sorted({groups[s] for s in markers});ns='doc_'+units[0] if len(units)==1 else 'mixed_'+''.join(units) if units else 'unmarked_'+r['id'] if len(known_units)>1 else 'unmarked'
        lookup[r['id']]={'namespace':ns,'units':units,'ambiguous':len(units)>1,'declared':bool(markers)}
    return {'rows':lookup,'same_source_declarations':declarations,'scope':'文书编号是来源命名空间；两份同源材料不算独立印证'}


def dates_in_field(raw):
    """Read explicit date lists; inheritance stays recorded and is never a timezone assumption."""
    raw=str(raw);full=re.compile(r'(?<![\dA-Za-z])((?:19|20)\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?(?![\d.])')
    first=full.search(raw)
    if not first:return {'items':[],'range':False,'raw':raw,'reason':'无完整日日期，保留原文时间'}
    def item(y,m,d,text,inherited):
        try:value=date(int(y),int(m),int(d)).isoformat()
        except ValueError:return None
        return {'date':value,'raw':text,'inherited_parts':inherited,'timezone':None}
    current=item(*first.groups(),first[0],[])
    if not current:return {'items':[],'range':False,'raw':raw,'reason':'日期无效'}
    items=[current];year,month,day=map(int,first.groups());cursor=first.end();is_range=False
    while cursor<len(raw):
        separator=re.match(r'\s*([/、,，;；—–~～至到-])\s*',raw[cursor:])
        if not separator:break
        is_range=is_range or separator[1] in '—–~～至到-';cursor+=separator.end();segment=raw[cursor:];m=full.match(segment)
        if m:y,mo,da=map(int,m.groups());inherited=[]
        else:
            m=re.match(r'(\d{1,2})(?:[月.](\d{1,2})日?)?(?![\dA-Za-z.月])',segment)
            if not m:return {'items':items,'range':is_range,'raw':raw,'reason':'日期列表尚有无法解析的项目'}
            if m[2]:y,mo,da=year,int(m[1]),int(m[2]);inherited=['year']
            else:y,mo,da=year,month,int(m[1]);inherited=['year','month']
        parsed=item(y,mo,da,m[0],inherited)
        if not parsed:return {'items':items,'range':is_range,'raw':raw,'reason':'省略日期部分无法有效还原'}
        if parsed['date']<items[-1]['date'] and inherited:
            return {'items':items,'range':True,'raw':raw,'reason':'省略年份且可能跨年，未自动补年'}
        items.append(parsed);year,month,day=y,mo,da;cursor+=m.end()
    reason='日期含约数或不确定限定' if re.search(r'约|前后|左右|可能|不详',raw) else ''
    if len(re.findall(r'(?:19|20)\d{2}[年./-]\d{1,2}[月./-]\d{1,2}',raw))>len(items):reason='日期列表有未解析的完整日期'
    return {'items':items,'range':is_range,'raw':raw,'reason':reason}


def _id(prefix,*parts):return prefix+hashlib.sha256('|'.join(map(str,parts)).encode()).hexdigest()[:18]


def statement_events(rows,scopes):
    events=[]
    for r in rows:
        view=field_view(r['fields']);f=view['values'];payer_key=next((k for k in ('付款人/日期','付款人','付款账户') if f.get(k)),None)
        target_key=next((k for k in ('收款方','收款账户') if f.get(k)),None);kind=None
        if payer_key and target_key:
            money_keys=[k for k in r['fields'] if (canonical_header(k)=='金额' or re.match(r'^金额[（(]',canonical_header(k))) and canonical_header(k) in f];kind='wire_statement' if f.get('付款机构') else 'account_payment_statement';date_raw=f.get('时间') or f[payer_key];sender=re.split(r'[,，]\s*(?=(?:19|20)\d{2})',f[payer_key])[0];target=f[target_key]
        elif f.get('地址/节点') or f.get('地址'):
            money_keys=[k for k in f if k=='关联资金'];text=f.get('关联资金','');m=re.match(r'\s*((?:FV|F-Victim)[ -]?\d+)\s*(?:转币|转账|汇款|支付)',text,re.I)
            if not m:continue
            kind='crypto_valuation_statement';sender=m[1];target=f.get('地址/节点') or f['地址'];date_raw=f.get('时间','')
        if not kind:continue
        amounts=[a for a in r['amounts'] if a['field'] in money_keys and a['value'] is not None and a['currency'] not in {None,'未明确币种','未明确币种(元)'}]
        if not amounts:continue
        parsed=dates_in_field(date_raw);ordered=len(amounts)==len(parsed['items']) and not parsed['range'] and not parsed['reason']
        for i,a in enumerate(amounts):
            aligned=parsed['items'][i] if ordered else None
            start=aligned['date'] if aligned else parsed['items'][0]['date'] if parsed['items'] else None
            end=aligned['date'] if aligned else parsed['items'][-1]['date'] if parsed['range'] and len(parsed['items'])>1 and not parsed['reason'] else None
            scope=scopes[r['id']];events.append({'event_id':_id('S_',r['id'],a['field'],a.get('span'),i),'record_id':r['id'],'locator':r['locator'],'case_id':r['case_id'],'source_namespace':scope['namespace'],'source_namespace_ambiguous':scope['ambiguous'],'source_citation':r['source_citation'],'event_kind':kind,'sender_ref':sender,'recipient_ref':target,'amount':a['value'],'amount_qualifier':a.get('qualifier',''),'valuation_currency':a['currency'],'asset_currency':f.get('币种/网络','未披露') if kind.startswith('crypto') else a['currency'],'amount_semantics':'原文法币等值，非链上币数' if kind.startswith('crypto') else '原表列示支付金额','amount_raw':a['raw'],'amount_field':a['field'],'source_column':a.get('source_column'),'source_span':a.get('span'),'date_raw':date_raw,'date_start':start,'date_end':end,'date_precision':'day' if aligned else 'range' if end else 'unresolved','date_inherited_parts':aligned['inherited_parts'] if aligned else [],'alignment':'并列列表按顺序对应，需核验' if ordered and len(amounts)>1 else '原行单项' if ordered else '未将区间或数量不等的列表分配到各笔','date_issue':parsed['reason'],'verified':False,'eligible_for_supervised_training':False})
    return events


def amount_envelope(events):
    """Exact Decimal arithmetic over source items; uncertainty is explicit, never a legal loss estimate."""
    groups=defaultdict(list)
    for event in events:groups[(event['source_namespace'],event['valuation_currency'])].append(event)
    out=[]
    for (namespace,currency),rows in sorted(groups.items()):
        # Equal economic-looking tuples remain possible duplicates, not silently removed transactions.
        buckets=defaultdict(list)
        for r in rows:buckets[(r['sender_ref'],r['recipient_ref'],r['date_raw'] if r['date_precision']!='day' else '',r['date_start'],r['date_end'],Decimal(r['amount']),r['event_kind'])].append(r)
        total=exact_sum(Decimal(r['amount']) for r in rows);low=exact_sum(Decimal(v[0]['amount']) for v in buckets.values())
        blocked=any(Decimal(r['amount'])<0 or r.get('amount_qualifier','') not in {'','原文未注明约数'} or r.get('source_namespace_ambiguous') for r in rows)
        subtotals={kind:format(exact_sum(Decimal(r['amount']) for r in rows if r['event_kind']==kind),'f') for kind in sorted({r['event_kind'] for r in rows})}
        out.append({'source_namespace':namespace,'currency':currency,'listed_item_count':len(rows),'record_ids':list(dict.fromkeys(r['record_id'] for r in rows)),'event_ids':[r['event_id'] for r in rows],'listed_nominal_total':format(total,'f'),'nominal_subtotals':subtotals,'possible_duplicate_groups':[[r['event_id'] for r in v] for v in buckets.values() if len(v)>1],'conditional_distinct_transfer_interval':None if blocked else {'lower':format(low,'f'),'upper':format(total,'f'),'assumptions':['每个不同的源文付款元组代表不同支付；相同元组可能重复','仅计本组明细，且法币等值陈述成立；没有汇率重估','这是转移名义金额，未处理返还、循环资金或本金去重']},'interval_unavailable_reason':'负数、约数或混合来源无法给出此条件区间' if blocked else '', 'whole_case_loss_interval':None,'whole_case_upper_bound':None,'scope':'源文列示项目的精确算术与条件区间；不是全案损失/置信区间','verified':False})
    return out


def hierarchy_claims(rows,entities,scopes):
    actors=[e for e in entities if e['kind']=='person_mention'];edges=[];unresolved=[]
    def resolve(alias,namespace):
        matches=[a for a in actors if alias.casefold() in {v.casefold() for v in a.get('aliases',[a['label']])} and (not a.get('anonymous_reference') or a.get('namespace')==namespace)]
        return matches[0] if len(matches)==1 else None
    aliases=sorted({alias for a in actors for alias in a.get('aliases',[a['label']]) if len(alias)>=2},key=lambda v:(-len(v),v))
    if not aliases:return {'nodes':actors,'edges':[],'partial_levels':{},'cycles':[],'unresolved':[],'scope':'未找到可定位的显式汇报/指令陈述'}
    def safe_token(alias):
        value=re.escape(alias)
        return r'(?<![A-Za-z0-9_-])'+value+r'(?![A-Za-z0-9_-])' if re.search('[A-Za-z0-9]',alias) else value
    token='(?:'+'|'.join(safe_token(a) for a in aliases)+')';found=[]
    for r in rows:
        f=field_view(r['fields'])['values'];namespace=scopes[r['id']]['namespace'];subject=f.get('人员/角色');duties=f.get('行为/职责','')
        seed=[a for a in actors if r['id'] in a['record_ids'] and subject and a['label']==re.split('[（(]',subject)[0].strip()]
        if len(seed)==1:
            me=seed[0]
            for m in re.finditer(r'(?:直报|直属于|向)\s*('+token+r')(?:汇报|报告)?',duties,re.I):
                if m[0].startswith('向') and not re.search('汇报|报告',m[0]):continue
                found.append((m[1],me['label'],r,m[0],'汇报关系'))
            for m in re.finditer('('+token+r')直属(?:下属)?',duties,re.I):found.append((m[1],me['label'],r,m[0],'直属关系'))
        for key,value in f.items():
            if key in {'人员/角色','时间','证据来源','起诉书段落','宣誓书段落'}:continue
            for m in re.finditer('('+token+r')\s*(?:指示|命令|指使|领导|监督)\s*('+token+')',value,re.I):found.append((m[1],m[2],r,m[0],'指令陈述'))
    seen=set()
    for upper,lower,r,phrase,relation in found:
        namespace=scopes[r['id']]['namespace'];a,b=resolve(upper,namespace),resolve(lower,namespace)
        if scopes[r['id']]['ambiguous']:a=b=None
        if not a or not b or a['id']==b['id']:
            unresolved.append({'record_id':r['id'],'phrase':phrase,'reason':'称谓不唯一、来源混合或对象无法定位'});continue
        if re.search(r'(?:未证实|否认|没有|并未|并非|不|未).{0,8}'+re.escape(phrase),r['text']):
            unresolved.append({'record_id':r['id'],'phrase':phrase,'reason':'存在否定/未证实限定'});continue
        key=(a['id'],b['id'],r['id'],relation)
        if key in seen:continue
        seen.add(key);edges.append({'claim_id':_id('H_',*key),'from_actor':a['id'],'to_actor':b['id'],'from_label':a['label'],'to_label':b['label'],'record_id':r['id'],'locator':r['locator'],'source_namespace':namespace,'source_citation':r['source_citation'],'relation':relation,'phrase':phrase,'verified':False,'scope':'原文方向候选，未认定现实控制关系'})
    graph=nx.DiGraph();graph.add_edges_from((e['from_actor'],e['to_actor']) for e in edges)
    cycles=[sorted(c) for c in nx.strongly_connected_components(graph) if len(c)>1];levels={}
    if not cycles:
        for node in nx.topological_sort(graph):levels[node]=max((levels[p]+1 for p in graph.predecessors(node)),default=0)
    return {'nodes':actors,'edges':edges,'partial_levels':levels,'cycles':cycles,'unresolved':unresolved,'scope':'仅在显式关系连通部分给出相对层次；未连接人员不推定为首脑，循环时不强制排序'}


def reconstruct(case,rows,entities,excluded):
    active=[r for r in rows if r['id'] not in excluded and not r.get('formula_present')];scoped=source_scopes(active);events=statement_events(active,scoped['rows']);hierarchy=hierarchy_claims(active,entities,scoped['rows']);money=amount_envelope(events)
    graph={'nodes':[],'edges':[],'scope':'源表明细的主体/收款引用网络，账户号及链上资产数量可能未披露'};seen=set()
    for event in events:
        ids=[]
        for kind,label in [('sender_ref',event['sender_ref']),('recipient_ref',event['recipient_ref'])]:
            nid=_id('F_',case['case_id'],event['source_namespace'],label);ids.append(nid)
            if nid not in seen:seen.add(nid);graph['nodes'].append({'id':nid,'label':label,'namespace':event['source_namespace'],'kind':kind,'verified':False})
        graph['edges'].append({'source':ids[0],'target':ids[1],**event})
    evidence=[{'hypothesis':'组织指挥/汇报关系','supports':[e['record_id'] for e in hierarchy['edges']],'contradictions':hierarchy['cycles'],'identified':False,'remaining':['原文真实性及指令/执行证据核验','同名与匿名编号的实体确认','替代解释和独立来源核查'],'scope':'结构与来源一致性检查，不是个案因果证明'}, {'hypothesis':'可见资金支付与产业再投入','supports':list(dict.fromkeys(e['record_id'] for e in events)),'contradictions':[],'identified':False,'remaining':['完整收付账户/交易哈希及币种、时区','返还与循环资金去重','支付、订单/交付与实际用途的同一性证据'],'scope':'未从时间先后推定因果效应'}]
    return {'source_scopes':scoped,'statement_events':events,'amount_reconstruction':money,'hierarchy_reconstruction':hierarchy,'statement_fund_graph':graph,'evidence_hypotheses':evidence}
