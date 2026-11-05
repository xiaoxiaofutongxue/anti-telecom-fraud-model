"""反电诈辅助分析：网络观测、订单关联溯源与可解释的多目标核验排序。"""
from collections import defaultdict, Counter
from datetime import timedelta
import hashlib
import ipaddress

from .core import FLOWS, SERVICE_TYPES, parse_time


def unique_records(graph, table=None):
    records={}
    for u,v,key,data in graph.edges(keys=True,data=True):
        if data.get('verified')=='pending' or (table and data['table']!=table):continue
        records.setdefault((data['table'],data['record_id']),{'data':data,'edges':[]})['edges'].append((u,v,key))
    return records


def behavior_timeline(dataset,graph):
    result=[]
    for (table,rid),entry in unique_records(graph).items():
        data=entry['data'];source=dataset.source(graph.graph['case_id'],data['source_id'])
        result.append({'timestamp':data['timestamp'],'effective_from':data.get('effective_from',data['timestamp']),'lineage_verified':data.get('lineage_verified',True),'flow':FLOWS[data['flow']],'table':table,'record_id':rid,'nodes':sorted({n for u,v,k in entry['edges'] for n in (u,v)}),'source_id':data['source_id'],'source_title':source['title'] if source else '', 'csv_line':data['line'],'original_line':data.get('original_line',data['line']),'original_sha256':data.get('original_sha256',''),'verification':data['verified']})
    return sorted(result,key=lambda r:(r['timestamp'],r['table'],r['record_id']))


def network_analysis(graph,window_minutes=30):
    if not 1<=window_minutes<=1440:raise ValueError('网络时间窗须在1至1440分钟内')
    groups=defaultdict(list); anchors=defaultdict(list); findings=[]; observations=[]
    for (_,record_id),entry in unique_records(graph,'network_events').items():
        data=entry['data']; ips={v for u,v,k in entry['edges'] if graph.nodes[v]['kind']=='ip'}
        for ip in ips:
            related=sorted({u for u,v,k in entry['edges'] if v==ip and graph.nodes[u]['kind'] in {'actor','wallet','account'}})
            row={'record_id':record_id,'ip_node':ip,'ip':graph.nodes[ip]['entity_id'],'scope':data['network_scope'],'timestamp':data['timestamp'],'anchors':related,'device_nodes':sorted({u for u,v,k in entry['edges'] if graph.nodes[u]['kind']=='device'}),'source_id':data['source_id'],'source_port':data.get('source_port',''),'event_type':data.get('event_type',''),'csv_line':data['line'],'original_line':data.get('original_line',data['line']),'edges':entry['edges']}
            observations.append(row);groups[ip].append(row)
            for anchor in related:anchors[anchor].append(row)
    def add(kind,title,rows,nodes,detail):
        ids=sorted({r['record_id'] for r in rows}); nodes=sorted(set(nodes))
        fid='F'+hashlib.sha256((kind+'|'+'|'.join(ids)+'|'+'|'.join(nodes)).encode()).hexdigest()[:10]
        findings.append({'id':fid,'kind':kind,'title':title,'nodes':nodes,'record_ids':ids,'source_ids':sorted({r['source_id'] for r in rows}),'detail':detail,'status':'待核验','edges':[list(e) for r in rows for e in r['edges']]})
    ip_rows=[]
    for ip,events in groups.items():
        events.sort(key=lambda r:r['timestamp']); different=set(a for r in events for a in r['anchors'])
        ip_rows.append({'ip_node':ip,'ip':graph.nodes[ip]['entity_id'],'scope':events[0]['scope'],'event_count':len(events),'anchor_count':len(different),'first_seen':events[0]['timestamp'],'last_seen':events[-1]['timestamp'],'address_class':'公网' if events[0]['scope']=='global' else '专用/共享/特殊地址','source_ids':sorted({r['source_id'] for r in events})})
        # 滑动窗口，每个IP只给一个可复核实例；不展开全部两两组合。
        right=0;actor_counts=Counter()
        for left,event in enumerate(events):
            while right<len(events) and parse_time(events[right]['timestamp'])-parse_time(event['timestamp'])<=timedelta(minutes=window_minutes):
                actor_counts.update(events[right]['anchors']);right+=1
            actors={a for a in actor_counts if graph.nodes[a]['kind']=='actor'}
            accounts={a for a in actor_counts if graph.nodes[a]['kind'] in {'wallet','account'}}
            if len(actors)>=2 or len(accounts)>=2:
                window=events[left:right]
                add('shared_ip','同时间窗共享IP线索',window,[ip,*actors,*accounts],f'{window_minutes}分钟内观测到{len(actors)}个主体、{len(accounts)}个账户使用相同IP/网络域；NAT、代理、共享网络等均可造成共用，不能推断同一人或控制关系。');break
            for a in event['anchors']:
                actor_counts[a]-=1
                if not actor_counts[a]:del actor_counts[a]
    for anchor,events in anchors.items():
        events.sort(key=lambda r:r['timestamp']);right=0;ip_counts=Counter()
        for left,event in enumerate(events):
            while right<len(events) and parse_time(events[right]['timestamp'])-parse_time(event['timestamp'])<=timedelta(minutes=window_minutes):ip_counts[events[right]['ip_node']]+=1;right+=1
            ips=set(ip_counts)
            if len(ips)>=3:
                window=events[left:right]
                add('multi_ip','短时多IP切换线索',window,[anchor,*ips],f'{window_minutes}分钟内同一来源主体/账户出现{len(ips)}个IP。仅为观测变化，不推断物理移动、代理使用或违法。');break
            ip_counts[event['ip_node']]-=1
            if not ip_counts[event['ip_node']]:del ip_counts[event['ip_node']]
    return {'ips':ip_rows,'observations':sorted(observations,key=lambda r:r['timestamp']),'findings':findings[:100],'window_minutes':window_minutes,'interpretation':'IP为时点网络观测，不用于自动实名绑定；非公网IP按network_scope隔离，不做IP地理定位。','truncated':len(findings)>100}


def industry_traces(dataset,graph,window_hours=168,weights=None):
    if not 1<=window_hours<=720:raise ValueError('产业关联时间窗须为1至720小时')
    weights=weights or {'coverage':.5,'source_diversity':.25,'temporal':.25}
    if set(weights)!={'coverage','source_diversity','temporal'} or any(not isinstance(v,(int,float)) or not 0<=v<=1 for v in weights.values()) or abs(sum(weights.values())-1)>1e-8:raise ValueError('三个排序权重须非负且合计为1')
    records=unique_records(graph);traces=[]
    order_nodes=[n for n,d in graph.nodes(data=True) if d['kind']=='order']
    for order in order_nodes:
        goods=[entry for (table,rid),entry in records.items() if table=='shipments' and any(order in (u,v) for u,v,k in entry['edges'])]
        if not goods:continue
        goods.sort(key=lambda e:e['data']['timestamp'])
        first=parse_time(goods[0]['data']['timestamp']);last=parse_time(goods[-1]['data']['timestamp'])
        earliest=first-timedelta(hours=window_hours)
        payments=[(u,v,k,d) for u,v,k,d in graph.in_edges(order,keys=True,data=True) if d['relation']=='payment' and earliest<=parse_time(d['timestamp'])<=first]
        initial_people={u for entry in goods for u,v,k in entry['edges'] if graph.nodes[u]['kind']=='actor'}
        people=set(initial_people)
        accounts={u for u,v,k,d in payments}
        identities=[]
        for u,v,k,d in graph.edges(keys=True,data=True):
            if d['relation']=='identity' and d.get('verified')=='verified' and parse_time(d.get('effective_from',d['timestamp']))<=first and (v in accounts or u in initial_people):
                identities.append((u,v,k));people.add(u)
        context=[]
        for (table,rid),entry in records.items():
            if table not in {'communications','network_events'}:continue
            when=parse_time(entry['data']['timestamp'])
            if not any(parse_time(event['data']['timestamp'])-timedelta(hours=window_hours)<=when<=parse_time(event['data']['timestamp']) for event in goods):continue
            if any(u in people or v in people or u in accounts or v in accounts for u,v,k in entry['edges']):context.extend(entry['edges'])
        chosen=set([e[:3] for e in payments]+identities+context+[e for entry in goods for e in entry['edges']])
        # 每条原始记录仅显示一次；派生边不会造成证据数量或金额膨胀。
        steps=[];seen=set()
        for u,v,k in sorted(chosen,key=lambda e:graph[e[0]][e[1]][e[2]]['timestamp']):
            d=graph[u][v][k];key=(d['table'],d['record_id'])
            if key in seen:continue
            seen.add(key);source=dataset.source(graph.graph['case_id'],d['source_id'])
            steps.append({'timestamp':d['timestamp'],'effective_from':d.get('effective_from',d['timestamp']),'lineage_verified':d.get('lineage_verified',True),'flow':FLOWS[d['flow']],'table':d['table'],'record_id':d['record_id'],'source_id':d['source_id'],'csv_line':d['line'],'original_line':d.get('original_line',d['line']),'source_title':source['title'] if source else '', 'reference':source['reference'] if source else '', 'association':'同订单直接关联' if d['table'] in {'funds','shipments'} else '经核实身份关联' if d['relation']=='identity' else '参与主体/付款账户在窗口内的背景记录'})
        observed_flows={graph[u][v][k]['flow'] for u,v,k in chosen}
        source_ids={s['source_id'] for s in steps}
        material_groups=set()
        for sid in source_ids:
            source=dataset.source(graph.graph['case_id'],sid) or {}
            material_groups.add(source.get('_sha256') or source.get('reference','').strip() or '未附来源引用')
        gap=min(((first-parse_time(d['timestamp'])).total_seconds()/3600 for u,v,k,d in payments),default=None)
        objectives={'coverage':len(observed_flows)/4,'source_diversity':min(len(material_groups)/3,1),'temporal':max(0,1-gap/window_hours) if gap is not None else 0}
        score=sum(weights[n]*objectives[n] for n in weights)
        labels=sorted({e['data'].get('goods_label','') for e in goods}-{''})
        types=sorted({SERVICE_TYPES.get(e['data'].get('service_type','unspecified'),'未分类') for e in goods})
        missing=[]
        if not payments:missing.append('窗口内未找到发运前的同订单付款记录')
        if not identities:missing.append('缺少在发运前生效的已核实身份对应')
        if not context:missing.append('窗口内缺少参与主体/付款账户的通信或IP记录')
        if len(material_groups)<2:missing.append('仅有单一来源材料组，需独立材料交叉核验')
        if len(labels)>1:missing.append('同订单物品标签不同，不能假定同一批物品')
        routes=[]
        for entry in goods:
            places_from=[graph.nodes[u]['label'] for u,v,k in entry['edges'] if graph[u][v][k]['relation']=='dispatch']
            places_to=[graph.nodes[v]['label'] for u,v,k in entry['edges'] if graph[u][v][k]['relation']=='deliver']
            routes.append({'record_id':entry['data']['record_id'],'from':places_from[0] if places_from else '', 'to':places_to[0] if places_to else '', 'timestamp':entry['data']['timestamp']})
        if any(a['to']!=b['from'] for a,b in zip(routes,routes[1:])):missing.append('运输地点衔接不连续，不能认定连续运输链')
        traces.append({'order':graph.nodes[order]['entity_id'],'order_node':order,'service_types':types,'goods_labels':labels,'routes':routes,'flows':[FLOWS[f] for f in FLOWS if f in observed_flows],'actor_nodes':sorted(people),'account_nodes':sorted(accounts),'steps':steps,'source_ids':sorted(source_ids),'source_group_count':len(material_groups),'source_independence_verified':False,'missing':missing,'objectives':objectives,'weights':weights,'priority_score':round(score*100,2),'pareto_front':True,'window_hours':window_hours,'interpretation':'分数用于材料核验排序，不是违法概率。背景记录表示窗口内关联，不能证明交易目的、控制关系或因果。'})
    for a in traces:
        a['pareto_front']=not any(all(b['objectives'][k]>=a['objectives'][k] for k in weights) and any(b['objectives'][k]>a['objectives'][k] for k in weights) for b in traces if b is not a)
    return sorted(traces,key=lambda r:(not r['pareto_front'],-r['priority_score'],r['order']))


def pca_projection(graph):
    """当前快照的无监督探索；不用于模型拟合、身份合并或标签生成。"""
    import numpy as np
    from .model import encode_graph,FEATURE_NAMES
    x,_=encode_graph(graph)
    centered=(x-x.mean(axis=0))/np.maximum(x.std(axis=0),.1)
    u,s,vt=np.linalg.svd(centered,full_matrices=False)
    dimension=min(2,len(s));coordinates=centered@vt[:dimension].T
    energy=float((s*s).sum())
    return {'method':'标准化后SVD-PCA，仅用于当前快照探索','explained_variance_ratio':[(float(v*v)/energy if energy else 0) for v in s[:dimension]],'feature_names':FEATURE_NAMES,'points':[{'node':node,'pc1':float(coordinates[i,0]),'pc2':float(coordinates[i,1]) if dimension>1 else 0} for i,node in enumerate(sorted(graph))],'loadings':vt[:dimension].tolist(),'interpretation':'相邻投影点不表示同一身份或同一团伙。PCA没有改变监督模型输入。'}
