"""Explicit field aliases. Ambiguous columns remain unresolved; source fields are never overwritten."""
import re
import unicodedata

ALIASES={
    '时间':('timestamp','date','日期','交易时间','发生时间','访问时间','通信时间','付款日期','发货时间'),
    '付款人':('payer','payer_name','付款方','转出方'),
    '付款人/日期':('付款方/日期',),
    '收款方':('payee','payee_name','收款人','转入方'),
    '付款账户':('from_id','from_account','付款账号','转出账户','转出地址'),
    '收款账户':('to_id','to_account','收款账号','转入账户','转入地址'),
    '金额':('amount','交易金额','支付金额','汇款金额'),
    '币种':('currency','交易币种','货币'),
    '链/账户域':('chain','账户域','链','链名称'),
    '访问账户':('account_id','登录账户','登录账号'),
    '访问账户类型':('account_kind',),
    '人员编号':('actor_id','主体编号'),
    '人员/角色':('人员姓名','人员名称','person_name'),
    '发送主体':('from_actor','发送人编号','通信发起人'),
    '接收主体':('to_actor','接收人编号','通信接收人'),
    '设备编号':('device_id','设备ID','设备标识'),
    'IP地址':('ip','访问IP','登录IP','源IP'),
    'IP网络域':('network_scope','内网域','专用网络域'),
    '源端口':('source_port',),
    '通信摘要':('summary','通信内容'),
    '渠道':('channel','通信方式'),
    '事件类型':('event_type','访问类型'),
    '订单编号':('order_id','订单号','采购单号'),
    '运输编号':('shipment_id','运单号','物流单号'),
    '发货主体':('sender_id','发货人编号'),
    '收货主体':('receiver_id','收货人编号'),
    '承运主体':('carrier_id','承运人编号'),
    '物品名称':('goods_label','货物名称','采购物品'),
    '发货地':('from_place','起运地'),
    '收货地':('to_place','目的地'),
    '产业类型':('service_type',),
    '行为/职责':('职责','人员职责'),
    '空间流动':('人员轨迹','活动轨迹'),
    '证据来源':('source_citation','文书段落'),
    '流转路径(来源→中介→去向)':('流转路径','资金路径'),
    '来源标识域':('identifier_scope','标识命名空间'),
}


def normalized_header(value):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value))).casefold()


_LOOKUP={normalized_header(alias):key for key,aliases in ALIASES.items() for alias in (key,*aliases)}


def canonical_header(value):
    compact=normalized_header(value)
    unit=re.fullmatch(r'(?:交易|支付|汇款)?金额\((usd|cny|eur|hkd|gbp|jpy|sgd|aud|usdt|usdc|btc|eth|美元|人民币)\)',compact)
    if unit:return '金额('+unit[1].upper()+')'
    return _LOOKUP.get(compact,str(value).strip())


def known_header(value):return normalized_header(value) in _LOOKUP or bool(re.match(r'^金额\(',canonical_header(value)))


def field_view(fields):
    values={};origins={};conflicts=[]
    for header,value in fields.items():
        if not str(value).strip():continue
        key=canonical_header(header);value=str(value).strip();origins.setdefault(key,[]).append(header)
        if key not in values:values[key]=value
        elif values[key]!=value:conflicts.append({'field':key,'columns':list(origins[key]),'reason':'同义列值冲突，未自动选取'})
    for conflict in conflicts:values.pop(conflict['field'],None)
    return {'values':values,'origins':origins,'conflicts':conflicts}


def currency_code(value):
    value=str(value or '').strip().upper()
    return {'美元':'USD','美金':'USD','人民币':'CNY','比特币':'BTC','以太币':'ETH'}.get(value,value) if value else None
