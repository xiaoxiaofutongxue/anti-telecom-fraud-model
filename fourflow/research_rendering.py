import html


def hierarchy_svg(result,width=1100):
    esc=lambda value:html.escape(str(value),quote=True)
    predictions=result.get('predictions',[])
    if not result.get('available') or not predictions:return '<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="60"><text x="20" y="30" font-size="14">暂无可用角色层级预测；不以中心度替代层级分类。</text></svg>'
    levels=list(result['task']['levels'])
    if any(p.get('level_index') is None for p in predictions):levels.append('暂不分类')
    groups={i:[p for p in predictions if (p.get('level_index') if p.get('level_index') is not None else len(levels)-1)==i] for i in range(len(levels))}
    height=max(340,120+max(map(len,groups.values()))*115);positions={}
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><defs><marker id="hier_arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#d97706"/></marker></defs><rect width="100%" height="100%" fill="white"/>']
    for i,label in enumerate(levels):
        x=130+i*(width-260)/max(1,len(levels)-1)
        parts.append(f'<text x="{x}" y="35" text-anchor="middle" font-size="17">{esc(label)}</text>')
        for j,row in enumerate(groups[i]):positions[row['node']]=(x,100+j*115)
    for link in result.get('hierarchy_links',[]):
        if link['upper'] not in positions or link['lower'] not in positions:continue
        x,y=positions[link['upper']];xx,yy=positions[link['lower']]
        parts.append(f'<path d="M{x+75},{y} L{xx-78},{yy}" stroke="#d97706" fill="none" stroke-dasharray="5 4" marker-end="url(#hier_arrow)"><title>{esc("来源联系："+", ".join(link["record_ids"])+"；方向由层级候选排序，非控制关系")}</title></path>')
    for row in predictions:
        x,y=positions[row['node']];score=f"{row['joint_probability']:.0%}" if row.get('prediction_available',True) else '待补材料';parts.append(f'<rect x="{x-75}" y="{y-30}" width="150" height="65" rx="10" fill="#fff7ed" stroke="#fdba74"/><text x="{x}" y="{y-6}" text-anchor="middle" font-size="14">{esc(row["alias"][:16])}</text><text x="{x}" y="{y+17}" text-anchor="middle" font-size="12">{esc(row["role"])} {score}</text>')
    parts.append(f'<text x="20" y="{height-15}" font-size="12" fill="#64748b">监督模型的候选角色/层级；虚线为已有联系与预测层级组织的候选，不代表犯罪、指挥或控制关系。</text></svg>')
    return ''.join(parts)
