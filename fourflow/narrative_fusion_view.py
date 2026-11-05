"""Desktop and offline report views for quality, IP observations and industry candidates."""
import html
from .narrative_extract import FLOW_NAMES
from .narrative_reconstruction_view import source_name

e=lambda value:html.escape(str(value),quote=True)
TIME_NAMES={'time':'明确时刻','day':'日','unknown':'未知/粗粒度'}


def text_view(case):
    f=case['fusion'];q=f['quality'];n=f['network'];industry=f['industry']
    lines=[case['title'],'四流关联与产业溯源（候选）',f['scope'],'',f"原行 {q['input_rows']}；参与解析 {q['active_rows']}；人工排除 {q['excluded_rows']}；公式隔离 {q['formula_rows']}",f"字段冲突 {q['conflicting_rows']} 行；无效IP {q['invalid_ip_rows']} 行；重复内容候选 {len(q['duplicate_candidate_groups'])} 组（未删原行）",'','四流输入条件：']
    for c in f['coverage']:lines+=['• '+c['name']+'：来源内容 '+str(c['source_rows'])+' 行，具备专门字段的观测 '+str(c['field_observations'])+' 行', '  需要：'+c['required']]
    lines+=['','IP观测与变化：',f"可用于时间窗的IP记录 {n['usable_observations']} 条；IP观测组 {len(n['ips'])} 组；共享/切换候选 {len(n['findings'])} 条",n['scope']]
    for finding in n['findings']:lines+=['• '+finding['title']+' '+finding['ip'],finding['detail'],'  原行 '+', '.join(finding['record_ids'])]
    lines+=['','产业关联候选 '+str(len(industry['traces']))+' 条；超限未展示 '+str(industry['omitted_traces'])+' 条',industry['scope']]
    for t in industry['traces']:
        lines+=['','• '+t['kind']+' / '+(t['order_id'] or '无明确订单')+' / '+'、'.join(FLOW_NAMES.get(k,k) for k in t['flows'])]
        if t['goods']:lines.append('  物品/服务：'+'、'.join(t['goods']))
        for item in t['evidence']:lines+=['  '+item['basis'],'  '+item['locator']+' / '+item['source_citation']+' / '+(item['time']['raw'] or '时间未知')]
        for check in t['temporal_checks']:lines.append('  时间检查：'+check['status'])
        lines.extend('  待核验：'+m for m in t['missing'])
        if t['omitted_evidence']:lines.append('  未展开原行 '+str(t['omitted_evidence'])+' 条；完整编号见JSON')
    lines+=['','质量问题原行 '+str(len(q['warnings']))+' 条；完整清单在报告和data_quality.csv。']
    for w in q['warnings'][:30]:lines.append(w['locator']+'：'+'；'.join(w['issues']))
    return '\n'.join(lines)


def html_view(case,table):
    f=case['fusion'];q=f['quality'];n=f['network'];industry=f['industry'];cid=case['case_id']
    link=lambda rid:'<a href="source_rows.html#'+e(rid)+'">原行</a>'
    coverage=table(['流别','来源内容行','专门字段观测行','输入条件'],[(e(c['name']),c['source_rows'],c['field_observations'],e(c['required'])) for c in f['coverage']])
    quality=table(['问题原行','问题与处理'],[(link(w['record_id'])+' '+e(w['locator']),'<br>'.join(e(m) for m in w['issues'])) for w in q['warnings']])
    ips=table(['IP / 网络域 / 标识域','观测数','明确时刻（UTC）','来源'],[(e(p['ip']+' / '+('公网' if p['network_scope']=='global' else p['network_scope'])+' / '+source_name(p['identifier_scope'])),p['event_count'],e(p['first_seen']+' ～ '+p['last_seen']),' '.join(link(rid) for rid in p['record_ids'])) for p in n['ips']])
    findings=table(['网络候选','说明','来源'],[(e(p['title']+' '+p['ip']),e(p['detail']),' '.join(link(rid) for rid in p['record_ids'])) for p in n['findings']])
    traces=[]
    for t in industry['traces']:
        evidence=table(['连接依据','原文时间 / 精度','来源定位'],[(e(p['basis']),e((p['time']['raw'] or '未知')+' / '+TIME_NAMES.get(p['time']['precision'],p['time']['precision'])),link(p['record_id'])+' '+e(p['locator']+' / '+p['source_citation'])) for p in t['evidence']])
        checks='；'.join(c['status'] for c in t['temporal_checks']) or '无适用的同订单付款时间检查'
        traces.append('<article><h4>'+e(t['kind']+' / '+(t['order_id'] or '无明确订单'))+'</h4><p>'+e('关联流别：'+'、'.join(FLOW_NAMES.get(k,k) for k in t['flows'])+'；物品/服务：'+('、'.join(t['goods']) or '原文路径内查看'))+'</p>'+evidence+'<p>'+e('时间检查：'+checks)+'</p><ul>'+''.join('<li>'+e(m)+'</li>' for m in t['missing'])+'</ul>'+('<p>尚有'+str(t['omitted_evidence'])+'条原行未展开，完整编号见JSON。</p>' if t['omitted_evidence'] else '')+'</article>')
    return '<section id="'+cid+'_fusion"><h2>'+e(case['title'])+'：四流关联与产业溯源</h2><aside>'+e(f['scope'])+'</aside><h3>数据质量与输入覆盖</h3><p>'+e(f"原行{q['input_rows']}；参与解析{q['active_rows']}；人工排除{q['excluded_rows']}；公式隔离{q['formula_rows']}；字段冲突{q['conflicting_rows']}行；无效IP{q['invalid_ip_rows']}行；重复内容候选{len(q['duplicate_candidate_groups'])}组")+'</p><p>'+e(q['duplicate_policy'])+'</p>'+coverage+'<h3>IP记录与网络关联</h3><p>'+e(n['scope'])+'；可用于窗口的记录 '+str(n['usable_observations'])+' 条。</p>'+ips+findings+'<h3>产业关联候选</h3><p>'+e(industry['scope'])+'；候选组 '+str(industry['total_candidate_groups'])+'，超限未展示 '+str(industry['omitted_traces'])+'。</p>'+(''.join(traces) or '<p>当前没有具备产业字段或明确产业路径的记录；补充订单/运单和交付材料后再分析。</p>')+'<h3>数据质量问题与来源</h3>'+quality+'</section>'
