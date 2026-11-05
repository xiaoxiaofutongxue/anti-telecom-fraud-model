"""Four-flow joins over explicit source fields; no inferred identity or automatic legal classification."""
from collections import Counter,defaultdict
from datetime import datetime,timezone
from decimal import Decimal
import hashlib,ipaddress,json,re
from .narrative_fields import field_view
from .narrative_reconstruction import source_scopes,statement_events
from .narrative_extract import FLOW_NAMES

MAX_TRACES=200
MAX_EVIDENCE=200


def _id(*parts):return hashlib.sha256(json.dumps(parts,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:18]


def observation_time(raw):
    raw=str(raw or '').strip();result={'raw':raw,'instant':None,'precision':'unknown','timezone_known':False}
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw):
        try:datetime.fromisoformat(raw);result['precision']='day'
        except ValueError:pass
        return result
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?',raw):return result
    try:
        offset=re.search(r'[+-](\d{2}):(\d{2})$',raw)
        if offset and (int(offset[1])>23 or int(offset[2])>59):raise ValueError('时区偏移无效')
        value=datetime.fromisoformat(raw.replace('Z','+00:00'));result['precision']='time';result['timezone_known']=value.tzinfo is not None
        if value.tzinfo is not None:result['instant']=value.astimezone(timezone.utc).isoformat()
    except (ValueError,OverflowError):result.update(instant=None,timezone_known=False,precision='invalid_time')
    return result


def _seconds(time):return datetime.fromisoformat(time['instant']).timestamp() if time['instant'] else None


def build_observations(rows,excluded=(),payment_events=None):
    active=[r for r in rows if r['id'] not in excluded and not r.get('formula_present')];scopes=source_scopes(active)['rows'];observations=[];warnings=[];duplicates=defaultdict(list)
    events=statement_events(active,scopes) if payment_events is None else payment_events
    payment_rows={p['record_id'] for p in events if Decimal(p['amount'])>0}
    for row in active:
        view=field_view(row['fields']);f=view['values'];scope=scopes[row['id']];namespace=scope['namespace'];anchors=[];missing=[]
        # Explicit identifier namespaces stay inside this file/case. A mixed document source remains unresolved.
        identifier_scope=f.get('来源标识域') or namespace
        def anchor(kind,label,domain=''):
            if not label or scope['ambiguous']:return
            anchors.append({'kind':kind,'label':label,'domain':domain,'key':_id(row['case_id'],identifier_scope,kind,domain,label)})
        people=sorted({f[k] for k in ('人员编号','发送主体','接收主体','发货主体','收货主体','承运主体') if f.get(k)})
        for person in people:anchor('actor_id',person)
        for key in ('付款账户','收款账户','访问账户'):
            if not f.get(key):continue
            if f.get('链/账户域'):anchor('account',f[key],f['链/账户域'])
            else:missing.append(key+'缺少链/账户域，未跨行合并账户')
        if f.get('设备编号'):anchor('device',f['设备编号'])
        if f.get('订单编号'):anchor('order',f['订单编号'])
        time=observation_time(f.get('时间',''));ip=None;ip_scope=None;ip_problem=None
        if f.get('IP地址'):
            try:
                if '%' in f['IP地址'] or '/' in f['IP地址']:raise ValueError('IP须为单个地址，不能含接口后缀或网段')
                address=ipaddress.ip_address(f['IP地址']);ip=str(address)
                if address.is_global:ip_scope='global'
                elif f.get('IP网络域') and f['IP网络域'].casefold()!='global':ip_scope=f['IP网络域']
                elif f.get('IP网络域'):ip_problem='非公网IP不能使用global作为网络域，未合并网络观测'
                else:ip_problem='非公网IP缺少IP网络域，未合并网络观测'
            except ValueError:ip_problem='IP地址格式无效，保留原文待核对'
            if f.get('源端口') and (not re.fullmatch(r'[0-9]{1,5}',f['源端口']) or not 1<=int(f['源端口'])<=65535):ip_problem='源端口须为1至65535的ASCII整数'
        if ip_problem:missing.append(ip_problem)
        if scope['ambiguous']:missing.append('一行混合多个文书范围，未跨行匹配标识')
        flow=[]
        has_parties=bool(f.get('付款账户') and f.get('收款账户') or (f.get('付款人') or f.get('付款人/日期')) and f.get('收款方'))
        payment=row['id'] in payment_rows
        if has_parties and not payment:missing.append('付款字段缺少可用正数金额及明确币种，未建立付款关联')
        information=bool(ip or f.get('发送主体') and f.get('接收主体') or f.get('通信摘要'))
        person=bool(f.get('人员编号') and (f.get('空间流动') or f.get('行为/职责')) or f.get('人员/角色'))
        industry=bool(f.get('物品名称') or f.get('运输编号') or f.get('产业类型'))
        for present,name in ((payment,'funds'),(information,'information'),(person,'people'),(industry,'industry')):
            if present:flow.append(name)
        missing.extend('字段冲突：'+c['field'] for c in view['conflicts'])
        if information and not time['instant']:missing.append('信息记录缺少带时区的明确时刻，未作分钟/小时窗口匹配')
        if industry and not f.get('订单编号'):missing.append('产业记录缺少订单编号，只有字段背景候选')
        fingerprint=json.dumps(f,sort_keys=True,ensure_ascii=False)
        if f:duplicates[fingerprint].append(row['id'])
        if missing:warnings.append({'record_id':row['id'],'locator':row['locator'],'issues':list(dict.fromkeys(missing))})
        anchors=list({a['key']:a for a in anchors}.values())
        observations.append({'record_id':row['id'],'case_id':row['case_id'],'locator':row['locator'],'source_citation':row.get('source_citation',''),'namespace':namespace,'identifier_scope':identifier_scope,'flows':flow,'time':time,'anchors':anchors,'order_id':f.get('订单编号',''),'goods':f.get('物品名称',''),'shipment_id':f.get('运输编号',''),'ip':ip,'ip_scope':ip_scope,'ip_usable':bool(ip and ip_scope and not ip_problem and time['instant'] and any(a['kind'] in {'actor_id','account','device'} for a in anchors)),'field_mapping':view,'missing':list(dict.fromkeys(missing)),'verified':False})
    groups=[ids for ids in duplicates.values() if len(ids)>1]
    quality={'input_rows':len(rows),'active_rows':len(active),'excluded_rows':sum(r['id'] in excluded for r in rows),'formula_rows':sum(bool(r.get('formula_present')) and r['id'] not in excluded for r in rows),'duplicate_candidate_groups':groups,'duplicate_policy':'仅提示同字段内容重复候选，未删除原行或认定同一事件','warnings':warnings,'conflicting_rows':sum(bool(o['field_mapping']['conflicts']) for o in observations),'invalid_ip_rows':sum(any('IP地址格式无效' in m for m in o['missing']) for o in observations)}
    return observations,quality


def network_findings(observations,window_minutes=30):
    if not 1<=window_minutes<=1440:raise ValueError('IP关联窗口须为1至1440分钟')
    groups=defaultdict(list);by_actor=defaultdict(list);findings=[];ips=[]
    for o in observations:
        if not o['ip_usable']:continue
        key=(o['case_id'],o['identifier_scope'],o['ip_scope'],o['ip']);groups[key].append(o)
        for a in o['anchors']:
            if a['kind'] in {'actor_id','account'}:by_actor[a['key']].append(o)
    for key,events in sorted(groups.items()):
        events.sort(key=lambda o:(_seconds(o['time']),o['record_id']));labels={a['key'] for o in events for a in o['anchors'] if a['kind'] in {'actor_id','account'}}
        ips.append({'ip':key[-1],'network_scope':key[-2],'identifier_scope':key[1],'event_count':len(events),'anchor_count':len(labels),'record_ids':[o['record_id'] for o in events],'first_seen':events[0]['time']['instant'],'last_seen':events[-1]['time']['instant']})
        right=0;counts=Counter();kinds={a['key']:a['kind'] for o in events for a in o['anchors']}
        for left,start in enumerate(events):
            while right<len(events) and _seconds(events[right]['time'])-_seconds(start['time'])<=window_minutes*60:
                counts.update(a['key'] for a in events[right]['anchors'] if a['kind'] in {'actor_id','account'});right+=1
            if right-left>=2 and any(sum(kinds[k]==kind for k in counts)>=2 for kind in ('actor_id','account')):
                window=events[left:right];findings.append({'kind':'shared_ip','title':'同时间窗共享IP候选','ip':key[-1],'record_ids':[o['record_id'] for o in window],'detail':'同一标识域及网络域内，不同主体/账户引用使用同一IP；NAT、代理或共享网络均可能造成，未据此并人。','verified':False});break
            counts.subtract(a['key'] for a in start['anchors'] if a['kind'] in {'actor_id','account'});counts=+counts
    for key,events in sorted(by_actor.items()):
        events.sort(key=lambda o:(_seconds(o['time']),o['record_id']));right=0;counts=Counter()
        for left,start in enumerate(events):
            while right<len(events) and _seconds(events[right]['time'])-_seconds(start['time'])<=window_minutes*60:counts[(events[right]['ip_scope'],events[right]['ip'])]+=1;right+=1
            if len(counts)>=3:
                findings.append({'kind':'multi_ip','title':'短时多IP切换候选','ip':'','record_ids':[o['record_id'] for o in events[left:right]],'detail':'同一主体/账户引用在窗口内出现至少3个IP；未推断真实位置或代理行为。','verified':False});break
            counts[(start['ip_scope'],start['ip'])]-=1;counts=+counts
    return {'window_minutes':window_minutes,'usable_observations':sum(o['ip_usable'] for o in observations),'ips':ips,'findings':findings[:100],'omitted_findings':max(0,len(findings)-100),'scope':'仅分析明确IP、时区时刻及主体/账户/设备对应的来源记录；不做IP地理定位或实名推断'}


def _temporal(payment,goods,window_hours):
    p,g=_seconds(payment['time']),_seconds(goods['time'])
    if p is None or g is None:return {'record_id':payment['record_id'],'status':'顺序待核验','detail':'缺少带时区的明确时刻，未按日期零时补造顺序'}
    gap=(g-p)/3600
    return {'record_id':payment['record_id'],'status':'付款晚于产业记录' if gap<0 else '付款超出关联窗口' if gap>window_hours else '窗口内先付款后产业记录','gap_hours':round(gap,6),'detail':'同订单字段关联不等同用途或因果认定'}


def industry_candidates(observations,paths,rows,window_hours=168):
    if not 1<=window_hours<=720:raise ValueError('产业关联窗口须为1至720小时')
    index=defaultdict(list);goods=defaultdict(list);traces=[]
    for o in observations:
        for a in o['anchors']:index[a['key']].append(o)
        if 'industry' in o['flows']:
            order=next((a['key'] for a in o['anchors'] if a['kind']=='order'),o['record_id']);goods[order].append(o)
    total_groups=len(goods)
    for key,shipments in list(sorted(goods.items()))[:MAX_TRACES]:
        seed=min(shipments,key=lambda o:(o['time']['instant'] or '',o['record_id']));associated={o['record_id']:o for o in shipments};basis={o['record_id']:'产业来源原行' for o in shipments};payments={};context_skipped=set();checks=[]
        # Same explicit order is a direct field association even when time is missing or contradicts sequence.
        for g in shipments:
            for a in g['anchors']:
                if a['kind']!='order':continue
                for o in index[a['key']]:
                    if 'funds' in o['flows']:payments[o['record_id']]=o;associated[o['record_id']]=o;basis[o['record_id']]='同订单编号的付款陈述'
        for p in payments.values():checks.append(_temporal(p,seed,window_hours))
        keys={a['key'] for o in [*shipments,*payments.values()] for a in o['anchors'] if a['kind'] in {'actor_id','account'}}
        for anchor in sorted(keys):
            for o in index[anchor]:
                if o['record_id'] in associated or not {'information','people'}&set(o['flows']):continue
                when=_seconds(o['time']);gtime=_seconds(seed['time'])
                if when is not None and gtime is not None and 0<=gtime-when<=window_hours*3600:
                    associated[o['record_id']]=o;basis[o['record_id']]='共同主体/账户引用且处于产业记录前的时间窗；背景关联'
                else:context_skipped.add(o['record_id'])
        flows=[f for f in FLOW_NAMES if any(f in o['flows'] for o in associated.values())];missing=[]
        if not payments:missing.append('缺少同订单明确付款陈述，未用金额相近代替')
        if not seed['order_id']:missing.append('缺少订单编号，未连接跨行资金与产业')
        if context_skipped:missing.append(str(len(context_skipped))+'条共同标识记录因缺时刻、超窗或次序不符未并入路径')
        for f in FLOW_NAMES:
            if f not in flows:missing.append('缺少可关联的'+FLOW_NAMES[f]+'观测')
        if any(c['status']!='窗口内先付款后产业记录' for c in checks):missing.append('付款与产业记录的时间顺序或窗口需要核验')
        missing.extend(['主体/账户引用的现实身份及货物交付、实际用途仍需独立材料核验','同一文件中的多条记录不自动算独立来源印证'])
        evidence=sorted(associated.values(),key=lambda o:(o['time']['instant'] or 'z',o['record_id']));ids=[o['record_id'] for o in evidence]
        traces.append({'trace_id':'T_'+_id(seed['case_id'],key),'kind':'同订单跨流关联候选','order_id':seed['order_id'],'goods':sorted({o['goods'] for o in shipments if o['goods']}),'flows':flows,'record_ids':ids,'evidence':[{'record_id':o['record_id'],'locator':o['locator'],'source_citation':o['source_citation'],'time':o['time'],'flows':o['flows'],'basis':basis[o['record_id']]} for o in evidence[:MAX_EVIDENCE]],'omitted_evidence':max(0,len(evidence)-MAX_EVIDENCE),'temporal_checks':checks,'missing':missing,'verified':False,'priority_basis':'先核对同订单付款和交付，再核对身份及背景记录；不输出违法概率'})
    # Original narrative paths remain a separate evidence class, never stitched into transaction observations.
    lookup={r['id']:r for r in rows};industry_paths=[p for p in paths if 'industry' in lookup[p['record_id']]['flows']]
    available=max(0,MAX_TRACES-len(traces))
    for p in industry_paths[:available]:
        row=lookup[p['record_id']];traces.append({'trace_id':'T_'+_id(p['record_id'],p['path']),'kind':'原文产业路径陈述','order_id':'','goods':[],'flows':row['flows'],'record_ids':[p['record_id']],'evidence':[{'record_id':p['record_id'],'locator':p['locator'],'source_citation':p['source_citation'],'time':observation_time(row.get('time',{}).get('raw','')),'flows':row['flows'],'basis':'同一原行明确写出的路径：'+p['path']}],'omitted_evidence':0,'temporal_checks':[],'missing':['缺少逐笔资金、订单/交付、IP日志及身份对应，未跨行拼接成已核实链条'],'verified':False,'priority_basis':'先回原件核对路径陈述，再取得逐笔和交付材料'})
    return {'window_hours':window_hours,'traces':traces,'total_candidate_groups':total_groups+len(industry_paths),'omitted_traces':max(0,total_groups+len(industry_paths)-len(traces)),'scope':'明确订单及标识的候选关联，或独立列示的原文产业路径；没有认定资金同一性、违法用途或因果'}


def analyze_fusion(rows,paths,excluded=(),window_hours=168,network_minutes=30,payment_events=None):
    observations,quality=build_observations(rows,excluded,payment_events);active={o['record_id'] for o in observations};paths=[p for p in paths if p['record_id'] in active];network=network_findings(observations,network_minutes);industry=industry_candidates(observations,paths,rows,window_hours)
    requirements={'funds':'付款/收款引用、明确金额币种；账户关联还需链/账户域','information':'通信双方或IP与主体/账户/设备对应；窗口计算需带时区时刻','people':'明确人员编号或称谓及职责/活动记录','industry':'订单号、物品/服务或运单；溯源需付款及交付依据'}
    coverage=[{'flow':flow,'name':name,'source_rows':sum(flow in r['flows'] for r in rows if r['id'] not in excluded and not r.get('formula_present')),'field_observations':sum(flow in o['flows'] for o in observations),'required':requirements[flow]} for flow,name in FLOW_NAMES.items()]
    return {'quality':quality,'coverage':coverage,'observations':observations,'network':network,'industry':industry,'scope':'来源字段适配、质量检查与可追溯关联候选；字段完整度不是模型准确率或犯罪概率'}
