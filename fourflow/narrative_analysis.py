"""基于文本提及的四流研判。所有关系是材料联系，不代表金融转账或控制。"""
from collections import Counter,defaultdict
from decimal import Decimal
import hashlib,re
from .narrative_extract import FLOW_NAMES
from .narrative_outputs import subject_mentions,alias_matches,task_outputs
from .narrative_reconstruction import source_scopes,ANONYMOUS,reconstruct
from .narrative_fields import field_view
from .narrative_fusion import analyze_fusion
from . import __version__


def analyze(corpus,store=None):
    corpus.validate();result={'version':__version__,'method':'字段适配与质量检查、来源隔离、IP时间窗、明示关系方向、支付精确核算、订单与四流关联及原文检索；未自动训练分类器','origin':corpus.manifest['origin'],'corpus_fingerprint':corpus.fingerprint,'scope':'来源陈述与待核验线索。局部层次和明细金额计算保留假设，不代表现实控制、全案损失或违法概率。','cases':[]}
    for case in corpus.cases:
        rows=corpus.rows(case['case_id']);lookup={r['id']:r for r in rows};reviews=store.reviews(corpus.fingerprint,case['case_id']) if store else [];review_map={r['finding_id'].removeprefix('narrative:'):r for r in reviews};entities={};links=[];amounts=[];issues=[]
        excluded=[rid for rid,r in review_map.items() if r['status']=='已排除']
        scopes=source_scopes([r for r in rows if r['id'] not in excluded and not r.get('formula_present')])['rows']
        def entity(kind,value,rid,metadata=None):
            value=value.strip()
            if not value:return
            anonymous=bool(ANONYMOUS.search(value)) or kind in {'wallet_prefix','wallet_fragment'}
            namespace=scopes[rid]['namespace'] if anonymous else None
            eid='E_'+hashlib.sha256((case['case_id']+'|'+kind+'|'+str(namespace)+'|'+value).encode()).hexdigest()[:16]
            row=entities.setdefault(eid,{'id':eid,'kind':kind,'label':value,'namespace':namespace,'anonymous_reference':anonymous,'case_id':case['case_id'],'record_ids':[],'verified':False})
            if rid not in row['record_ids']:row['record_ids'].append(rid)
            if metadata:row.update({k:v for k,v in metadata.items() if k not in {'record_ids','verified','case_id','id','label','kind','field','source_column','span','value','raw'}})
        for r in rows:
            row_excluded=review_map.get(r['id'],{}).get('status')=='已排除'
            if not row_excluded and not r.get('formula_present'):
                for k,value in field_view(r['fields'])['values'].items():
                    if k=='人员/角色':
                        for subject in subject_mentions(value):entity('person_mention',subject['label'],r['id'],subject)
                    elif k=='产业/企业':entity('industry_mention',re.split('[（(]',value)[0],r['id'])
                for asset in r['assets']:entity(asset['kind'],asset['value'],r['id'],asset)
                for a in r['amounts']:amounts.append({**a,'record_id':r['id'],'locator':r['locator'],'source_citation':r['source_citation'],'source_text':r['fields'].get(a['field'],''),'review_status':review_map.get(r['id'],{}).get('status','待核验')})
            for message in r['issues']:issues.append({'record_id':r['id'],'locator':r['locator'],'issue':message,'review_status':review_map.get(r['id'],{}).get('status','待核验')})
        # 只把实际文本相同提及连到原行，不推断指挥、归属或资金方向。始终按案件隔离别名。
        alias_owners=defaultdict(set)
        for e in entities.values():
            if e['kind'] in {'person_mention','industry_mention'}:
                for alias in e.get('aliases',[e['label']]):alias_owners[(e.get('namespace'),alias.casefold())].add(e['id'])
        for e in entities.values():
            if len(e['label'])>=2 and e['kind'] in {'person_mention','industry_mention'}:
                for r in rows:
                    if r['id'] not in scopes:continue
                    if e['anonymous_reference'] and (scopes[r['id']]['ambiguous'] or scopes[r['id']]['namespace']!=e['namespace']):continue
                    if any(len(alias_owners[(e.get('namespace'),alias.casefold())])==1 and alias_matches(alias,r['text']) for alias in e.get('aliases',[e['label']])) and r['id'] not in e['record_ids']:e['record_ids'].append(r['id'])
            for rid in e['record_ids']:links.append({'record_id':rid,'entity_id':e['id'],'relation':'原行提及','locator':lookup[rid]['locator'],'verified':False})
        # 相同币种/金额提及只标记重复可能，绝不把出现次数当流水笔数或相加。
        groups=defaultdict(list)
        for i,a in enumerate(amounts):
            if a['value'] is not None:groups[(a['currency'],Decimal(a['value']))].append(i)
        for group in groups.values():
            if len(group)>1:
                for i in group:amounts[i]['same_value_mentions']=len(group);amounts[i]['dedup_status']='相同数值重复提及，尚未证明同一笔或不同笔'
        primary=[r for r in rows if r['kind'] not in {'note','overview','evidence_index','matrix'} and r['id'] not in excluded and not r.get('formula_present')]
        flow_counts={k:sum(k in r['flows'] for r in primary) for k in FLOW_NAMES}
        asset_counts=dict(Counter(e['kind'] for e in entities.values()))
        actionable=[{'record_id':r['id'],'locator':r['locator'],'flow':r['flows'],'time':r['time'],'statement':r['text'],'source_citation':r['source_citation'],'statement_status':r['statement_status'],'review':review_map.get(r['id'])} for r in rows if r['kind'] not in {'note','overview','evidence_index'} and review_map.get(r['id'],{}).get('status')!='已排除' and not r.get('formula_present')]
        timeline=sorted((r for r in actionable if r['time']['start']),key=lambda r:(r['time'].get('instant') or r['time']['start'],r['record_id']))
        paths=[]
        for r in rows:
            if review_map.get(r['id'],{}).get('status')=='已排除' or r.get('formula_present'):continue
            for key,value in r['fields'].items():
                if '流转路径' in key or '赃款→产业' in key:paths.append({'record_id':r['id'],'locator':r['locator'],'kind':r['kind'],'time':r['time']['raw'],'path':value,'amount_text':r['fields'].get('金额/规模',''),'source_citation':r['source_citation'],'scope':'表内路径陈述，未验证为同一笔资金或实际控制关系'})
        cross=[]
        for ent in entities.values():
            flows=sorted({flow for rid in ent['record_ids'] for flow in lookup[rid]['flows'] if flow in FLOW_NAMES})
            if len(flows)>=2:cross.append({'entity_id':ent['id'],'label':ent['label'],'namespace':ent.get('namespace'),'flows':flows,'record_ids':ent['record_ids'],'scope':'同一资料内跨流提及，仅提示交叉核验，不是独立印证'})
        reconstruction=reconstruct(case,rows,list(entities.values()),excluded)
        fusion=analyze_fusion(rows,paths,excluded,payment_events=reconstruction['statement_events'])
        tasks=task_outputs(case,rows,list(entities.values()),paths,issues,excluded)
        for request in tasks['investigation_checklist']:
            ids=[];detail=None
            if request['category']=='IP与通信轨迹' and fusion['network']['usable_observations']:
                ids=[o['record_id'] for o in fusion['observations'] if o['ip_usable']];detail='已有'+str(len(ids))+'条具备时刻与对应标识的IP记录；核对原始日志完整性、时区、网络域和账户/设备对应，候选不作为身份认定'
            elif request['category']=='逐笔资金与兑换' and reconstruction['statement_events']:
                ids=list(dict.fromkeys(p['record_id'] for p in reconstruction['statement_events']));detail='已提取'+str(len(reconstruction['statement_events']))+'项明示支付陈述；继续核对原始凭证/交易哈希、账户域、币种、返还和循环资金，不将列示名义合计当作去重本金'
            elif request['category']=='货物与产业链':
                ids=[o['record_id'] for o in fusion['observations'] if 'industry' in o['flows'] and o['order_id']]
                if ids:detail='已有'+str(len(ids))+'条带订单的产业字段记录；核对运单、收货和实际交付/用途、身份对应及独立来源，不将同订单直接解释为违法用途'
            if detail:request.update(priority='核验已有资料',action=detail,record_ids=ids,locator='已有字段记录，事实待核')
        for task in tasks['requirement_outputs']:
            if task['name']=='团伙层级还原' and reconstruction['hierarchy_reconstruction']['edges']:
                task.update(status='显式方向与局部层次',detail='提取指令/直属/汇报方向，检查循环与同源性；未连接者不推定为首脑，真实控制待核',record_ids=list(dict.fromkeys(e['record_id'] for e in reconstruction['hierarchy_reconstruction']['edges'])))
            elif task['name']=='涉案金额区间推算' and reconstruction['statement_events']:
                task.update(status='明细条件核算',detail='按来源与币种精确核算明示支付项目，给出条件去重区间；全案损失及覆盖率仍不可识别',record_ids=list(dict.fromkeys(e['record_id'] for e in reconstruction['statement_events'])))
            elif task['name']=='因果证据链':task.update(status='假设与证据缺口审计',detail='列出假设、支持原行、结构冲突与替代解释所需证据；不把时间先后作为因果证明')
            elif task['name']=='资金链图谱' and reconstruction['statement_events']:task.update(status='明示支付与原文路径图',detail='按源表付款方/收款方还原明细引用网络，保持掩码、币种和时间不确定性；不是完整账户网络')
        result['cases'].append({'case_id':case['case_id'],'title':case['title'],'source_id':case['source_id'],'file':case['file'],'sha256':case['sha256'],'records':len(rows),'primary_rows':len(primary),'sheet_count':len(case.get('sheets',[])),'flows':flow_counts,'kind_counts':dict(Counter(r['kind'] for r in rows)),'asset_counts':asset_counts,'entities':list(entities.values()),'mention_links':links,'timeline':timeline,'unknown_time_records':sum(not r['time']['start'] for r in actionable),'amount_mentions':amounts,'issues':issues,'reviews':reviews,'excluded_records':excluded,'observations':_observations(flow_counts,asset_counts,amounts,issues,fusion),'path_statements':paths,'cross_flow_mentions':cross,'fusion':fusion,**tasks,**reconstruction})
    return result


def _observations(flows,assets,amounts,issues,fusion=None):
    out=['已读取'+('、'.join(FLOW_NAMES[k] for k,v in flows.items() if v) or '未分类')+'资料；同一来源的分析表不作为多份独立印证。']
    if assets.get('wallet'):out.append(f"提取完整钱包文本 {assets['wallet']} 个；尚未取得或核对链上交易明细。")
    if assets.get('wallet_prefix'):out.append('存在截短钱包，仅保留前缀线索，不绑定到完整地址。')
    if not assets.get('ip'):out.append('未提取到可用IP地址提及；不把域名、注册地或园区坐标转换为访问日志。')
    elif fusion and fusion['network']['usable_observations']:out.append('存在明确IP、带时区时刻及对应标识的观测 '+str(fusion['network']['usable_observations'])+' 条；网络候选 '+str(len(fusion['network']['findings']))+' 条，见“四流关联与产业溯源”。')
    else:out.append('IP仅为文字提及；没有逐条访问时间与账户/设备对应时，不生成访问轨迹。')
    if fusion:out.append('产业关联候选 '+str(len(fusion['industry']['traces']))+' 条；原文路径与同订单关联分别列示，均保留来源和材料缺口。')
    if amounts:out.append(f'提取金额/数量提及 {len(amounts)} 项；余额、汇总、费用及背景统计分别展示，未计算交易总额。')
    if any('重叠' in i['issue'] for i in issues):out.append('原文包含重叠金额口径，需按原始文书和逐笔记录核对。')
    out.append('人物角色与产业功能保留原文陈述，未据此生成已核实控制边或训练标签。')
    return out
