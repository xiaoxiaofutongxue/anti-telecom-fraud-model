"""Material-backed task outputs. All assertions remain scoped to their source text."""
import hashlib,re
from collections import Counter
from .narrative_fields import field_view


def subject_mentions(value):
    stem=re.split('[（(]',value)[0].strip();group=re.fullmatch(r'([A-Za-z]+-)(\d+(?:/\d+)+)',stem)
    if group:names=[group[1]+n for n in group[2].split('/')]
    elif '、' in stem:names=[v.strip() for v in stem.split('、') if v.strip()]
    elif re.fullmatch(r'(?:[A-Za-z]+-\d+/)+[A-Za-z]+-\d+',stem):names=stem.split('/')
    else:names=[stem]
    out=[]
    for name in names:
        aliases={name}
        if re.search(r'[\u4e00-\u9fff]',name) and not re.search(r'(?:TV|FV|CC|Worker|Company|INDIVIDUAL)[ -]?\d',name,re.I):
            aliases.update(re.findall(r'[\u4e00-\u9fff]{2,}',name));aliases.update(re.findall(r'[A-Z]{3,}(?:-\d+)?',name))
        if len(names)==1:
            aliases.update(re.findall(r'["“]([^"”]{2,20})["”]',value))
            aliases.update(v.strip() for v in re.findall(r'[（(]([A-Za-z]+ [A-Za-z ]+)[）)]',value))
        out.append({'label':name,'aliases':sorted(aliases),'mention_scope':'原文称谓/明示别名，不代表实名身份确认','group_statement':value if len(names)>1 else None})
    return out


def alias_matches(alias,text):
    if len(alias)<2:return False
    if re.search(r'[A-Za-z0-9]',alias):return bool(re.search(r'(?<![A-Za-z0-9_-])'+re.escape(alias)+r'(?![A-Za-z0-9_-])',text,re.I))
    return alias in text


def task_outputs(case,rows,entities,paths,issues,excluded):
    active=[r for r in rows if r['id'] not in excluded and not r.get('formula_present')]
    role=[];assertions=[];patterns=[];identities=[]
    for r in active:
        f=field_view(r['fields'])['values'];ref={'record_id':r['id'],'locator':r['locator'],'source_citation':r['source_citation'],'verified':False}
        if f.get('人员/角色'):role.append({**ref,'subject_and_role':f['人员/角色'],'duties':f.get('行为/职责',''),'movement':f.get('空间流动',''),'scope':'来源中的角色/职责陈述，未恢复真实控制层级'})
        for k,v in f.items():
            if k in {'A','案件名称','指标'} and v in {'指控罪名','核心犯罪模式','案号/法院'}:
                values=list(f.values());index=values.index(v)
                if index+1<len(values):assertions.append({**ref,'claim_type':v,'statement':values[index+1]})
    definitions=[('投资诈骗叙述',r'杀猪盘|加密货币投资诈骗|虚假投资|仿冒交易平台'),('冒充执法叙述',r'冒充.*(?:执法|检察|警察)|枪支信用卡|假冒.*(?:FBI|检察)'),('资金转换/清洗叙述',r'洗钱|混币|bridging|水房|即时兑换'),('拆分/归集叙述',r'喷撒|汇聚|归集|拆散|拆分'),('产业再投入叙述',r'再投资|复建|回流|反哺'),('迁移/贩运叙述',r'人员迁移|贩运|武装押运|园区迁移')]
    for title,pattern in definitions:
        hits=[r for r in active if re.search(pattern,r['text'],re.I) and not re.search(r'未发现|并非|排除(?:该|此)?模式',r['text'])]
        if hits:patterns.append({'pattern':title,'record_ids':[r['id'] for r in hits],'scope':'规则命中的原文模式叙述，不是已验证违法模式预测'})
    # An identity candidate is emitted only from an explicit ownership sentence in the same source sheet.
    for r in active:
        context=r.get('source_context',{});m=re.search(r'私钥由([\u4e00-\u9fffA-Za-z ]{2,30}?)(?:个人持有|自持)',context.get('text',''))
        if not m:continue
        for a in r['assets']:
            if a['kind']=='wallet':identities.append({'record_id':r['id'],'locator':r['locator'],'wallet':a['value'],'claimed_owner':m[1].strip(),'claim_locator':context.get('locator'),'claim_text':context.get('text'),'checksum_status':a.get('checksum_status','not_checked'),'verified':False,'scope':'工作表来源说明中的归属陈述；未形成实名绑定'})
    # Each edge stays inside one explicit source path. No cross-row money-flow stitching.
    path_graph={'scope':'原文路径段；没有交易哈希、逐笔时钟或资金同一性验证','nodes':[],'edges':[]}
    for p in paths:
        segments=[s.strip() for s in re.split(r'→|->|⇒',p['path']) if s.strip()]
        if len(segments)<2:continue
        ids=[]
        for i,label in enumerate(segments):
            nid='P_'+hashlib.sha256((p['record_id']+'|'+str(i)).encode()).hexdigest()[:16];ids.append(nid);path_graph['nodes'].append({'id':nid,'label':label,'record_id':p['record_id'],'case_id':case['case_id'],'verified':False})
        for a,b in zip(ids,ids[1:]):path_graph['edges'].append({'source':a,'target':b,'record_id':p['record_id'],'relation':'原文相邻路径段','verified':False})
    requests=[];seen=set()
    for issue in issues:
        message=issue['issue'];r=next((r for r in active if r['id']==issue['record_id']),None)
        if r is None:continue
        if '编码校验' in message:kind,action,priority='地址编码','逐字符核对原始文书/扫描件中的地址，记录校正依据；校验通过后再进行链上查询','优先核对'
        elif '掩码片段' in message:kind,action,priority='不完整地址','取得完整链标识、地址与对应来源；保持前后缀独立','补充材料'
        elif '缺少原始文书' in message and r['kind'] not in {'overview','note'}:kind,action,priority='出处缺口','补原始文书名称、页/段位置及原件；保留当前表格行','优先核对'
        elif '重叠' in message:kind,action,priority='金额重叠','按原始凭证/交易标识核对重叠、受害者与币种，不将叙述汇总直接加总','优先核对'
        else:continue
        key=(kind,r['id'])
        if key in seen:continue
        seen.add(key);requests.append({'category':kind,'priority':priority,'action':action,'record_ids':[r['id']],'locator':r['locator'],'status':'待核验'})
    for kind,action in [('逐笔资金与兑换','取得交易哈希/银行流水、链与币种、发送接收账户、时间和兑换凭证，核对资金同一性及去重本金'),('IP与通信轨迹','取得授权范围内原始访问/通信日志、账户设备对应及时间，不用域名或坐标代替IP日志'),('角色与控制关系','独立核对身份、职责、指令/执行证据及反证，形成按案件隔离且经核实的标签'),('货物与产业链','补充采购订单、物流节点/承运主体、收货和用途凭证，核对资金与物品的关联')]:requests.append({'category':kind,'priority':'补充材料','action':action,'record_ids':[],'locator':'全案资料缺口','status':'待核验'})
    tasks=[('诈骗/违法研判','仅保留来源指控','提取原表指控与模式陈述；没有独立法律事实或诈骗分类性能验证',[a['record_id'] for a in assertions]),('团伙层级还原','来源角色陈述','保留人员职责和分工；文本提及不能恢复已核实控制层级',[a['record_id'] for a in role]),('涉案金额区间推算','材料不足','金额、存量、费用与背景统计分列；缺逐笔去重及覆盖率，暂不给区间',[]),('违法行为模式识别','原文模式线索','按明确文字归类并回到来源，未生成违法概率',list(dict.fromkeys(rid for p in patterns for rid in p['record_ids']))),('因果证据链','关联与来源链','提供原文路径与证据位置；因果假设和替代解释尚需独立材料',[p['record_id'] for p in paths]),('实名化地址','来源归属候选' if identities else '材料不足','保留明示归属文本；地址编码、链上活动和实名对应分别核验',[a['record_id'] for a in identities]),('资金链图谱','原文路径图','每条连边只连接同一原文路径的相邻段，未形成完整逐笔资金网络',[p['record_id'] for p in paths]),('调证线索清单','待人工处理','提供待取得项、核验目的和原行位置；由人员审查后使用',list(dict.fromkeys(rid for q in requests for rid in q['record_ids'])))]
    return {'role_statements':role,'source_assertions':assertions,'pattern_statements':patterns,'identity_statements':identities,'path_graph':path_graph,'investigation_checklist':requests,'requirement_outputs':[{'name':name,'status':status,'detail':detail,'record_ids':list(dict.fromkeys(ids))} for name,status,detail,ids in tasks]}
