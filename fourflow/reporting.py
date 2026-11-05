import csv
import hashlib
import html
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

from .analytics import amount_summary,detect_patterns,actor_structure,evidence_chain,lead_list
from .core import TZ,FLOWS,safe_json,is_true,parse_time
from .rendering import graph_svg
from .investigation import network_analysis,industry_traces,pca_projection,behavior_timeline
from .service import identity_states,review_signature
from .model import graph_fingerprint
from .trajectory import behavior_graph,behavior_svg
from . import __version__


def csv_cell(value):
    if isinstance(value,(list,dict)):
        value=json.dumps(value,ensure_ascii=False,default=safe_json)
    if isinstance(value,str) and (value.startswith(("=","+","@","\t","\r")) or (value.startswith("-") and not re.fullmatch(r"-\d+(\.\d+)?",value))):
        return "'"+value
    return value


def write_csv(path,rows,fields):
    with Path(path).open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key:csv_cell(row.get(key,"")) for key in fields} for row in rows)


def build_report(dataset,graph,store,paths=None,assessment=None,analysis_options=None,research=None):
    if hasattr(store,'verify_audit') and not store.verify_audit()['valid']:raise ValueError('审计记录一致性检查未通过，先核对后再导出')
    cid=graph.graph["case_id"]
    if graph.graph.get('dataset_fingerprint')!=dataset.fingerprint:raise ValueError('图谱与数据集版本不一致，请重新分析后导出')
    if assessment and assessment.get('available') and assessment.get('graph_fingerprint')!=graph_fingerprint(graph):raise ValueError('模型评分与图谱快照不一致，请重新分析后导出')
    options={'network_window_minutes':30,'industry_window_hours':168,**(analysis_options or {})}
    pattern_diagnostics={};findings=detect_patterns(graph,diagnostics=pattern_diagnostics)
    reviews=store.reviews(dataset.fingerprint,cid,graph.graph.get('as_of') or None)
    signature=graph.graph.get('review_signature')
    if (signature and signature!=review_signature(reviews)) or (reviews and not signature):raise ValueError('核验状态在图谱生成后发生变化，请刷新分析后导出，避免混用新旧结论')
    for path in paths or []:
        if any(not graph.has_edge(u,v,k) for u,v,k in path.get('edges',[])):raise ValueError('所选路径与当前图谱不一致，请重新查询')
    statuses={r["finding_id"]:r for r in reviews}
    for item in findings:
        if item["id"] in statuses:
            item["review"]=statuses[item["id"]]
            item["status"]=statuses[item["id"]]["status"]
    identities=identity_states(dataset,graph,store)
    cutoff=graph.graph.get("as_of")
    if cutoff:
        identities=[r for r in identities if parse_time(r["timestamp"])<=parse_time(cutoff)]
    scenario=store.scenario(dataset.fingerprint,cid)
    if scenario and scenario.get("scope_as_of","")!=(cutoff or ""):
        scenario={"available":False,"reason":"此前金额情景对应不同的截至时间，请在当前快照重新填写假设。"}
    network=network_analysis(graph,options['network_window_minutes'])
    findings=[f for f in findings if f['kind'] not in {'shared_ip','multi_ip'}]+network['findings']
    for finding in findings:
        if finding['id'] in statuses:finding.update(review=statuses[finding['id']],status=statuses[finding['id']]['status'])
    traces=industry_traces(dataset,graph,options['industry_window_hours'])
    leads=lead_list(dataset,graph,findings,identities)
    for trace in traces:
        if trace['missing']:leads.append({'lead_id':f'L{len(leads)+1:03d}','question':'核验产业订单 '+trace['order'],'related_records':[s['table']+':'+s['record_id'] for s in trace['steps']],'source_ids':trace['source_ids'],'suggested_material':'；'.join(trace['missing']),'status':'建议补充核验；非已取得证据'})
    result={"system":"反电诈四流辅助侦查系统","version":"1.3.0","created_at":datetime.now(TZ).isoformat(),"case":next(r for r in dataset.tables["cases"] if r["case_id"]==cid),"origin":dataset.manifest["origin"],"dataset_fingerprint":dataset.fingerprint,"as_of":cutoff or "全部已导入记录","graph":{"nodes":graph.number_of_nodes(),"edges":graph.number_of_edges(),"flows":{flow:sum(d["flow"]==flow for u,v,k,d in graph.edges(keys=True,data=True)) for flow in FLOWS}},"amounts":amount_summary(graph),"principal_scenario":scenario,"findings":findings,"actor_structure":actor_structure(graph),"identity_links":identities,"evidence_chains":[evidence_chain(dataset,graph,p) for p in (paths or [])],"network_analysis":network,"analysis_options":options,"industry_traces":traces,"pca":pca_projection(graph) if 0<len(graph)<=256 else {'available':False,'reason':'PCA仅支持1至256节点的当前快照'},"cleaning_report":str(dataset.root/'cleaning_report.json') if (dataset.root/'cleaning_report.json').is_file() else '',"model_assessment":assessment or {"available":False,"reason":"尚未加载模型"},"leads":leads,"reviews":reviews,"sources":dataset.rows("sources",cid),"boundaries":["全部输出用于辅助核验，违法认定由有权人员根据完整材料作出。","关联路径及删除特征后的分数变化不是因果证明。","共享IP不等于同一人员，不进行无来源地理定位。产业排序不等于违法概率。","图谱只覆盖导入数据；未知身份保持未知；候选身份不作为已核实绑定。","交易额不等于涉案本金；金额区间仅在人工提供覆盖率假设后输出。","图模型为小规模本地实现，合成指标不能替代真实案件评估。"]}

    research=research or {}
    result['pattern_diagnostics']=pattern_diagnostics
    roles=research.get('roles') or {'available':False,'reason':'尚未载入角色层级模型'}
    if roles.get('available') and (roles.get('graph_fingerprint')!=graph_fingerprint(graph) or roles.get('dataset_fingerprint')!=dataset.fingerprint):raise ValueError('角色层级预测与图谱快照不一致，请刷新后导出')
    if roles.get('available') and roles.get('origin')=='synthetic' and not dataset.synthetic:raise ValueError('合成角色预测不能附到真实案件报告')
    cohort=research.get('cohort_causal')
    if cohort and (cohort.get('case_linked') is not False or cohort.get('result',{}).get('origin')!=dataset.manifest['origin']):raise ValueError('独立队列附件范围或来源不匹配，不能作为当前个案因果结论')
    joint=research.get('joint')
    if joint and joint.get('available'):
        from .hd_eia import auxiliary
        if joint.get('origin')!=dataset.manifest['origin'] or joint.get('dataset_fingerprint')!=dataset.fingerprint or joint.get('graph_fingerprint')!=graph_fingerprint(graph) or joint.get('aux_fingerprint')!=auxiliary(dataset)['fingerprint']:raise ValueError('联合研判与当前数据、媒体或图谱快照不一致')
    result.update(role_hierarchy=roles,cohort_causal=cohort,joint_analysis=joint)
    from .feature_audit import audit_features
    result['feature_formula_audit']=audit_features(dataset,cid,cutoff)
    result['version']=__version__
    return result



def export_report(dataset,graph,store,output_root,paths=None,assessment=None,analysis_options=None,research=None):
    report=build_report(dataset,graph,store,paths,assessment,analysis_options,research)
    report['behavior_timeline']=behavior_timeline(dataset,graph)
    report['behavior_graph']=behavior_graph(graph)
    cid=graph.graph["case_id"]
    name=f"{cid}_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S_%f')}"
    destination=Path(output_root)/name
    destination.mkdir(parents=True,exist_ok=False)
    json_path=destination/"analysis.json"
    json_path.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=safe_json),encoding="utf-8")
    (destination/'feature_formula_audit.json').write_text(json.dumps(report['feature_formula_audit'],ensure_ascii=False,indent=2),encoding='utf-8')
    highlight=(paths or [{}])[0]
    svg=graph_svg(graph,highlight)
    (destination/"graph.svg").write_text(svg,encoding="utf-8")
    (destination/'behavior_graph.json').write_text(json.dumps(report['behavior_graph'],ensure_ascii=False,indent=2),encoding='utf-8')
    event_svg=behavior_svg(report['behavior_graph']);(destination/'behavior_graph.svg').write_text(event_svg,encoding='utf-8')
    write_csv(destination/"findings.csv",report["findings"],["id","kind","title","nodes","record_ids","source_ids","detail","status","review"])
    write_csv(destination/"leads.csv",report["leads"],["lead_id","question","related_records","source_ids","suggested_material","status"])
    write_csv(destination/"identity_links.csv",report["identity_links"],["link_id","actor_id","entity_kind","entity_id","chain","timestamp","verified","effective_verified","manual_review","verification_note","source_id"])
    write_csv(destination/"evidence_chain.csv",[row for chain in report["evidence_chains"] for row in chain],["step","from","to","relation","timestamp","effective_from","lineage_verified","table","record_id","csv_line","original_line","original_sha256","original_sheet","source_id","source_title","reference","source_file_present","verification","amount","currency"])
    write_csv(destination/"amounts.csv",report["amounts"]["addresses"],["node","currency","incoming","outgoing","net"])
    write_csv(destination/'network_events.csv',report['network_analysis']['observations'],['record_id','ip','scope','timestamp','anchors','device_nodes','source_port','source_id','csv_line','original_line'])
    write_csv(destination/'industry_traces.csv',report['industry_traces'],['order','service_types','flows','actor_nodes','account_nodes','priority_score','pareto_front','objectives','weights','missing','source_ids','source_group_count','source_independence_verified'])
    write_csv(destination/'industry_evidence.csv',[{'order':t['order'],**step} for t in report['industry_traces'] for step in t['steps']],['order','timestamp','effective_from','lineage_verified','flow','association','table','record_id','source_id','csv_line','original_line','source_title','reference'])
    write_csv(destination/'pca.csv',report['pca'].get('points',[]),['node','pc1','pc2'])
    write_csv(destination/'behavior_timeline.csv',report['behavior_timeline'],['timestamp','effective_from','lineage_verified','flow','table','record_id','nodes','source_id','source_title','csv_line','original_line','original_sha256','verification'])
    role_result=report['role_hierarchy']
    write_csv(destination/'person_predictions.csv',role_result.get('predictions',[]),['actor_id','alias','role','level','prediction_available','abstention_reasons','support_records','support_flows','feature_shift','candidate_role','candidate_level','role_probability','level_probability','joint_probability','allowed_mass','incompatible_heads','low_confidence','status'])
    write_csv(destination/'hierarchy_candidates.csv',role_result.get('hierarchy_links',[]),['upper','lower','record_ids','source_ids','status'])
    from .research_rendering import hierarchy_svg
    hierarchy=hierarchy_svg(role_result)
    (destination/'hierarchy.svg').write_text(hierarchy,encoding='utf-8')
    if report.get('joint_analysis'):
        joint=report['joint_analysis'];(destination/'joint_analysis.json').write_text(json.dumps(joint,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        write_csv(destination/'joint_control_candidates.csv',joint.get('control_candidates',[]),['from','to','score','evidence','status'])
    if report.get('cohort_causal'):
        (destination/'independent_cohort_causal.json').write_text(json.dumps(report['cohort_causal'],ensure_ascii=False,indent=2),encoding='utf-8')
        from .causal_rendering import causal_html
        (destination/'independent_cohort_causal.html').write_text(causal_html(report['cohort_causal']['result']),encoding='utf-8')
    role_rows=[{**r,'score_text':f"{r['joint_probability']:.1%}" if r.get('prediction_available',True) else '暂不输出','status':r['status']+('；'+'；'.join(r.get('abstention_reasons',[])) if r['low_confidence'] else '')} for r in role_result.get('predictions',[])]
    role_version='模型：'+role_result.get('model_id','未加载')+'；来源：'+role_result.get('origin','未知')
    if role_result.get('known_split') or role_result.get('related_splits'):role_version+='；当前图或资产与训练/评估集合重合，不能作为独立外部验证。'
    esc=lambda value:html.escape(str(value),quote=True)
    def table(rows,columns):
        if not rows:return '<p class="muted">暂无记录。</p>'
        header=''.join(f'<th>{esc(label)}</th>' for key,label in columns)
        body=''.join('<tr>'+''.join(f'<td>{esc(row.get(key,""))}</td>' for key,label in columns)+'</tr>' for row in rows)
        return f'<div class="scroll"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>'
    totals=' / '.join(f'{esc(currency)} {esc(amount)}' for currency,amount in report["amounts"]["gross_by_currency"].items())
    banner="合成数据验证报告：所有主体、事件和标签均为虚构，不代表真实案件或识别能力。" if dataset.synthetic else "导入数据分析报告：来源与核验状态详见各记录。"
    model_text=json.dumps(report["model_assessment"],ensure_ascii=False,indent=2)
    scenario=report["principal_scenario"]
    scenario_text=esc(json.dumps(scenario,ensure_ascii=False,indent=2)) if scenario else "未填写经核实的去重本金、覆盖率假设及来源，未输出涉案金额区间。"
    chains=''.join(table(chain,[("step","步骤"),("from","起点"),("to","终点"),("relation","关系"),("timestamp","时间"),("record_id","记录"),("source_id","来源"),("csv_line","CSV行")]) for chain in report["evidence_chains"]) or '<p>未选择待核验路径。</p>'
    doc=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{esc(cid)} 四流关联分析报告</title>
<style>body{{font-family:"Microsoft YaHei",Arial,sans-serif;max-width:1180px;margin:36px auto;padding:0 28px;color:#1e293b;line-height:1.75;background:#fff}}h1{{font-size:28px;margin-bottom:8px}}h2{{font-size:20px;margin-top:34px}}.meta,.muted{{color:#64748b;font-size:13px}}.banner{{padding:15px 18px;background:#fff7ed;border-radius:8px;margin:22px 0}}.cards{{display:flex;gap:16px;flex-wrap:wrap}}.card{{background:#f1f5f9;padding:14px 20px;border-radius:8px;min-width:110px}}.card strong{{display:block;font-size:24px}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{padding:9px;border:1px solid #dbe2ea;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{background:#eef2f7}}.scroll{{overflow:auto}}svg{{width:100%;height:auto}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f8fafc;padding:16px;font-size:12px}}@media print{{body{{margin:0;max-width:none}}h2{{break-after:avoid}}tr{{break-inside:avoid}}}}</style></head><body>
<h1>四流关联分析报告</h1><div class="meta">{esc(cid)} · {esc(report['case']['title'])} · {esc(report['created_at'])}</div>
<div class="banner">{esc(banner)}</div><div class="cards"><div class="card">图谱节点<strong>{len(graph)}</strong></div><div class="card">关联边<strong>{graph.number_of_edges()}</strong></div><div class="card">待核验模式<strong>{len(report['findings'])}</strong></div></div>
<p>分析时间范围：{esc(report['as_of'])}。交易额合计：{totals}。</p><p class="muted">{esc(report['amounts']['interpretation'])}</p>
<h2>四流关联图谱</h2>{svg}
<h2>可追溯时序关联链</h2><p class="muted">按记录连接和时间顺序组织，不表示已经证明因果关系或同一笔资金归属。</p>{chains}
<h2>事件行为轨迹图</h2><p class="muted">这是独立于实体图的事件关系图。箭头仅表示同一实体在窗口内先后出现，不等于因果或同一笔资金。</p>{event_svg}
<h2>模式及核验状态</h2><p class="muted">{esc(report['pattern_diagnostics']['interpretation'])}</p>{table(report['findings'],[("id","编号"),("title","模式"),("detail","依据与限制"),("record_ids","相关记录"),("source_ids","来源"),("review","人工核验")])}
<h2 id="network">IP网络观测与关联</h2><p class="muted">{esc(report['network_analysis']['interpretation'])}</p>{table(report['network_analysis']['ips'],[('ip','IP地址'),('scope','网络域'),('event_count','观测数'),('anchor_count','主体及账户数'),('first_seen','首次时间'),('last_seen','末次时间')])}
<h2 id="industry">资金 信息 人员关联产业溯源</h2><p class="muted">使用同订单付款、产业记录及窗口内的主体背景记录。窗口{esc(report['analysis_options']['industry_window_hours'])}小时，IP分析窗口{esc(report['analysis_options']['network_window_minutes'])}分钟；优先级=四流覆盖50%+来源材料组多样性25%（不等于已验证来源独立性）+时间接近度25%，不是违法概率。帕累托前沿仅表示多目标排序。细节见 industry_evidence.csv 和 analysis.json。</p>{table(report['industry_traces'],[('order','产业订单'),('service_types','来源类别'),('flows','关联流'),('priority_score','核验优先级'),('pareto_front','前沿'),('missing','证据缺口')])}
<h2>清洗与特征探索</h2><p>清洗记录：{esc(report['cleaning_report'] or '未执行修复，仅严格校验')}。原始行号随记录保留。PCA仅用于当前快照探索，不用于身份合并或标签生成。</p>
<h2>主体结构与已记录角色</h2>{table(report['actor_structure'],[("alias","主体"),("community","结构社区"),("distinct_neighbours","不同邻居数"),("recorded_role","已核实记录角色"),("interpretation","解释")])}
<h2 id="roles">人员角色与层级模型候选</h2><p>{esc(role_version)}</p><p class="muted">{esc(role_result.get('interpretation',role_result.get('reason','尚未载入角色模型')))}</p>{hierarchy}{table(role_rows,[('alias','主体'),('role','候选角色'),('level','候选层级'),('score_text','允许组合分数'),('status','核验状态')])}
<h2>独立队列因果研究附件</h2><p class="muted">{esc((report.get('cohort_causal') or {}).get('scope','未附加独立队列研究。本报告中的图谱关联不是因果证明。'))}</p>
<h2>双图与原始多模态联合研判</h2><p>{esc((report.get('joint_analysis') or {}).get('scope','本次未运行联合模型。'))}</p>{'<p><a href="joint_analysis.json">联合分数、控制方向候选、四流解释及多目标核验排序</a></p>' if report.get('joint_analysis') else ''}
{'<p><a href="independent_cohort_causal.html">查看附加的独立队列研究及识别条件</a></p>' if report.get('cohort_causal') else ''}
<h2>身份关联记录</h2>{table(report['identity_links'],[("actor_id","主体编号"),("entity_id","地址或账户"),("verified","原始核实标记"),("effective_verified","当前有效核实"),("manual_review","人工核验"),("verification_note","来源说明"),("source_id","来源")])}
<h2>本金覆盖率情景估计</h2><pre>{scenario_text}</pre>
<h2>图模型辅助分析</h2><pre>{esc(model_text)}</pre>
<h2>建议补充核验的材料</h2>{table(report['leads'],[("lead_id","编号"),("question","核验问题"),("related_records","相关记录"),("source_ids","已有来源"),("suggested_material","建议材料"),("status","状态")])}
<h2>来源清单</h2>{table(report['sources'],[("source_id","来源编号"),("title","名称"),("reference","原始材料引用")])}
<h2>使用边界</h2><ul>{''.join('<li>'+esc(x)+'</li>' for x in report['boundaries'])}</ul>
<p class="meta">数据指纹 SHA-256：{esc(dataset.fingerprint)}。报告包含的原始来源文件仅通过引用定位，未自动复制或对外发送。</p></body></html>'''
    from .feature_audit import readable_audit
    doc=doc.replace('<h2>主体结构与已记录角色</h2>','<h2>观测特征与示例公式核验</h2><p><a href="feature_formula_audit.json">完整逐项计算、缺失值及定义</a></p><pre>'+html.escape(readable_audit(report['feature_formula_audit']))+'</pre><h2>主体结构与已记录角色</h2>')
    (destination/"report.html").write_text(doc,encoding="utf-8")
    hashes={file.name:hashlib.sha256(file.read_bytes()).hexdigest() for file in destination.iterdir() if file.is_file()}
    (destination/"checksums.json").write_text(json.dumps(hashes,indent=2),encoding="utf-8")
    archive=destination.with_suffix(".zip")
    with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as z:
        for file in destination.iterdir():z.write(file,file.name)
    store.audit("report_exported",{"dataset":dataset.fingerprint,"case_id":cid,"path":str(destination),"origin":dataset.manifest["origin"]})
    return destination
