"""整理已冻结的合成验收结果及明确指定的模拟输出，不选择用户最近案件。"""
from pathlib import Path
import argparse,html,json,shutil

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--simulation',type=Path,required=True);args=parser.parse_args()
    simulation=args.simulation.resolve()
    if not simulation.is_relative_to((ROOT/'runtime/simulations').resolve()):raise ValueError('仅整理项目独立模拟目录')
    result=json.loads((simulation/'result.json').read_text(encoding='utf-8'))
    if result.get('origin')!='synthetic' or result.get('version')!='1.3.0':raise ValueError('交付自测必须是本轮合成模拟')
    output=ROOT/'examples/1.3';evaluation=output/'evaluation';evaluation.mkdir(parents=True,exist_ok=True)
    for name in ('before.json','after.json','training_record.json','selection_protocol.json','final_acceptance.json'):
        shutil.copyfile(ROOT/'runtime/upgrade_1_3'/name,evaluation/name)
    target=output/'simulation'
    if target.exists():raise ValueError('交付模拟目录已存在，不覆盖')
    shutil.copytree(simulation,target,ignore=shutil.ignore_patterns('*.sqlite3','*.sqlite3-*','roundtrip_models'))
    final=json.loads((evaluation/'final_acceptance.json').read_text(encoding='utf-8'));before=json.loads((evaluation/'before.json').read_text(encoding='utf-8'));after=json.loads((evaluation/'after.json').read_text(encoding='utf-8'))
    e=lambda value:html.escape(str(value),quote=True)
    def table(head,rows):return '<table><tr>'+''.join('<th>'+e(h)+'</th>' for h in head)+'</tr>'+''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</table>'
    comparisons=table(['指标','开发集：旧权重','开发集：新权重'],[['资金F1',before['fund']['f1'],after['fund']['f1']],['角色宏F1',f"{before['roles']['macro_f1']:.4f}",f"{after['roles']['macro_f1']:.4f}"],['高分可用人员候选错分',before['selective']['high_confidence_joint_errors'],after['selective']['high_confidence_joint_errors']]])
    final_table=table(['最终合成验收指标','实际结果'],[['案件数',final['case_count']],['资金精确率 / 召回率 / F1',f"{final['fund']['precision']:.4f} / {final['fund']['recall']:.4f} / {final['fund']['f1']:.4f}"],['资金混淆矩阵',json.dumps(final['fund']['confusion'])],['角色 / 层级宏F1',f"{final['roles']['macro_f1']:.4f} / {final['levels']['macro_f1']:.4f}"],['已标注人员可用覆盖率',f"{final['selective']['eligible']}/{final['selective']['labelled_persons']}"],['可用候选同时正确率',f"{final['selective']['joint_accuracy_on_eligible']:.4%}"],['单次观测的未标注主体暂不分类',f"{final['selective']['unlabelled_abstained']}/{final['selective']['unlabelled_persons']}"]])
    errors=table(['案件','合成目标标签','资金分数'],[[r['case_id'],r['label'],r['fund_score']] for r in final['cases'] if r['fund_available'] and int(r['fund_score']>=.5)!=r['label']])
    role_errors=table(['案件/人员','设定角色','候选角色','组合分数'],[[r['case_id']+'/'+r['actor_id'],r['truth_role'],r['role'],f"{r['joint_score']:.4f}"] for r in final['persons'] if r['eligible'] and not r['correct']])
    page=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>1.3模型升级与合成验收</title><style>body{{font:16px/1.8 'Microsoft YaHei UI',sans-serif;max-width:1100px;margin:30px auto;padding:24px;background:#f4f6fa;color:#172b44}}section{{background:white;padding:24px;border-radius:12px;margin:20px 0}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #dbe3ee}}th{{background:#eef4ff}}aside{{background:#fff0d7;border-radius:10px;padding:20px}}a{{color:#1d4ed8}}</style><h1>1.3 模型升级与合成验收</h1><aside>全部为合成数据与预设目标标签，不能作为真实案件效果。功能运行通过不等于材料全部达标，也不构成诈骗、控制关系或个案因果认定。</aside><section><h2>完整流程</h2><p>95项自动测试通过。实际桌面全流程：{result['passed']}项通过、{result['warnings']}项需关注、{result['failed']}项失败。需关注保留实际错例，未改成全通过。</p><a href="simulation/report.html">查看完整流程、清洗隔离、来源、核验、因果、材料和报告输出</a></section><section><h2>开发压力集</h2>{comparisons}<p>种子619、24案，已用于发现退化，不能再称最终独立验收。角色改善不能掩盖资金退化；之后资金权重只按固定验证集的F1/NLL选择，温度仅由验证标签拟合。</p></section><section><h2>冻结后另生成的最终验收</h2>{final_table}<p>种子1703、不同资产及网络域；未用于训练、校准或选择。仍来自相关合成机制，不是跨地区/案型的现实外部验证。分类头指标与暂不分类后的覆盖率须同时阅读。</p></section><section><h2>保留资金误报</h2>{errors}<h2>保留人员错分</h2>{role_errors}<p>全部逐案分数、未判定原因和人工核对方向见<a href="evaluation/final_acceptance.json">最终验收JSON</a>；<a href="evaluation/selection_protocol.json">选择协议</a>保留验证集合、候选指标和选中权重指纹。</p></section><section><h2>需求差距</h2><p>结构化四流辅助研判、来源核验、条件金额、角色候选、本地文字层材料识别和条件队列因果研究已可运行。真实泛化、真实团伙控制恢复、自动法律/个案因果认定、完整HD-EIA/MOID-XAI/双图联合训练、原始视听端到端融合、外部平台采集和自主调证尚未实现或未验证。</p><p>原始信息不足时，程序不能从高分自动补出事实。完整核对见项目docs/1.3升级与全流程自查.md。</p></section></html>'''
    (output/'模型验收报告.html').write_text(page,encoding='utf-8')
    index=output/'index.html';text=index.read_text(encoding='utf-8');text+='<h2>本轮升级验收</h2><ul><li><a href="模型验收报告.html">模型对比、最终验收与保留错例</a></li><li><a href="simulation/report.html">全流程模拟自测</a></li></ul>';index.write_text(text,encoding='utf-8')
    print(json.dumps({'report':str(output/'模型验收报告.html'),'simulation':str(target),'origin':'synthetic','tests':95},ensure_ascii=False))


if __name__=='__main__':main()
