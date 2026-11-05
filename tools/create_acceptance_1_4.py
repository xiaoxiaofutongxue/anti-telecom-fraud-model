"""整理显式指定的本轮合成输出，不选择用户最近案件。"""
from pathlib import Path
import argparse,html,json,shutil
ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--domain',required=True,type=Path);parser.add_argument('--simulation',required=True,type=Path);args=parser.parse_args()
    domain=args.domain.resolve();simulation=args.simulation.resolve();output=ROOT/'examples/1.4'
    if not domain.is_relative_to(ROOT/'runtime/domain_runs') or not simulation.is_relative_to(ROOT/'runtime/simulations'):raise ValueError('仅整理本项目域评测和独立模拟目录')
    result=json.loads((domain/'result.json').read_text(encoding='utf-8'));sim=json.loads((simulation/'result.json').read_text(encoding='utf-8'))
    if result['version']!='1.4.0' or sim['version']!='1.4.0' or sim['origin']!='synthetic' or any(r['origin']!='synthetic' for r in result['runs']):raise ValueError('只整理1.4合成验收')
    if (output/'domain').exists() or (output/'simulation').exists():raise ValueError('交付输出已存在，停止覆盖')
    output.mkdir(parents=True,exist_ok=True);shutil.copytree(domain,output/'domain');shutil.copytree(simulation,output/'simulation',ignore=shutil.ignore_patterns('*.sqlite3','*.sqlite3-*','roundtrip_models'))
    evaluation=output/'evaluation';evaluation.mkdir(exist_ok=True)
    for name in ('selection_protocol.json','training_record.json'):shutil.copyfile(ROOT/'runtime/upgrade_1_4'/name,evaluation/name)
    log=ROOT/'runtime/upgrade_1_4_tests_final.txt'
    if log.exists():shutil.copyfile(log,evaluation/'自动测试记录.txt')
    e=lambda s:html.escape(str(s),quote=True);rows=[]
    for r in result['runs']:
        m=r['metrics'];rows.append('<tr>'+''.join('<td>'+e(v)+'</td>' for v in [r['name'],m['cases'],m['fund']['f1'],f"{m['roles']['macro_f1']:.4f}",f"{m['levels']['macro_f1']:.4f}",f"{m['displayed_control_candidates']['f1']:.4f}",len(m['fund_errors'])])+'</tr>')
    errors=[{'dataset':r['name'],**row} for r in result['runs'] for row in r['metrics']['fund_errors']]
    page=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>1.4联合模型验收</title><style>body{{font:16px/1.8 'Microsoft YaHei UI',sans-serif;max-width:1180px;margin:30px auto;padding:25px;background:#f4f6fa;color:#172b44}}section{{background:white;padding:24px;margin:20px 0;border-radius:12px}}aside{{background:#fff0d7;padding:20px}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #dbe3ee}}th{{background:#eef4ff}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#1d4ed8}}</style><h1>1.4 双图、原始多模态与多目标验收</h1><aside>全部为合成输入。原材料没有提供HD-EIA/MOID-XAI公式，交付为明确定义的自主工程实现，不能冒充外部算法完整复现。高分不构成真实团伙控制或现实泛化证据。</aside><section><h2>实现与训练</h2><p>96案，57/19/20时间分割；实体/事件双图共同训练，原始RGB、波形、视频时序卷积与文字掩码融合；资金、角色、层级、定向控制四头联合监督。未知控制关系不作负类。</p><p>实体、事件、交互、图像、音频、视频、控制头实测都有非零梯度。两套60轮候选只按验证复合损失选择：ERM 0.20588145略优于GroupDRO 0.20589401，采用ERM。未按外部成绩改权重。</p><p>原始编码范围：32×32图像，前0.512秒音轨，视频全时段4帧；这是轻量研究输入，不能替代完整OCR/ASR或真实语义模型。</p><a href="evaluation/selection_protocol.json">冻结与选择协议</a> · <a href="evaluation/training_record.json">训练记录与指标</a></section><section><h2>冻结后72案合成域评测</h2><table><tr><th>集合</th><th>案件</th><th>资金F1</th><th>角色宏F1</th><th>层级宏F1</th><th>实际控制候选F1</th><th>资金错例</th></tr>{''.join(rows)}</table><p>集合1为未见地区、集合2为未见案型、集合3为同时未见。资产/IP/媒体隔离；每案6名有标签人员和1名未标注单次观测主体。结构仍来自相关生成机制，不能证明真实外部泛化。</p><a href="domain/report.html">完整域报告</a> · <a href="domain/result.json">逐域、逐案、拒判和实际候选明细</a><h3>保留资金错误</h3><pre>{e(json.dumps(errors,ensure_ascii=False,indent=2))}</pre></section><section><h2>全流程与功能自查</h2><p>完整模拟：{sim['passed']}项通过、{sim['warnings']}项需关注、{sim['failed']}项失败；保留旧资金/角色模型的错误警告，不强行改为全部通过。</p><a href="simulation/report.html">13环节流程与来源输出</a> · <a href="evaluation/自动测试记录.txt">自动测试完整记录</a><p>新增检查覆盖原始解码、视频时间采样、梯度、权重校验、标签隔离、媒体人工对应/历史/路径、缓存变化、缺流拒判、控制方向、Shapley、Pareto、报告快照及高级清洗。</p></section><section><h2>使用入口与仍需验证</h2><p>桌面：人员·因果·材料 → 双图·多模态联合研判 → 载入双图样例 → 联合研判与解释。可继续运行外部域评测，或导出真实数据空模板。</p><p>真实控制关系恢复、跨真实地区/案型效果、长视听语义效果尚无真实数据验证；人脸/说话人识别、自动法律认定、外部自动采集和自主调证未实现。模型解释不等于因果证明。</p><p>架构、损失、选择、输入上限和详细差距见docs/1.4联合模型与验收.md。升级前1.3包和核验库已备份。</p></section></html>'''
    (output/'index.html').write_text(page,encoding='utf-8');print(json.dumps({'report':str(output/'index.html'),'domain_cases':sum(r['metrics']['cases'] for r in result['runs'])},ensure_ascii=False))


if __name__=='__main__':main()
