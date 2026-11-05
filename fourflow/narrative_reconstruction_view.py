"""Readable source reconstruction views, with all calculations linked to source rows."""
import html
from decimal import Decimal

e=lambda v:html.escape(str(v),quote=True)
money=lambda v:format(Decimal(v),',f')
source_name=lambda v:(v or '案件内提及').replace('doc_','文书').replace('mixed_','混合出处').replace('unmarked','未标注来源')


def text_view(case):
    hierarchy=case['hierarchy_reconstruction'];events=case['statement_events']
    lines=[case['title'],'结构还原与核算 · 原文陈述，全部待核验','', '来源隔离：匿名编号按文书区分；同源文书不算独立印证。']
    for d in case['source_scopes']['same_source_declarations']:lines.append('同源说明：'+d['statement'])
    lines+=['','显式方向与局部层次：']
    for edge in hierarchy['edges']:lines+=['• '+edge['from_label']+' → '+edge['to_label']+' / '+edge['relation'], '  “'+edge['phrase']+'” / '+edge['locator']+' / '+edge['source_citation']]
    if not hierarchy['edges']:lines.append('没有符合明示关系规则的记录；不按角色称谓自动生成上下级。')
    lines.append('循环冲突 '+str(len(hierarchy['cycles']))+' 组；未定位关系 '+str(len(hierarchy['unresolved']))+' 项。未连接者不推定为首脑。')
    lines+=['','列示项目核算（不是全案损失）：']
    for amount in case['amount_reconstruction']:
        lines += [source_name(amount['source_namespace'])+' / '+amount['currency']+' / '+str(amount['listed_item_count'])+'项', '名义合计 '+money(amount['listed_nominal_total'])+'；银行支付 '+(money(amount['nominal_subtotals']['wire_statement']) if 'wire_statement' in amount['nominal_subtotals'] else '无')+'；加密资产法币等值 '+(money(amount['nominal_subtotals']['crypto_valuation_statement']) if 'crypto_valuation_statement' in amount['nominal_subtotals'] else '无')]
        interval=amount['conditional_distinct_transfer_interval']
        if 'account_payment_statement' in amount['nominal_subtotals']:lines.append('账户支付陈述（类型待核） '+money(amount['nominal_subtotals']['account_payment_statement']))
        lines += ['条件去重区间：'+money(interval['lower'])+' ～ '+money(interval['upper']), *['  假设：'+v for v in interval['assumptions']]] if interval else ['条件区间不可给出：'+amount['interval_unavailable_reason']]
    if not events:lines.append('当前资料没有可定位付款方、收款方、明确金额的适用明细；没有生成零损失结论。')
    lines+=['','支付明细与日期对应：']
    for event in events:lines += ['• '+event['sender_ref']+' → '+event['recipient_ref']+' / '+money(event['amount'])+' '+event['valuation_currency'], '  '+(event['date_start'] or '未知')+' ～ '+(event['date_end'] or '未明确')+' / '+event['alignment'], '  '+event['amount_semantics']+'；原行 '+event['locator']+'；'+event['source_citation']]
    lines+=['','假设与证据缺口：']
    for h in case['evidence_hypotheses']:lines += [h['hypothesis']+'：支持原行 '+str(len(set(h['supports'])))+' 条；未识别因果效应', *['  待取得/核验：'+v for v in h['remaining']]]
    return '\n'.join(lines)


def html_view(case,table):
    link=lambda rid:'<a href="source_rows.html#'+e(rid)+'">原行</a>'
    edges=case['hierarchy_reconstruction']['edges']
    relations=table(['方向候选','原文/关系','文书范围','出处'],[(e(t['from_label'])+' → '+e(t['to_label']),e(t['phrase']+' / '+t['relation']),e(source_name(t['source_namespace'])),link(t['record_id'])+' '+e(t['source_citation'])) for t in edges])
    subtypes={'wire_statement':'银行支付','crypto_valuation_statement':'加密资产法币等值','account_payment_statement':'账户支付陈述（类型待核）'}
    amounts=table(['来源/币种','项目数及名义合计','分类名义合计','条件去重区间/假设'],[(e(source_name(t['source_namespace'])+' / '+t['currency']),e(str(t['listed_item_count'])+'项 / '+money(t['listed_nominal_total'])),'<br>'.join(e(subtypes[k]+'：'+money(v)) for k,v in t['nominal_subtotals'].items()), e(t['interval_unavailable_reason']) if not t['conditional_distinct_transfer_interval'] else e(money(t['conditional_distinct_transfer_interval']['lower'])+' ～ '+money(t['conditional_distinct_transfer_interval']['upper']))+'<br>'+'<br>'.join(e(v) for v in t['conditional_distinct_transfer_interval']['assumptions'])) for t in case['amount_reconstruction']])
    events=table(['付款引用 → 收款引用','原表金额与口径','日期边界/对应依据','来源'],[(e(t['sender_ref'])+' → '+e(t['recipient_ref']),e(money(t['amount'])+' '+t['valuation_currency'])+'<br>'+e(t['amount_semantics']),e((t['date_start'] or '未知')+' ～ '+(t['date_end'] or '未明确'))+'<br>'+e(t['alignment'])+'<br>'+e('省略部分：'+(','.join({'year':'年','month':'月'}[p] for p in t['date_inherited_parts']) or '无')),link(t['record_id'])+' '+e(source_name(t['source_namespace']))+' '+e(t['source_citation'])) for t in case['statement_events']])
    evidence=table(['假设','支持来源','结构冲突','尚需核验'],[(e(t['hypothesis']),' '.join(link(rid) for rid in dict.fromkeys(t['supports'])) or '无适用明细',e(str(t['contradictions'])),'<br>'.join(e(v) for v in t['remaining'])) for t in case['evidence_hypotheses']])
    diagram=hierarchy_svg(case)
    return '<section id="'+case['case_id']+'_reconstruction"><h2>'+e(case['title'])+'：结构还原与核算</h2><aside>以下为可回溯的来源陈述及条件计算；没有核实为现实控制关系，也没有形成全案损失或因果证明。匿名编号按文书隔离，同源材料不重复计算印证。</aside><h3>显式方向与局部层次</h3>'+diagram+relations+'<p>循环冲突 '+str(len(case['hierarchy_reconstruction']['cycles']))+' 组。未连接者不推定为首脑，循环时不强制排序。</p><h3>源表列示项目核算</h3>'+amounts+'<p>空表表示无适用明细，不表示零损失。名义总和保留源文法币等值，不将其当本金、全案上界或统计置信区间。</p><h3>支付明细引用网络</h3>'+events+'<h3>假设与证据缺口审计</h3>'+evidence+'</section>'


def hierarchy_svg(case):
    hierarchy=case['hierarchy_reconstruction'];levels=hierarchy['partial_levels'];nodes=[n for n in hierarchy['nodes'] if n['id'] in levels]
    if not nodes:return '<p>没有可排序的局部关系；详见来源角色表和循环记录。</p>'
    groups={level:[n for n in nodes if levels[n['id']]==level] for level in sorted(set(levels.values()))};width=max(920,260*max(map(len,groups.values())));height=100+130*len(groups);positions={}
    for level,items in groups.items():
        for i,node in enumerate(items):positions[node['id']]=(width*(i+1)/(len(items)+1),65+level*130)
    marker='arrow_'+case['case_id'];out=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="原文关系局部层次"><defs><marker id="{marker}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10" fill="#7189aa"/></marker></defs>']
    for edge in hierarchy['edges']:
        x,y=positions[edge['from_actor']];xx,yy=positions[edge['to_actor']];out.append(f'<a href="source_rows.html#{edge["record_id"]}"><path d="M{x},{y+25} L{xx},{yy-25}" stroke="#7189aa" stroke-width="2" marker-end="url(#{marker})"/><title>{e(edge["phrase"]+" / "+edge["locator"])}</title></a>')
    for node in nodes:
        x,y=positions[node['id']];out.append(f'<rect x="{x-115}" y="{y-25}" width="230" height="50" rx="8" fill="#eaf1fb" stroke="#adc1db"/><text x="{x}" y="{y+5}" text-anchor="middle" font-size="14">{e(node["label"][:24])}</text>')
    return ''.join(out)+'</svg>'
