from collections import defaultdict,Counter
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING,localcontext,InvalidOperation
import itertools
import math
import hashlib

import networkx as nx

from .core import parse_time, is_true,money


def exact_sum(values):
    values=list(values)
    if not values:return Decimal(0)
    integer=max(max(1,v.adjusted()+1) for v in values)
    scale=max(max(0,-v.as_tuple().exponent) for v in values)
    with localcontext() as context:
        context.prec=integer+scale+len(str(len(values)))+3
        return sum(values,Decimal(0))


def comparable_pass_amount(incoming,outgoing):
    with localcontext() as context:
        context.prec=max(len(incoming.as_tuple().digits),len(outgoing.as_tuple().digits))+4
        return incoming*Decimal('.5')<=outgoing<=incoming*Decimal('1.5')


def transfer_edges(graph):
    return [(u,v,k,d) for u,v,k,d in graph.edges(keys=True,data=True) if d["relation"]=="transfer" and d.get('verified')!='pending']


def amount_summary(graph):
    totals, addresses = defaultdict(list), {}
    # payment 等派生关系不重复计入金额；每个 tx_id 只统计原始 transfer 边。
    for u,v,key,data in transfer_edges(graph):
        amount,currency=data["amount"],data["currency"]
        totals[currency].append(amount)
        for node in (u,v):
            addresses.setdefault((node,currency),{"incoming":[],"outgoing":[]})
        addresses[(u,currency)]["outgoing"].append(amount)
        addresses[(v,currency)]["incoming"].append(amount)
    rows=[]
    for (node,currency),values in sorted(addresses.items()):
        incoming,outgoing=exact_sum(values['incoming']),exact_sum(values['outgoing'])
        rows.append({'node':node,'currency':currency,'incoming':incoming,'outgoing':outgoing,'net':exact_sum([incoming,outgoing.copy_negate()])})
    return {"gross_by_currency":{c:exact_sum(v) for c,v in totals.items()},"addresses":rows,"interpretation":"逐币种交易额合计包含重复流转，不等于涉案本金；净流入不等于账户余额。"}


def principal_scenario(principal, coverage_min, coverage_max, currency, source_ids):
    try:p=money(str(principal));lo=Decimal(str(coverage_min));hi=Decimal(str(coverage_max))
    except (InvalidOperation,ValueError) as exc:raise ValueError('本金和覆盖率必须为有效数字') from exc
    if not all(v.is_finite() for v in (p,lo,hi)) or p<=0 or not 0<lo<=hi<=1:
        raise ValueError("本金必须为正数；覆盖率须满足 0 < 下限 ≤ 上限 ≤ 1")
    if not source_ids or not currency:
        raise ValueError("必须填写核实本金的来源编号及币种")
    if any(len(v.as_tuple().digits)>1000 or abs(v.as_tuple().exponent)>1000 for v in (lo,hi)):raise ValueError('覆盖率超过支持的1000位精度范围')
    with localcontext() as context:
        context.prec=30+sum(len(v.as_tuple().digits)+abs(v.as_tuple().exponent) for v in (p,lo,hi))
        lower=(p/hi).quantize(Decimal('.01'),rounding=ROUND_FLOOR);upper=(p/lo).quantize(Decimal('.01'),rounding=ROUND_CEILING)
    return {"confirmed_principal":str(p),"coverage_min":str(lo),"coverage_max":str(hi),"currency":currency,"lower":str(lower),"upper":str(upper),"source_ids":source_ids,"method":"已核实去重本金 / 假定覆盖率","interpretation":"条件情景区间；不是统计置信区间或自动认定的涉案金额。覆盖率由分析人员提供，系统未证明该假设。"}


def detect_patterns(graph, window_minutes=30, fan_threshold=3,diagnostics=None):
    if not 1<=window_minutes<=1440 or not isinstance(fan_threshold,int) or fan_threshold<2:raise ValueError('模式窗口须为1至1440分钟，对端阈值至少2')
    diagnostics=diagnostics if diagnostics is not None else {}
    diagnostics.update(skipped_cycle_currencies=[],truncated_cycle_currencies=[],fund_findings_truncated=False,ip_findings_truncated=False,search_complete=True)
    rows=transfer_edges(graph)
    findings=[]
    def add(kind,title,edges,nodes,detail):
        refs=sorted({e[3]["source_id"] for e in edges})
        records=sorted({e[3]["record_id"] for e in edges})
        finding_id="F"+hashlib.sha256((kind+"|"+"|".join(records)+"|"+"|".join(sorted(set(nodes)))).encode()).hexdigest()[:10]
        findings.append({"id":finding_id,"kind":kind,"title":title,"nodes":sorted(set(nodes)),"record_ids":records,"source_ids":refs,"detail":detail,"status":"待核验","edges":[[e[0],e[1],e[2]] for e in edges]})
    for direction in ("out","in"):
        groups=defaultdict(list)
        for e in rows:
            groups[(e[0] if direction=="out" else e[1],e[3]["currency"])].append(e)
        for (node,currency),edges in groups.items():
            edges=sorted((e for e in edges if e[0]!=e[1]),key=lambda e:e[3]['timestamp'])
            times=[parse_time(e[3]['timestamp']) for e in edges];right=0;counts=Counter()
            for i,first in enumerate(edges):
                while right<len(edges) and (times[right]-times[i]).total_seconds()<=window_minutes*60:
                    e=edges[right];counts[e[1] if direction=='out' else e[0]]+=1;right+=1
                counterpart=set(counts)
                if len(counterpart)>=fan_threshold:
                    chosen=edges[i:right]
                    add("fan_out" if direction=="out" else "fan_in","短时拆分候选" if direction=="out" else "短时归集候选",chosen,[node,*counterpart],f"{window_minutes} 分钟内连接 {len(counterpart)} 个不同对端，币种 {currency}。候选结构不表示违法。")
                    break
                peer=first[1] if direction=='out' else first[0];counts[peer]-=1
                if not counts[peer]:del counts[peer]
    by_node=defaultdict(lambda:{"in":[],"out":[]})
    for e in rows:
        if e[0]!=e[1]:
            by_node[e[1]]["in"].append(e); by_node[e[0]]["out"].append(e)
    for node,group in by_node.items():
        found=None
        for incoming in group["in"]:
            candidates=[outgoing for outgoing in group["out"] if outgoing[3]["currency"]==incoming[3]["currency"] and 0<=(parse_time(outgoing[3]["timestamp"])-parse_time(incoming[3]["timestamp"])).total_seconds()<=window_minutes*60 and comparable_pass_amount(incoming[3]['amount'],outgoing[3]['amount'])]
            if candidates:
                found=[incoming,candidates[0]]; break
        if found:
            add("pass_through","短时转入转出候选",found,[found[0][0],node,found[1][1]],f"同币种两笔记录间隔不超过 {window_minutes} 分钟，金额比在 0.5—1.5。没有证明为同一笔资金。")
    for currency in sorted({e[3]["currency"] for e in rows}):
        edges=[e for e in rows if e[3]["currency"]==currency and e[0]!=e[1]]
        simple=nx.DiGraph((u,v) for u,v,k,d in edges)
        if len(simple)>400:
            diagnostics['skipped_cycle_currencies'].append(currency)
            continue
        for at,cycle in enumerate(itertools.islice(nx.simple_cycles(simple,length_bound=4),13)):
            if at==12:diagnostics['truncated_cycle_currencies'].append(currency);break
            pairs=set(zip(cycle,cycle[1:]+cycle[:1]))
            chosen=[e for e in edges if (e[0],e[1]) in pairs]
            add("cycle","回流结构候选",chosen,cycle,f"{currency} 中存在 {len(cycle)} 个地址的闭合连接。此项为静态结构，尚需核对时序和资金归属。")
    from .investigation import network_analysis
    network=network_analysis(graph,window_minutes);diagnostics['fund_findings_truncated']=len(findings)>100;diagnostics['ip_findings_truncated']=network['truncated']
    diagnostics['search_complete']=not any(diagnostics[k] for k in ('skipped_cycle_currencies','truncated_cycle_currencies','fund_findings_truncated','ip_findings_truncated'))
    output=(findings[:100]+network['findings'])[:200];diagnostics['returned_findings']=len(output)
    diagnostics['interpretation']='部分回流分析未执行或候选列表被截断，请缩小案件时间窗口；没有显示的候选不表示不存在' if not diagnostics['search_complete'] else '已执行当前规则范围的搜索；规则本身不覆盖全部异常或违法行为'
    return output


def actor_structure(graph):
    simple=nx.Graph(graph)
    communities=list(nx.community.greedy_modularity_communities(simple)) if simple.number_of_edges() else [{n} for n in simple]
    membership={n:i+1 for i,c in enumerate(communities) for n in c}
    rows=[]
    for node,data in graph.nodes(data=True):
        if data["kind"]!="actor": continue
        neighbours=set(graph.predecessors(node))|set(graph.successors(node))
        rows.append({"node":node,"alias":data["label"],"community":membership.get(node,0),"distinct_neighbours":len(neighbours),"in_degree":graph.in_degree(node),"out_degree":graph.out_degree(node),"recorded_role":data["role"] if data.get("role_verified") else "未核实", "interpretation":"社区与结构连接度不是团伙层级或犯罪角色认定。"})
    return sorted(rows,key=lambda r:r["distinct_neighbours"],reverse=True)


def evidence_chain(dataset,graph,path):
    chain=[]
    for index,(u,v,key) in enumerate(path.get("edges",[]),1):
        d=graph[u][v][key]
        source=dataset.source(graph.graph["case_id"],d["source_id"])
        file=dataset.evidence_file(graph.graph["case_id"],d["source_id"])
        chain.append({"step":index,"from":u,"to":v,"relation":d["relation"],"timestamp":d["timestamp"],"effective_from":d.get("effective_from",d["timestamp"]),"lineage_verified":d.get("lineage_verified",True),"table":d["table"],"record_id":d["record_id"],"csv_line":d["line"],"original_line":d.get("original_line",d["line"]),"original_sha256":d.get("original_sha256",""),"original_sheet":d.get("original_sheet",""),"source_id":d["source_id"],"source_title":source["title"] if source else "", "reference":source["reference"] if source else "", "source_file_present":bool(file),"verification":d.get("verified"),"amount":str(d.get("amount","")),"currency":d.get("currency","")})
    return chain


def lead_list(dataset,graph,findings,identities=None):
    leads=[]
    for finding in findings:
        if finding.get('status')=='已排除':continue
        material="带时区的原始访问日志、源端口、网络域、设备标识及经授权的运营商对应材料；不能仅凭共享IP认定同一人" if finding['kind'] in {'shared_ip','multi_ip'} else "原始交易回执、同币种时序记录、对应业务凭据及控制关系材料"
        leads.append({"lead_id":f"L{len(leads)+1:03d}","question":f"核验 {finding['title']} 的业务背景及实际控制关系", "related_records":finding["record_ids"],"source_ids":finding["source_ids"],"suggested_material":material, "status":"建议核验；非已取得证据"})
    active_identities={d['record_id'] for u,v,k,d in graph.edges(keys=True,data=True) if d['relation']=='identity' and d.get('verified')=='verified'}
    for r in (identities if identities is not None else dataset.rows("identity_links",graph.graph["case_id"])):
        if graph.graph.get("as_of") and parse_time(r['timestamp'])>parse_time(graph.graph["as_of"]):
            continue
        review=r.get('manual_review') or {}
        if review.get('status')=='已排除':continue
        if r['link_id'] not in active_identities:
            leads.append({"lead_id":f"L{len(leads)+1:03d}","question":f"核验主体 {r['actor_id']} 与地址/账户 {r['entity_id']} 的候选关联", "related_records":[r["link_id"]],"source_ids":[r["source_id"]],"suggested_material":"经授权取得的开户/认证资料、控制关系材料及核验记录", "status":"身份未知；不得自动实名绑定"})
    return leads
