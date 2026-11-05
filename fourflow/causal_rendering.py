"""离线阅读的队列研究报告；严格保留条件与个案边界。"""
import html

import networkx as nx


def causal_html(result):
    def e(value):return html.escape(str(value),quote=True)
    def number(value):return '未输出' if value is None else f'{value:.4f}'
    def table(headers,rows):return '<table><thead><tr>'+''.join('<th>'+e(v)+'</th>' for v in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table>'
    graph=nx.DiGraph();graph.add_nodes_from(result['dag']['nodes']);graph.add_edges_from(result['dag']['edges'])
    layers=list(nx.topological_generations(graph));positions={node:(110+index*230,80+offset*100) for index,nodes in enumerate(layers) for offset,node in enumerate(nodes)}
    width=max(600,len(layers)*230);height=max(240,max(map(len,layers))*100+70)
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="声明的因果图" viewBox="0 0 {width} {height}"><defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#64748b"/></marker></defs>'
    for a,b in graph.edges():
        x,y=positions[a];u,v=positions[b];svg+=f'<line x1="{x+65}" y1="{y}" x2="{u-68}" y2="{v}" stroke="#64748b" stroke-width="2" marker-end="url(#a)"/>'
    for node,(x,y) in positions.items():svg+=f'<rect x="{x-65}" y="{y-26}" width="130" height="52" rx="12" fill="#e0edff"/><text x="{x}" y="{y+5}" text-anchor="middle" font-size="12"><title>{e(node)}</title>{e(node[:20])}</text>'
    svg+='</svg>'
    ci=result.get('confidence_interval_95');diag=result['diagnostics'];ref=result.get('refutations',{})
    cards=table(['观测/独立组','未调整差异','AIPW总体效应','近似95%区间'],[[f"{result['n']} / {result['independent_groups']}",number(result['naive_difference']),number(result.get('estimate')),f'{number(ci[0])} 至 {number(ci[1])}' if ci else '未输出']])
    balance=table(['调整变量','调整前标准化差异','IPW后标准化差异'],[[r['variable'],number(r['smd_before']),number(r['smd_after'])] for r in diag['balance']])
    assumptions=table(['识别假设','分析人员声明'],[[k,'已确认' if v is True else '未确认'] for k,v in result['assumptions'].items()])
    history=table(['创建时间','来源指纹','调整变量','估计'],[[r.get('created_at'),r.get('study_fingerprint'),', '.join(r.get('adjustment') or []),number(r.get('estimate'))] for r in result.get('previous_runs',[])])
    warnings=[result.get('reason',''),*result['warnings']];warning='<ul>'+''.join('<li>'+e(v)+'</li>' for v in warnings if v)+'</ul>'
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:"><title>四流系统 · 独立队列因果研究</title><style>body{{font:16px/1.65 'Microsoft YaHei UI',sans-serif;background:#f4f6fa;color:#172b44;margin:0}}main{{max-width:1120px;margin:auto;padding:32px}}section{{background:white;border-radius:12px;padding:24px;margin:20px 0}}h1{{margin:0}}h2{{font-size:21px}}table{{border-collapse:collapse;width:100%;font-size:14px;table-layout:fixed}}th,td{{padding:12px;border-bottom:1px solid #dce4ef;text-align:left;overflow-wrap:anywhere}}th{{background:#eef4fd}}.note{{background:#fff0d7;padding:18px;border-radius:8px}}svg{{width:100%;max-height:600px}}code{{overflow-wrap:anywhere}}</style><main><h1>独立队列因果研究</h1><p>{e(result['question'])}</p><p class="note">{e(result['scope'])}。来源：{e(result['origin'])}。分析人员：{e(result['acknowledged_by'])}。</p><section><h2>估计与适用条件</h2>{cards}<p>{e(result['identification'])}</p><p>{e(result['method'])}</p><p>{e(result.get('interval_method',''))}</p>{warning}</section><section><h2>声明的因果图与假设</h2><p>图中方向来自研究设计；程序检查后门准则，不自动证明图或假设真实。</p>{svg}{assumptions}<p>处理：{e(result['treatment'])}；结果：{e(result['outcome'])}</p></section><section><h2>重叠与协变量平衡</h2><p>倾向分数 {number(diag['propensity_min'])} 至 {number(diag['propensity_max'])}；极端比例 {diag['extreme_fraction']:.1%}；有效样本量：对照 {number(diag['effective_sample_size_control'])}，处理 {number(diag['effective_sample_size_treated'])}。</p>{balance}</section><section><h2>稳健性压力检查</h2><p>置换均值：{number(ref.get('placebo_mean'))}；增加随机协变量后：{number(ref.get('random_covariate_estimate'))}。这些检查不证明无未测混杂。</p>{table(['刻意遗漏变量','改变后的估计','相对主估计变化'],[[r['omitted_variable'],number(r['estimate_without_adjustment_variable']),number(r['change'])] for r in ref.get('leave_one_adjustment_out',[])])}</section><section><h2>版本和来源</h2><p>研究：{e(result['task_id'])}；时间：{e(result.get('created_at',''))}</p><p>研究指纹：<code>{e(result['study_fingerprint'])}</code></p><p>原始来源引用见同目录 sources.csv；原材料未复制。机器可读结果、设计、平衡和交叉拟合分组分别保存在 JSON/CSV 中，校验清单为 checksums.json。</p>{history}</section></main></html>'''
