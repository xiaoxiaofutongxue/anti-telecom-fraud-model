import html
import math
from collections import Counter, defaultdict

import networkx as nx

from .core import FLOWS, KINDS

FLOW_COLORS={"funds":"#2563eb","goods":"#059669","information":"#9333ea","people":"#d97706"}
NODE_COLORS={"actor":"#fde7bc","wallet":"#dbeafe","account":"#e0e7ff","order":"#d1fae5","place":"#e2f5e9","vehicle":"#b9e8dd","device":"#f3e8ff","exchange":"#c7d2fe","ip":"#ead7fc"}


def node_choices(graph):
    bases={node:KINDS[data['kind']]+' | '+str(data['label'])+(' | '+str(data['chain']) if data.get('chain') else '') for node,data in graph.nodes(data=True)}
    counts=Counter(bases.values());forward={node:(label if counts[label]==1 else label+' ['+node+']') for node,label in bases.items()}
    return forward,{display:node for node,display in forward.items()}
RELATION_LABELS={"transfer":"转账","payment":"关联订单","exchange_in":"兑换关联入","exchange_out":"兑换关联出","dispatch":"起运","deliver":"到达","sender":"发货方","receiver":"收货方","carrier":"运输方","transport":"运输工具","communicate":"通信","uses_device":"使用载体","identity":"身份关联","actor_relation":"主体关系","observed_ip":"主体网络观测","account_access":"账户网络观测","device_network":"设备网络观测"}


def positions(graph,width=1000,height=650):
    if not graph:
        return {}
    raw=nx.spring_layout(graph,seed=42,iterations=120,weight=None,k=1.1/max(1,math.sqrt(len(graph))))
    xs=[float(v[0]) for v in raw.values()]; ys=[float(v[1]) for v in raw.values()]
    lo_x,hi_x=min(xs),max(xs); lo_y,hi_y=min(ys),max(ys)
    result={node:[65+(float(v[0])-lo_x)/max(1e-8,hi_x-lo_x)*(width-130),60+(float(v[1])-lo_y)/max(1e-8,hi_y-lo_y)*(height-130)] for node,v in raw.items()}
    minimum=min(94,max(35,math.sqrt(width*height/(max(1,len(graph))*2))*.95))
    nodes=sorted(result)
    for _ in range(90):
        moved=False
        for i,a in enumerate(nodes):
            for b in nodes[i+1:]:
                dx=result[b][0]-result[a][0];dy=result[b][1]-result[a][1]
                distance=math.hypot(dx,dy)
                if distance<minimum:
                    if distance<.01:dx,dy,distance=1.,1.,math.sqrt(2)
                    push=(minimum-distance)*.52
                    for node,sign in [(a,-1),(b,1)]:
                        result[node][0]=min(width-65,max(65,result[node][0]+sign*dx/distance*push))
                        result[node][1]=min(height-65,max(60,result[node][1]+sign*dy/distance*push))
                    moved=True
        if not moved:break
    return result


def edges_geometry(graph,pos):
    count=Counter((u,v) for u,v,k in graph.edges(keys=True)); seen=defaultdict(int)
    for u,v,key,data in graph.edges(keys=True,data=True):
        x1,y1=pos[u]; x2,y2=pos[v]
        i=seen[(u,v)]; seen[(u,v)]+=1
        if u==v:
            points=(x1-10,y1-18,x1-45,y1-70,x1+45,y1-70,x1+10,y1-18)
            label=(x1+40,y1-45)
            path=f"M {points[0]} {points[1]} C {' '.join(str(p) for p in points[2:])}"
        else:
            dx,dy=x2-x1,y2-y1; distance=max(1,math.hypot(dx,dy))
            offset=15+(i-(count[(u,v)]-1)/2)*28
            cx,cy=(x1+x2)/2-dy/distance*offset,(y1+y2)/2+dx/distance*offset
            d1=max(1,math.hypot(cx-x1,cy-y1)); d2=max(1,math.hypot(x2-cx,y2-cy))
            sx,sy=x1+(cx-x1)/d1*20,y1+(cy-y1)/d1*20
            ex,ey=x2-(x2-cx)/d2*24,y2-(y2-cy)/d2*24
            points=(sx,sy,cx,cy,ex,ey)
            label=((sx+2*cx+ex)/4,(sy+2*cy+ey)/4-7)
            path=f"M {sx} {sy} Q {cx} {cy} {ex} {ey}"
        yield u,v,key,data,points,label,path


def edge_label(data):
    if data["relation"]=="transfer":
        return f"{data['amount']:.2f} {data['currency']}"
    return RELATION_LABELS.get(data["relation"],data["relation"])


def graph_svg(graph,highlight=None,width=1100,height=760):
    if len(graph)>150:
        nodes=list(graph)[:150]
        graph=graph.subgraph(nodes).copy()
        caption="图像仅绘制前150个节点；结构化数据及统计包含完整分析图。"
    else:
        caption="箭头表示记录中的关系方向；橙色表示所选关联路径。"
    highlight=highlight or {"nodes":[],"edges":[]}
    selected={tuple(e) for e in highlight.get("edges",[])}
    pos=positions(graph,width,height-50)
    esc=lambda x:html.escape(str(x),quote=True)
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="四流关系图"><rect width="100%" height="100%" fill="#ffffff"/><defs>']
    for key,color in {**FLOW_COLORS,"selected":"#ea580c","pending":"#94a3b8"}.items():
        parts.append(f'<marker id="arrow_{key}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="{color}"/></marker>')
    parts.append('</defs><g font-family="Microsoft YaHei,Arial,sans-serif">')
    for u,v,key,data,points,label,path in edges_geometry(graph,pos):
        style="selected" if (u,v,key) in selected else ("pending" if data.get("verified")=="pending" else data["flow"])
        color={**FLOW_COLORS,"selected":"#ea580c","pending":"#94a3b8"}[style]
        title=f"{u} → {v} | {edge_label(data)} | {data['timestamp']} | 来源 {data['source_id']} / {data['record_id']}"
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{2.5 if style=="selected" else 1.3}" marker-end="url(#arrow_{style})"><title>{esc(title)}</title></path>')
        if style=="selected" or (data["relation"]=="transfer" and len(graph)<20 and {d["flow"] for a,b,k,d in graph.edges(keys=True,data=True)}=={"funds"}):
            parts.append(f'<text x="{label[0]}" y="{label[1]}" text-anchor="middle" font-size="10" fill="#334155" stroke="white" stroke-width="4" paint-order="stroke">{esc(edge_label(data))}</text>')
    for node,data in graph.nodes(data=True):
        x,y=pos[node]; color="#fed7aa" if node in highlight.get("nodes",[]) else NODE_COLORS[data["kind"]]
        label=data["label"] if len(data["label"])<=19 else data["label"][:16]+"…"
        parts.append(f'<circle cx="{x}" cy="{y}" r="18" fill="{color}" stroke="#64748b"><title>{esc(data["label"])} / {esc(node)}</title></circle><text x="{x}" y="{y+4}" text-anchor="middle" font-size="9">{esc(KINDS[data["kind"]][:2])}</text>')
        if len(graph)<=35 or data['kind'] in {'actor','order'} or node in highlight.get('nodes',[]):parts.append(f'<text x="{x}" y="{y+34}" text-anchor="middle" font-size="11">{esc(label)}</text>')
    parts.append(f'<text x="20" y="{height-22}" font-size="12" fill="#64748b">{esc(caption)}</text></g></svg>')
    return "".join(parts)
