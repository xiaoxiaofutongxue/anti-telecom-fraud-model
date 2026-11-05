"""由实际记录构建第二条事件轨迹图，不把时序相邻关系当作因果或同一笔资金。"""
from collections import defaultdict
import html

import networkx as nx

from .core import FLOWS,parse_time
from .investigation import unique_records


def behavior_graph(graph,max_gap_hours=72,max_events=10000):
    if not 0<max_gap_hours<=720:raise ValueError('行为轨迹关联窗口须为0至720小时')
    records=unique_records(graph)
    if len(records)>max_events:return {'available':False,'reason':f'事件数超过本地轨迹图{max_events}条上限，未构建轨迹图；原始统计未截断'}
    result=nx.DiGraph();by_entity=defaultdict(list)
    for (table,rid),record in records.items():
        data=record['data'];uid=table+':'+rid;entities=sorted({n for u,v,k in record['edges'] for n in (u,v)})
        result.add_node(uid,table=table,record_id=rid,flow=data['flow'],timestamp=data['timestamp'],source_id=data['source_id'],csv_line=data['line'],entities=entities)
        for entity in entities:by_entity[entity].append(uid)
    for entity,events in by_entity.items():
        events.sort(key=lambda uid:(result.nodes[uid]['timestamp'],uid))
        for a,b in zip(events,events[1:]):
            gap=(parse_time(result.nodes[b]['timestamp'])-parse_time(result.nodes[a]['timestamp'])).total_seconds()/3600
            if not 0<=gap<=max_gap_hours:continue
            if not result.has_edge(a,b):result.add_edge(a,b,entities=[],gap_hours=gap,relation='同实体先后出现')
            result[a][b]['entities'].append(entity)
    if not nx.is_directed_acyclic_graph(result):raise ValueError('行为轨迹图产生非预期环，请检查时间顺序')
    return {'available':True,'case_id':graph.graph['case_id'],'as_of':graph.graph.get('as_of',''),'max_gap_hours':max_gap_hours,'nodes':[{'id':node,**data} for node,data in result.nodes(data=True)],'edges':[{'from':u,'to':v,**data} for u,v,data in result.edges(data=True)],'interpretation':'事件节点来自去重原始记录；边只表示同一实体在窗口内先后出现，不证明资金归属、实体控制或因果关系。'}


def behavior_svg(result,limit=40,width=1100):
    esc=lambda value:html.escape(str(value),quote=True)
    if not result.get('available'):return '<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="60"><text x="20" y="30">行为轨迹图不可用，见结构化说明。</text></svg>'
    nodes=sorted(result['nodes'],key=lambda n:(n['timestamp'],n['id']))[:limit];height=max(180,120+len(nodes)*47);positions={}
    colors={'funds':'#dbeafe','goods':'#d1fae5','information':'#f3e8ff','people':'#fde7bc'};flows=list(FLOWS)
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><defs><marker id="event_arrow" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#94a3b8"/></marker></defs><rect width="100%" height="100%" fill="white"/>']
    for i,flow in enumerate(flows):parts.append(f'<text x="{145+i*265}" y="30" text-anchor="middle" font-size="16">{FLOWS[flow]}</text>')
    for i,node in enumerate(nodes):positions[node['id']]=(145+flows.index(node['flow'])*265,75+i*47)
    for edge in result['edges']:
        if edge['from'] in positions and edge['to'] in positions:
            x,y=positions[edge['from']];xx,yy=positions[edge['to']]
            parts.append(f'<line x1="{x}" y1="{y+14}" x2="{xx}" y2="{yy-15}" stroke="#cbd5e1" marker-end="url(#event_arrow)"><title>{esc("；".join(edge["entities"]))}</title></line>')
    for node in nodes:
        x,y=positions[node['id']];parts.append(f'<rect x="{x-115}" y="{y-15}" width="230" height="32" rx="5" fill="{colors[node["flow"]]}" stroke="#cbd5e1"/><text x="{x}" y="{y+5}" text-anchor="middle" font-size="11">{esc(node["record_id"][:20])} · {esc(node["timestamp"][5:16])}</text>')
    parts.append(f'<text x="20" y="{height-18}" font-size="12" fill="#64748b">显示{len(nodes)}/{len(result["nodes"])}个事件；完整结构见behavior_graph.json。箭头为同实体时序关联，非因果。</text></svg>')
    return ''.join(parts)
