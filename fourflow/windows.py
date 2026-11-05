"""按原始事件分窗；保留完整统计，不把局部分数拼成整案概率。"""
from .investigation import unique_records
from .model import graph_fingerprint
from .core import parse_time


def plan_windows(graph,max_nodes=256,max_events=128,overlap=24,max_windows=64):
    if not 2<=max_nodes<=256 or not 1<=max_events<=256 or not 0<=overlap<max_events or not 1<=max_windows<=512:raise ValueError('分窗大小、重叠或窗口上限无效')
    records=unique_records(graph);ordered=sorted(records,key=lambda k:(parse_time(records[k]['data']['timestamp']),k));windows=[];covered=set();start=0
    reference=graph.graph.get('as_of') or (max((d['data']['timestamp'] for d in records.values()),key=parse_time) if records else '')
    while start<len(ordered) and len(windows)<max_windows:
        chosen=[];nodes=set();end=start
        while end<len(ordered) and len(chosen)<max_events:
            key=ordered[end];new={n for u,v,k in records[key]['edges'] for n in (u,v)}
            if len(nodes|new)>max_nodes:break
            nodes|=new;chosen.append(key);end+=1
        if not chosen:raise ValueError('单条事件超过本地节点上限，须人工拆分该原始事件')
        edges=[e for key in chosen for e in records[key]['edges']];sub=graph.edge_subgraph(edges).copy();sub.graph.update(graph.graph);sub.graph['as_of']=reference
        covered.update(chosen);windows.append({'index':len(windows)+1,'graph':sub,'record_keys':chosen,'nodes':len(sub),'events':len(chosen),'start':records[chosen[0]]['data']['timestamp'],'end':records[chosen[-1]]['data']['timestamp'],'graph_fingerprint':graph_fingerprint(sub)})
        start=max(start+1,end-overlap) if end<len(ordered) else end
    return {'windows':windows,'total_records':len(ordered),'covered_records':len(covered),'coverage':len(covered)/len(ordered) if ordered else 1.,'truncated':len(covered)<len(ordered),'reference_time':reference,'interpretation':'窗口可重叠，金额和候选不得跨窗口相加；窗口分数不是整案概率。完整金额与原记录统计仍使用原图。'}
