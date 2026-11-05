"""可离线复核的案件文本研判报告，含原件、原行、字段和提取结果。"""
from pathlib import Path
from datetime import datetime
import csv,hashlib,html,json,math,re,shutil,zipfile
from .core import TZ
from .io_utils import atomic_json
from .narrative_analysis import analyze
from .narrative_extract import FLOW_NAMES
from .narrative_reconstruction_view import html_view,hierarchy_svg,source_name
from .narrative_fusion_view import html_view as fusion_html
from .narrative_fields import field_view

STYLE="body{font:15px/1.8 'Microsoft YaHei UI',sans-serif;max-width:1240px;margin:28px auto;padding:24px;color:#23334d;background:#f3f6fa}h1{font-size:28px}h2{font-size:21px;margin-top:28px}section{background:white;padding:24px;margin:18px 0;border-radius:12px}aside{background:#fff1d9;padding:18px;border-radius:10px}table{border-collapse:collapse;width:100%;font-size:13px;background:white}td,th{padding:10px;border-bottom:1px solid #dce4ee;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#eaf0f8}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.8 'Microsoft YaHei UI',sans-serif}a{color:#1d4ed8}svg{max-width:100%;height:auto}.scroll{overflow-x:auto}.muted{color:#596d88}.cards{display:flex;gap:14px;flex-wrap:wrap}.card{background:#eaf1fb;padding:14px 22px;border-radius:8px}"
ENTITY_NAMES={'person_mention':'主体称谓','industry_mention':'产业/机构称谓','wallet':'完整钱包文本','wallet_prefix':'钱包前缀','wallet_fragment':'掩码钱包片段','domain':'域名','ip':'IP提及','coordinate':'原文坐标'}
STYLE+=' .report-nav{position:sticky;top:0;z-index:10;display:flex;flex-wrap:wrap;gap:12px;padding:12px 18px;background:#fff;border-radius:8px;box-shadow:0 2px 9px #23334d1a}.case-links{display:flex;flex-wrap:wrap;gap:12px}.case-entry{border-left:4px solid #2563eb}h1,h2,section,[id]{scroll-margin-top:90px}nav a{overflow-wrap:anywhere}@media(max-width:800px){body{margin:12px auto;padding:12px}section{padding:16px}.report-nav{padding:10px 12px}}@media print{.report-nav{display:none}}'
PRECISION={'time':'明确时刻','invalid_time':'时刻格式异常','day':'日','month':'月','year':'年','range':'区间','partial_range':'省略/歧义区间末端','multiple_dates':'多个日期，未合并为区间','invalid_order':'日期次序异常','unknown':'未知'}
CHECKSUM_NAMES={'valid':'编码校验通过；归属待核验','invalid':'编码校验未通过，优先核对原件','not_checked':'未校验此格式编码'}
e=lambda value:html.escape(str(value),quote=True)


def graph_svg(case,max_entities=22,max_records=35):
    entities=sorted(case['entities'],key=lambda r:(-len(r['record_ids']),r['label']))[:max_entities];entity_ids={r['id'] for r in entities};links=[r for r in case['mention_links'] if r['entity_id'] in entity_ids];records=list(dict.fromkeys(r['record_id'] for r in links))[:max_records];record_set=set(records);links=[r for r in links if r['record_id'] in record_set]
    height=max(350,60+max(len(entities),len(records))*33);positions={r['id']:(220,60+i*33) for i,r in enumerate(entities)};positions.update({rid:(780,60+i*33) for i,rid in enumerate(records)})
    out=[f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="{height}" viewBox="0 0 1000 {height}" role="img" aria-label="材料提及图"><rect width="1000" height="{height}" fill="white"/><text x="22" y="25" font-size="15">主体/资产提及 → 来源记录；连线不表示控制或转账</text>']
    for link in links:
        x,y=positions[link['entity_id']];xx,yy=positions[link['record_id']];out.append(f'<path d="M{x+12},{y} C450,{y} 550,{yy} {xx-12},{yy}" fill="none" stroke="#b7c8df" stroke-width="1"/>')
    for ent in entities:
        x,y=positions[ent['id']];out.append(f'<circle cx="{x}" cy="{y}" r="7" fill="#2563eb"/><text x="22" y="{y+4}" font-size="12">{e(ent["label"][:16])}</text>')
    lookup={r['record_id']:r['locator'] for r in links}
    for rid in records:
        x,y=positions[rid];out.append(f'<a href="source_rows.html#{rid}"><circle cx="{x}" cy="{y}" r="6" fill="#d97706"/><text x="795" y="{y+4}" font-size="11">{e(lookup[rid][:23])}</text></a>')
    return ''.join(out)+'</svg>'


def _table(headers,rows):return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+e(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+str(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'


def _time_label(time):
    if time['precision']=='time':return (time.get('instant') or time['raw'])+' / '+('明确时刻（UTC）' if time.get('instant') else '明确时刻，时区未声明')
    return (time['start'] or '未知')+' ～ '+(time['end'] or '未明确结束')+' / '+PRECISION[time['precision']]


def path_svg(case,limit=8):
    paths=[p for p in case['path_statements'] if re.search(r'→|->|⇒',p['path'])][:limit]
    marker='path_arrow_'+case['case_id'];height=50+len(paths)*100;out=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1120 {height}" role="img" aria-label="原文资金路径示意"><defs><marker id="{marker}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L10,5 L0,10" fill="#4a6b99"/></marker></defs><text x="10" y="22" font-size="14">原文路径示意：箭头仅表示同一原行的相邻段；显示前{len(paths)}条，完整路径见下表/JSON</text>']
    for i,p in enumerate(paths):
        segments=[v.strip() for v in re.split(r'→|->|⇒',p['path']) if v.strip()];shown=segments[:6];y=45+i*100
        for j,label in enumerate(shown):
            x=10+j*165
            if j:out.append(f'<path d="M{x-24},{y+29} L{x-4},{y+29}" stroke="#4a6b99" fill="none" marker-end="url(#{marker})"/>')
            out.append(f'<rect x="{x}" y="{y}" width="142" height="62" rx="7" fill="#edf3fc" stroke="#a7bddb"/><title>{e(label)}</title>')
            for n in range(3):out.append(f'<text x="{x+7}" y="{y+18+n*17}" font-size="11">{e(label[n*12:(n+1)*12])}{"…" if n==2 and len(label)>36 else ""}</text>')
        out.append(f'<a href="source_rows.html#{p["record_id"]}"><text x="1010" y="{y+25}" font-size="12" fill="#1d4ed8">原行{e(p["locator"].split(" / ")[-1])}</text></a>')
        if len(segments)>6:out.append(f'<text x="1010" y="{y+48}" font-size="10">另{len(segments)-6}段见原文</text>')
    return ''.join(out)+'</svg>'


def export_narrative(corpus,output_root,store=None):
    corpus.validate();output_root=Path(output_root).resolve()
    if output_root.is_relative_to(corpus.root):raise ValueError('报告须保存到案件文本资料目录外')
    if store and not store.verify_audit()['valid']:raise ValueError('本地审计一致性检查未通过')
    result=analyze(corpus,store);directory=output_root/datetime.now(TZ).strftime('narrative_%Y%m%d_%H%M%S_%f');directory.mkdir(parents=True)
    (directory/'sources').mkdir()
    for case in corpus.cases:shutil.copyfile(corpus.source_file(case['case_id']),directory/case['file'])
    atomic_json(directory/'result.json',result);atomic_json(directory/'records.json',corpus.records);atomic_json(directory/'narrative_manifest.json',corpus.manifest)
    def csv_file(name,rows,fields):
        with (directory/name).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader()
            for row in rows:
                safe={k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else str(v) if v is not None else '' for k,v in row.items()}
                # 导出文本以等号等开头时不作为Excel公式执行，原文仍在JSON和原件。
                w.writerow({k:("'"+v if v.startswith(('=','+','-','@')) else v) for k,v in safe.items() if k in fields})
    csv_file('events.csv',corpus.records,['id','case_id','sheet','position','locator','kind','flows','time','source_citation','text','issues'])
    csv_file('amount_mentions.csv',[{'case_id':c['case_id'],**a} for c in result['cases'] for a in c['amount_mentions']],['case_id','record_id','locator','field','source_column','raw','span','value','lower','upper','currency','unit_source','qualifier','category','overlap_warning','dedup_status','source_citation'])
    csv_file('assets.csv',[{'source_records':r['record_ids'],**r} for c in result['cases'] for r in c['entities']],['case_id','id','kind','label','namespace','anonymous_reference','aliases','source_records','checksum_status','validation_scope','verified'])
    csv_file('investigation_checklist.csv',[{'case_id':c['case_id'],**r} for c in result['cases'] for r in c['investigation_checklist']],['case_id','category','priority','action','record_ids','locator','status'])
    csv_file('path_segments.csv',[{'case_id':c['case_id'],**r} for c in result['cases'] for r in c['path_graph']['edges']],['case_id','source','target','record_id','relation','verified'])
    csv_file('reviews.csv',[{'case_id':c['case_id'],**r} for c in result['cases'] for r in c['reviews']],['case_id','finding_id','status','reviewer','note','updated'])
    csv_file('statement_payments.csv',[r for c in result['cases'] for r in c['statement_events']],['case_id','event_id','record_id','locator','source_namespace','source_citation','event_kind','sender_ref','recipient_ref','amount','amount_qualifier','valuation_currency','asset_currency','amount_semantics','date_raw','date_start','date_end','date_precision','date_inherited_parts','alignment','amount_field','source_column','source_span','verified','eligible_for_supervised_training'])
    csv_file('hierarchy_candidates.csv',[{'case_id':c['case_id'],**r} for c in result['cases'] for r in c['hierarchy_reconstruction']['edges']],['case_id','claim_id','from_actor','to_actor','from_label','to_label','relation','phrase','record_id','locator','source_namespace','source_citation','verified'])
    csv_file('amount_reconstruction.csv',[{'case_id':c['case_id'],**r} for c in result['cases'] for r in c['amount_reconstruction']],['case_id','source_namespace','currency','listed_item_count','listed_nominal_total','nominal_subtotals','possible_duplicate_groups','conditional_distinct_transfer_interval','interval_unavailable_reason','whole_case_loss_interval','scope','record_ids'])
    csv_file('fourflow_coverage.csv',[{'case_id':c['case_id'],**r} for c in result['cases'] for r in c['fusion']['coverage']],['case_id','flow','name','source_rows','field_observations','required'])
    csv_file('network_observations.csv',[{'case_id':c['case_id'],**r} for c in result['cases'] for r in c['fusion']['observations'] if r['ip'] or any('IP' in m or '端口' in m for m in r['missing'])],['case_id','record_id','locator','source_citation','namespace','identifier_scope','ip','ip_scope','ip_usable','time','anchors','missing','verified'])
    csv_file('network_findings.csv',[{'case_id':c['case_id'],**r} for c in result['cases'] for r in c['fusion']['network']['findings']],['case_id','kind','title','ip','record_ids','detail','verified'])
    csv_file('industry_candidates.csv',[{'case_id':c['case_id'],**r} for c in result['cases'] for r in c['fusion']['industry']['traces']],['case_id','trace_id','kind','order_id','goods','flows','record_ids','evidence','omitted_evidence','temporal_checks','missing','verified','priority_basis'])
    csv_file('data_quality.csv',[{'case_id':c['case_id'],**r} for c in result['cases'] for r in c['fusion']['quality']['warnings']],['case_id','record_id','locator','issues'])
    mappings=[]
    for row in corpus.records:
        view=field_view(row['fields']);conflicts={c['field'] for c in view['conflicts']}
        for key,originals in view['origins'].items():
            for original in originals:mappings.append({'case_id':row['case_id'],'record_id':row['id'],'locator':row['locator'],'original_header':original,'canonical_field':key,'source_column':row.get('column_map',{}).get(original,''),'status':'冲突，未自动选取' if key in conflicts else '明确字段对应'})
    csv_file('field_mapping.csv',mappings,['case_id','record_id','locator','original_header','canonical_field','source_column','status'])
    rows=[];case_lookup={c['case_id']:c for c in corpus.cases};review_lookup={(c['case_id'],r['finding_id']):r for c in result['cases'] for r in c['reviews']}
    for r in corpus.records:
        case=case_lookup[r['case_id']];context=r.get('source_context',{})
        scope='；引注来自工作表说明 '+context.get('locator','') if r.get('source_citation_scope')=='sheet_context' else ''
        review=review_lookup.get((r['case_id'],'narrative:'+r['id']))
        if review:scope+='；当前核验：'+review['status']+' / '+review['reviewer']+' / '+review['updated']+'；依据：'+review['note']
        rows.append(f'<section id="{r["id"]}"><h2>{e(case["title"])} · {e(r["locator"])}</h2><p><a href="{e(case["file"])}">原件：{e(case["original_name"])}</a> · 原表范围 {e(r.get("range",r["position"]))} · 文书定位 {e(r["source_citation"] or "原文未标注")}{e(scope)}</p><pre>{e(r["text"])}</pre><p class="muted">{e("；".join(r["issues"]))}</p></section>')
        rows[-1]=rows[-1].replace('</h2>','</h2><p><a href="index.html#'+e(r['case_id'])+'_start">返回本案目录</a></p>',1)
    source_nav='<nav class="report-nav" aria-label="原行导航"><a href="index.html#report_directory">返回研判报告</a><a href="#source_top">原行页顶部</a></nav>'
    (directory/'source_rows.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>原行与来源复核</title><style>'+STYLE+'</style>'+source_nav+'<h1 id="source_top">原行与来源复核</h1>'+''.join(rows)+'</html>',encoding='utf-8')
    sections=[]
    for case in result['cases']:
        case_id=case['case_id'];file=case_lookup[case_id]['file'];link=lambda rid:'<a href="source_rows.html#'+rid+'">原行</a>'
        menu=[('_tasks','八项输出'),('_reconstruction','结构与核算'),('_fusion','四流关联与产业溯源'),('_paths','资金与产业路径'),('','提及与时间'),('_reviews','人工核验')]
        sections.append('<section class="case-entry" id="'+case_id+'_start"><h2>'+e(case['title'])+'</h2><nav class="case-links" aria-label="本案分区">'+''.join('<a href="#'+case_id+suffix+'">'+title+'</a>' for suffix,title in menu)+'</nav></section>')
        graph=graph_svg(case);(directory/(case_id+'.svg')).write_text(graph,encoding='utf-8')
        summary='、'.join(FLOW_NAMES[k]+' '+str(v)+' 条' for k,v in case['flows'].items())
        timeline=_table(['原文时间','排序时间/区间与精度','原文事件/描述（完整内容见原行）','资料定位','文书出处'],[(e(r['time']['raw']),e(_time_label(r['time'])),e(r['statement'][:350]+('…' if len(r['statement'])>350 else '')),link(r['record_id'])+' '+e(r['locator']),e(r['source_citation'])) for r in case['timeline']])
        amounts=_table(['金额/数量原文','提取数值及币种','口径/单位依据','约数/重叠','出处'],[(e(a['raw']),e(str(a['value'] if a['value'] is not None else str(a['lower'])+'～'+str(a['upper']) if 'lower' in a else '未知')+' '+str(a['currency'] or '未明确')),e(a['category']+'；'+a.get('unit_source','原文')),e(a['qualifier']+('；口径重叠' if a['overlap_warning'] else '')+('；相同值多次出现' if a.get('same_value_mentions') else '')),link(a['record_id'])+' '+e(a.get('source_column') or a['locator'])+' '+e(a['source_citation'])) for a in case['amount_mentions']])
        issues=_table(['待核验事项','出处','当前意见'],[(e(i['issue']),link(i['record_id'])+' '+e(i['locator']),e(i['review_status'])) for i in case['issues']])
        assets=_table(['类型','原文提及','文书范围','相关记录数','校验范围','来源'],[(e(ENTITY_NAMES.get(a['kind'],a['kind'])),e(a['label']),e(source_name(a.get('namespace'))),len(a['record_ids']),e(CHECKSUM_NAMES.get(a.get('checksum_status'),'仅原文提及')),link(a['record_ids'][0])) for a in case['entities']])
        paths=_table(['表内路径陈述','原文时间','金额口径原文','来源'],[(e(p['path']),e(p['time']),e(p['amount_text']),link(p['record_id'])+' '+e(p['source_citation'])) for p in case['path_statements']])
        cross=_table(['跨流提及','文书范围','流别','相关原行'],[(e(p['label']),e(source_name(p.get('namespace'))),e('、'.join(FLOW_NAMES[f] for f in p['flows'])),' '.join(link(rid) for rid in p['record_ids'])) for p in case['cross_flow_mentions']])
        tasks=_table(['大纲输出项','当前状态','依据与缺口','可回溯原行'],[(e(t['name']),e(t['status']),e(t['detail']),' '.join(link(rid) for rid in t['record_ids'][:6])) for t in case['requirement_outputs']])
        roles=_table(['原文人员/角色','职责陈述','空间流动','来源'],[(e(t['subject_and_role']),e(t['duties']),e(t['movement']),link(t['record_id'])) for t in case['role_statements']])
        identities=_table(['钱包原文','来源声称的归属','编码状态','归属陈述出处','原行'],[(e(t['wallet']),e(t['claimed_owner']),e(CHECKSUM_NAMES[t['checksum_status']]),e(t['claim_locator']),link(t['record_id'])) for t in case['identity_statements']])
        checklist=_table(['核验项','处理顺序','待取得材料/目的','来源'],[(e(t['category']),e(t['priority']),e(t['action']),' '.join(link(rid) for rid in t['record_ids']) or '全案缺口') for t in case['investigation_checklist']])
        review_table=_table(['核验状态','核验人','依据/备注','保存时间','原行'],[(e(r['status']),e(r['reviewer']),e(r['note']),e(r['updated']),link(r['finding_id'].removeprefix('narrative:'))) for r in case['reviews'] if r['finding_id'].startswith('narrative:')])
        review_section='<section id="'+case_id+'_reviews"><h2>'+e(case['title'])+'：人工核验记录</h2><p>当前有效核验 '+str(len(case['reviews']))+' 项。已排除原行保留在原件及原行页，不参与本版提及、关系与金额计算；行级核验不自动成为事实认定或训练标签。</p>'+review_table+'</section>'
        sections.append('<section id="'+case_id+'_tasks"><h2>'+e(case['title'])+'：大纲八项输出核对</h2>'+tasks+'<h3>来源角色与职责</h3>'+roles+'<h3>来源中的钱包归属陈述</h3><p>以下归属仅来自原表说明，编码校验与实名绑定分别核验。</p>'+identities+'<h3>调证与核验清单</h3>'+checklist+'</section>')
        sections.append(html_view(case,_table))
        sections.append(fusion_html(case,_table))
        sections.append(review_section)
        (directory/(case_id+'_hierarchy.svg')).write_text(hierarchy_svg(case),encoding='utf-8')
        diagram=path_svg(case);(directory/(case_id+'_paths.svg')).write_text(diagram,encoding='utf-8')
        sections.append('<section id="'+case_id+'_paths"><h2>'+e(case['title'])+'：资金与产业回流线索</h2><p>以下路径直接保留表内陈述，不据此认定同一笔资金、控制关系或独立证据印证。</p>'+diagram+paths+'<h3>跨流会合提及</h3>'+cross+'</section>')
        sections.append(f'<section id="{case_id}"><h2>{e(case["title"])}</h2><p><a href="{e(file)}">打开原件</a> · 内容行 {case["records"]} · 工作表 {case["sheet_count"]} · {e(summary)}</p><ul>'+''.join('<li>'+e(v)+'</li>' for v in case['observations'])+f'</ul><h3>主体/资产提及与原行联系</h3><p>图示前22个高频提及、最多35条来源记录；完整关系见result.json。已排除内容不参与提及图。</p>{graph}{assets}<h3>事件时间线</h3><p>按文字可见的日期/区间排序，未知时间 {case["unknown_time_records"]} 条未强行排列。日期边界不代表实际发生在该日零时。</p>{timeline}<h3>金额与数量提及</h3><p>以下数值不相加为案件交易总额，账单、余额和行业统计均保留各自口径。</p>{amounts}<h3>核验清单</h3>{issues}</section>')
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>案件文本四流研判</title><style>'+STYLE+'</style><h1>案件文本四流研判</h1><aside>'+e(result['scope'])+'</aside><p>'+e(result['method'])+'</p><p>来源类型：'+e(result['origin'])+'。共 '+str(len(result['cases']))+' 份资料。引用的文书真实性和陈述仍需原件核对。</p><nav>'+''.join('<p><a href="#'+c['case_id']+'">'+e(c['title'])+'</a></p>' for c in result['cases'])+'</nav>'+''.join(sections)+'<section><h2>文件与复核</h2><p><a href="source_rows.html">全部原行</a> · <a href="events.csv">事件CSV</a> · <a href="amount_mentions.csv">金额提及CSV</a> · <a href="assets.csv">资产提及CSV</a> · <a href="result.json">完整分析JSON</a></p><p>三个资料样本用作解析与检索输入，未据此训练或宣称验证真实分类模型。来源中的角色判断及核心结论保持为原文陈述，不作为独立监督答案。</p></section></html>'
    page=page.replace('<a href="events.csv">事件CSV</a>','<a href="events.csv">原文事件CSV</a> · <a href="statement_payments.csv">支付明细CSV</a> · <a href="hierarchy_candidates.csv">关系候选CSV</a> · <a href="amount_reconstruction.csv">明细核算CSV</a>')
    page=page.replace('三个资料样本用作解析与检索输入，未据此训练或宣称验证真实分类模型。','导入资料用于解析、检索和关联，未据此自动训练或宣称验证真实分类模型。')
    page=page.replace('<a href="result.json">完整分析JSON</a>','<a href="reviews.csv">核验意见CSV</a> · <a href="result.json">完整分析JSON</a>')
    page=page.replace('<a href="result.json">完整分析JSON</a>','<a href="fourflow_coverage.csv">四流输入覆盖CSV</a> · <a href="network_observations.csv">IP观测CSV</a> · <a href="network_findings.csv">网络候选CSV</a> · <a href="industry_candidates.csv">产业关联CSV</a> · <a href="data_quality.csv">质量问题CSV</a> · <a href="field_mapping.csv">字段对应CSV</a> · <a href="result.json">完整分析JSON</a>')
    page=page.replace('<nav>','<nav id="report_directory" aria-label="案件目录">',1).replace('<section><h2>文件与复核</h2>','<section id="report_files"><h2>文件与复核</h2>')
    for case in result['cases']:page=page.replace('href="#'+case['case_id']+'">'+e(case['title']), 'href="#'+case['case_id']+'_start">'+e(case['title']))
    origin_label='合成验证资料' if result['origin']=='synthetic' else '用户提供资料（内容待核验）'
    page=page.replace('来源类型：'+e(result['origin']), '资料来源：'+origin_label)
    toolbar='<nav class="report-nav" aria-label="报告导航"><a href="#report_directory">案件目录</a><a href="#report_files">导出文件</a></nav>'
    page=page.replace('</style>','</style>'+toolbar,1)
    (directory/'index.html').write_text(page,encoding='utf-8');corpus.validate()
    atomic_json(directory/'checksums.json',{p.relative_to(directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()})
    with zipfile.ZipFile(directory.with_suffix('.zip'),'w',zipfile.ZIP_DEFLATED) as z:
        for p in directory.rglob('*'):
            if p.is_file():z.write(p,p.relative_to(directory).as_posix())
    if store:store.audit('narrative_exported',{'corpus':corpus.fingerprint,'cases':len(corpus.cases),'path':str(directory)})
    return directory,result
