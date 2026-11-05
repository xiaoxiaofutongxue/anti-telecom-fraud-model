"""从固定合成输入生成可阅读示例；不使用本机核验库或最近案件。"""
from pathlib import Path
import html
import json
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fourflow.core import load_dataset
from fourflow.model import TemporalHeteroGNN,resolve_model_directory
from fourflow.roles import RoleHierarchyModel
from fourflow.service import analysis_graph
from fourflow.store import Store
from fourflow.reporting import export_report
from fourflow.causal import load_study,estimate_study,save_causal_run
from fourflow.multimodal import LocalMediaProcessor,save_extraction


def main():
    output=ROOT/'examples/1.3'
    if output.exists() and any(output.iterdir()):raise ValueError('交付示例目录已存在，不覆盖；请先保留旧版本后再生成')
    output.mkdir(parents=True,exist_ok=True);temporary=ROOT/'runtime/test_workspace';temporary.mkdir(parents=True,exist_ok=True)
    data=load_dataset(ROOT/'data/enhanced_sample')
    if not data.synthetic:raise ValueError('演示目录必须保持合成来源')
    model=TemporalHeteroGNN.load(resolve_model_directory(ROOT))
    pointer=Path(json.loads((ROOT/'runtime/models/roles_latest.json').read_text(encoding='utf-8'))['path'])
    role_model=RoleHierarchyModel.load(pointer if pointer.is_absolute() else ROOT/pointer)
    with tempfile.TemporaryDirectory(dir=temporary) as work:
        store=Store(Path(work)/'example.sqlite3');graph=analysis_graph(data,'C001',store)
        case=export_report(data,graph,store,output/'case',assessment=model.assess(data,graph),research={'roles':role_model.predict(data,graph)})
    study=load_study(ROOT/'data/causal_demo')
    if study['design']['origin']!='synthetic':raise ValueError('演示研究必须保持合成来源')
    causal,result=save_causal_run(study,estimate_study(study),output/'causal')
    processor=LocalMediaProcessor(ROOT);extractions=[]
    for name in ('synthetic_record.png','synthetic_speech.wav','synthetic_scan.pdf','synthetic_silent_video.mp4'):
        extraction=processor.extract(ROOT/'data/media_demo'/name,'synthetic')
        directory=save_extraction(extraction,output/'media');extractions.append({'name':name,'directory':directory.relative_to(output).as_posix(),'segments':len(extraction['segments']),'candidate_fields':len(extraction['candidate_facts'])})
    links=[('个案四流、人员候选与行为图',case/'report.html'),('独立队列因果研究',causal/'report.html')]
    items=''.join('<li><a href="'+p.relative_to(output).as_posix()+'">'+html.escape(label)+'</a></li>' for label,p in links)
    media=''.join('<li><a href="'+r['directory']+'/text.txt">'+html.escape(r['name'])+'</a>：'+str(r['segments'])+'个片段，'+str(r['candidate_fields'])+'个待核验字段</li>' for r in extractions)
    (output/'index.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>四流1.3合成交付示例</title><style>body{font:17px/1.8 "Microsoft YaHei UI",sans-serif;max-width:980px;margin:50px auto;padding:20px;color:#172b44}h1{color:#1e40af}aside{background:#fff1d9;padding:20px;border-radius:12px}li{padding:10px}a{color:#1d4ed8}</style><h1>四流系统1.3 · 合成验证示例</h1><aside>所有记录、人员角色和材料均为合成。模型指标不表示真实反诈性能；队列效应不构成个案因果；机器提取结果尚需人工核验。</aside><h2>离线报告</h2><ul>'+items+'</ul><h2>原始材料识别实测</h2><ul>'+media+'</ul><p>各输出目录附机器可读结果、来源指纹和文件校验清单。原材料位于项目的data/media_demo；识别输出中的source_path记录生成当时的本机路径。</p></html>',encoding='utf-8')
    summary={'origin':'synthetic','case_report':(case/'report.html').relative_to(ROOT).as_posix(),'causal_report':(causal/'report.html').relative_to(ROOT).as_posix(),'causal_estimate':result['estimate'],'causal_ci':result['confidence_interval_95'],'extractions':extractions}
    (output/'生成记录.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':main()
