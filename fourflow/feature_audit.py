"""Observable feature and example-formula audit. Never silently changes trained inputs."""
from collections import defaultdict
from decimal import Decimal,localcontext
import hashlib,json,math
from pathlib import Path
from .core import parse_time
from . import __version__

SPECS={
 'funds':{'monthly_sender_volume':('positive',),'time_anomaly_factor':('nonnegative',)},
 'goods':{'transport_duration':('positive',),'shortest_route_duration':('positive',),'disguise_degree':('unit',)},
 'information':{'information_similarity':('unit',),'propagation_hops':('integer',),'coded_language_intensity':('nonnegative',)},
 'people':{'coappearance_frequency':('integer',),'relationship_path_length':('integer',),'time_overlap_factor':('nonnegative',)},
}
FORMULAS={'funds':'amount / monthly_sender_volume * time_anomaly_factor','goods':'exp(-transport_duration / shortest_route_duration) * disguise_degree','information':'information_similarity / (1 + propagation_hops) * coded_language_intensity','people':'coappearance_frequency / (1 + relationship_path_length) * time_overlap_factor'}
TABLES={'funds':'funds','goods':'shipments','information':'communications','people':'relations'}
ID_FIELDS={'funds':'tx_id','goods':'shipment_id','information':'event_id','people':'relation_id'}


def evaluate_formula(flow,values):
    """Missing is None, including undefined n(overlap); weights are not probabilities."""
    if flow not in SPECS:raise ValueError('未知流别')
    required=list(SPECS[flow])+(['amount'] if flow=='funds' else [])
    missing=[k for k in required if values.get(k) is None]
    v={}
    for key in required:
        if values.get(key) is None:continue
        if isinstance(values[key],bool):raise ValueError('参数不能是布尔值')
        try:number=float(values[key])
        except (ValueError,TypeError,OverflowError) as exc:raise ValueError('参数不是有限数值') from exc
        spec=SPECS[flow].get(key,('positive',))[0]
        if not math.isfinite(number) or number<0 or (spec=='positive' and number<=0) or (spec=='unit' and number>1) or (spec=='integer' and not number.is_integer()):raise ValueError('参数超出声明范围：'+key)
        v[key]=number
    if missing:return {'value':None,'missing':missing,'formula':FORMULAS[flow]}
    if flow=='funds':
        if v['monthly_sender_volume']<v['amount']:raise ValueError('所声明同月总额小于原始单笔金额')
        result=v['amount']/v['monthly_sender_volume']*v['time_anomaly_factor']
    elif flow=='goods':result=math.exp(-v['transport_duration']/v['shortest_route_duration'])*v['disguise_degree']
    elif flow=='information':result=v['information_similarity']/(1+v['propagation_hops'])*v['coded_language_intensity']
    else:result=v['coappearance_frequency']/(1+v['relationship_path_length'])*v['time_overlap_factor']
    if not math.isfinite(result):raise ValueError('公式计算超出支持数值范围')
    return {'value':result,'missing':[],'formula':FORMULAS[flow]}


def audit_features(dataset,case_id,as_of=None,observations=None):
    if not any(c['case_id']==case_id for c in dataset.tables['cases']):raise ValueError('案件不存在')
    cutoff=parse_time(as_of) if as_of else None
    funds=[r for r in dataset.rows('funds',case_id) if not cutoff or r['_time']<=cutoff]
    groups=defaultdict(list);months=defaultdict(list)
    for r in funds:
        key=(r['from_kind'],r['chain'],r['from_id'],r['currency']);groups[key].append(r);months[key+(r['_time'].strftime('%Y-%m'),)].append(r)
    features=[]
    for key,rows in sorted(groups.items()):
        # Decimal ratios avoid overflow from large monetary magnitudes; only dimensionless results use floats.
        with localcontext() as ctx:
            ctx.prec=max(40,max(len(r['_amount'].as_tuple().digits) for r in rows)+len(str(len(rows)))+5)
            volume=sum((r['_amount'] for r in rows),Decimal(0));counterparties=defaultdict(lambda:Decimal(0))
            for r in rows:counterparties[(r['to_kind'],r['to_id'])]+=r['_amount']
            entropy=max(0.0,-sum(float(value/volume)*math.log2(float(value/volume)) for value in counterparties.values() if float(value/volume)>0))
            mean=volume/len(rows);cv=(sum(((r['_amount']/mean-1)**2 for r in rows),Decimal(0))/len(rows)).sqrt()
        features.append({'sender_kind':key[0],'chain':key[1],'sender_ref':key[2],'currency':key[3],'record_ids':[r['tx_id'] for r in rows],'transaction_count':len(rows),'counterparty_amount_entropy_bits':entropy,'amount_population_cv':str(cv),'mixer_use_rate':None,'mixer_missing_reason':'未提供经核验且按链区分的混币服务名单及覆盖范围；不能将未知记为0'})
    formula_rows=[]
    for flow in ('goods','information','people'):
        for r in dataset.rows(TABLES[flow],case_id):
            if cutoff and r['_time']>cutoff:continue
            formula_rows.append({'flow':flow,'record_id':r[ID_FIELDS[flow]],'values':{},'evaluation':evaluate_formula(flow,{}),'scope':'当前原始表未包含此示例公式的全部观测量；保持缺失'})
    for r in funds:
        key=(r['from_kind'],r['chain'],r['from_id'],r['currency'],r['_time'].strftime('%Y-%m'))
        with localcontext() as ctx:
            ctx.prec=max(40,max(len(x['_amount'].as_tuple().digits) for x in months[key])+len(str(len(months[key])))+5)
            denominator=sum((x['_amount'] for x in months[key]),Decimal(0));share=r['_amount']/denominator
        formula_rows.append({'flow':'funds','record_id':r['tx_id'],'values':{'amount':str(r['_amount']),'monthly_sender_volume':str(denominator),'time_anomaly_factor':None},'month':key[-1],'currency':r['currency'],'sender_month_observed_share':str(share),'scope':'仅导入快照中该月的可见总额，未证明全月覆盖；不可作为完整月度权重','evaluation':evaluate_formula('funds',{'amount':r['_amount'],'monthly_sender_volume':denominator})})
    attachment=None
    if observations is not None:
        path=Path(observations);raw=path.read_bytes();doc=json.loads(raw.decode('utf-8-sig'))
        if doc.get('kind')!='fourflow_formula_observations' or doc.get('schema_version')!=1 or doc.get('dataset_fingerprint')!=dataset.fingerprint:raise ValueError('公式观测清单类型、版本或数据指纹不符')
        attachment={'file_sha256':hashlib.sha256(raw).hexdigest(),'path':str(path.resolve())};seen=set()
        if not isinstance(doc.get('records'),list):raise ValueError('公式观测records须为列表')
        for item in doc['records']:
            flow=item.get('flow');rid=item.get('record_id');key=(flow,rid)
            if flow not in SPECS or key in seen:raise ValueError('流别不支持或重复登记公式观测')
            seen.add(key)
            target=next((r for r in dataset.rows(TABLES[flow],case_id) if r[ID_FIELDS[flow]]==rid),None)
            if not target:raise ValueError('公式观测未定位到当前案件原始记录')
            available=parse_time(item.get('available_at',''))
            if available<target['_time']:raise ValueError('公式观测可用时间不能早于原事件')
            if cutoff and available>cutoff:continue
            evidence=item.get('evidence',[])
            if not isinstance(evidence,list) or not evidence or any(not dataset.source(case_id,s) for s in evidence):raise ValueError('公式观测须引用本案来源')
            if item.get('confirmed') is not True or not str(item.get('reviewer','')).strip():raise ValueError('外部公式参数须有核验声明和核验人')
            values=item.get('values',{});definitions=item.get('definitions',{})
            if not isinstance(values,dict) or not isinstance(definitions,dict):raise ValueError('参数值及定义须为对象')
            required=set(SPECS[flow]);missing_definitions=[k for k in required if values.get(k) is not None and not str(definitions.get(k,'')).strip()]
            if missing_definitions:raise ValueError('补充参数的观测定义/单位/口径缺失：'+','.join(missing_definitions))
            values={k:values.get(k) for k in required}
            if flow=='funds':values['amount']=str(target['_amount'])
            formula_rows.append({'flow':flow,'record_id':rid,'values':values,'definitions':definitions,'evidence':evidence,'reviewer':item['reviewer'],'available_at':available.isoformat(),'evaluation':evaluate_formula(flow,values),'scope':'按提供方核验声明计算；月覆盖、最短路径基准、暗语尺度和n(时间重叠)函数定义由来源负责，未独立证明'})
    return {'version':__version__,'case_id':case_id,'dataset_fingerprint':dataset.fingerprint,'as_of':as_of,'sender_features':features,'formula_audit':formula_rows,'observations':attachment,'unobserved_features':['包装相似度/签收异常','信息语义风险/暗语强度','共同出现及时间重叠函数','历史案件关系','经核验混币名单与覆盖率'],'definitions':{'entropy':'按发送方、链、币种分组，接收方金额占比p的-sum(p*log2(p))','volatility':'同组逐笔金额的总体标准差/均值，n而非n-1；描述当前快照','monthly_volume':'按导入时区日历月、发送方、链和币种的观测金额；截至时点过滤先于统计'},'scope':'大纲示例公式的条件计算与观测缺口；不等同已训练模型特征、违法概率或实战验证。现有29维权重保持不变'}


def readable_audit(result):
    labels={'funds':'资金流','goods':'产业/货物流','information':'信息流','people':'人员流'}
    parameters={'amount':'单笔金额','monthly_sender_volume':'同币种月度发送总额','time_anomaly_factor':'时间异常因子','transport_duration':'实际运输时长','shortest_route_duration':'最短路径运输时长','disguise_degree':'伪装程度','information_similarity':'信息相似度','propagation_hops':'传播跳数','coded_language_intensity':'暗语强度','coappearance_frequency':'共同出现次数','relationship_path_length':'关系路径长度','time_overlap_factor':'时间重叠函数及参数'}
    lines=['观测特征与大纲示例公式核验','当前为独立核验，尚未用于模型评分。公式条件成立也不表示违法或控制关系成立。','案件 '+result['case_id']+'；截至 '+str(result['as_of'] or '全部导入记录'),'','当前可观测特征：']
    for r in result['sender_features']:lines += [r['sender_ref']+' / '+r['chain']+' / '+r['currency']+' / '+str(r['transaction_count'])+'笔',f"接收方金额熵 {r['counterparty_amount_entropy_bits']:.6f} bit；金额总体变异系数 {Decimal(r['amount_population_cv']):.6f}",'混币使用率：缺少核验名单，未计算']
    lines+=['','定义：',*result['definitions'].values(),'','示例公式逐记录核验：']
    for r in result['formula_audit']:lines += [labels[r['flow']]+' / '+r['record_id']+' / '+r['evaluation']['formula'],'结果 '+(str(r['evaluation']['value']) if r['evaluation']['value'] is not None else '未计算')+'；缺少 '+('、'.join(parameters[k] for k in r['evaluation']['missing']) or '无'),'口径：'+r['scope']]
    return '\n'.join(lines)
