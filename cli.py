"""批量分析与训练入口。桌面使用 app.py。"""
from fourflow.io_utils import atomic_json,point_model
import argparse
import json
from datetime import datetime
from pathlib import Path

from fourflow.core import load_dataset,safe_json,temporal_paths,TZ,build_case_graph
from fourflow.service import analysis_graph
from fourflow.analytics import amount_summary,detect_patterns
from fourflow.store import Store
from fourflow.model import TemporalHeteroGNN,train_model,resolve_model_directory
from fourflow.reporting import export_report
from fourflow.synthetic import create_dataset,create_templates
from fourflow.cleaning import clean_dataset
from fourflow.investigation import network_analysis,industry_traces,pca_projection
from fourflow.roles import RoleHierarchyModel,train_roles
from fourflow.extended_data import create_extended_dataset
from fourflow.causal import load_study,estimate_study,save_causal_run,create_causal_template
from fourflow.multimodal import LocalMediaProcessor,save_extraction

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description="四流关联分析系统")
    parser.add_argument("action",choices=["analyze","export","train","templates","causal-template","generate-synthetic","generate-extended","clean","investigate","roles-train","roles-predict","causal","extract","simulate","generate-advanced","joint-train","joint-predict","joint-template","domain-evaluate","hd-eia-clean","hd-eia-audit","preflight","workflow","empirical","backup","restore","inspect-table","import-tables","narrative-import","narrative-report","narrative-search","narrative-audit","feature-audit"])
    parser.add_argument("--dataset",type=Path,default=ROOT/"data"/"enhanced_sample")
    parser.add_argument("--case",default="C001")
    parser.add_argument("--as-of")
    parser.add_argument("--source")
    parser.add_argument("--target")
    parser.add_argument("--currency",default="USDT")
    parser.add_argument("--epochs",type=int,default=70)
    parser.add_argument("--count",type=int)
    parser.add_argument("--output",type=Path)
    parser.add_argument('--input',type=Path)
    parser.add_argument('--origin',choices=['real','synthetic'],default='real')
    parser.add_argument('--refutations',type=int,default=12)
    parser.add_argument('--seed',type=int,default=1001)
    parser.add_argument('--skip-media',action='store_true')
    parser.add_argument('--pdf-mode',choices=['auto','hybrid'],default='hybrid')
    parser.add_argument('--model',type=Path)
    parser.add_argument('--namespace',default='ADV14')
    parser.add_argument('--domain-dataset',type=Path,action='append')
    parser.add_argument('--model-kind',choices=['fund','roles','joint'],default='fund')
    parser.add_argument('--criteria',type=Path)
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--table',default='funds')
    parser.add_argument('--documents',type=Path,nargs='+')
    parser.add_argument('--query')
    parser.add_argument('--reference',type=Path)
    args=parser.parse_args()
    if args.action.startswith('narrative-'):
        from fourflow.narrative import import_documents,load_corpus,search
        from fourflow.narrative_report import export_narrative
        if args.action=='narrative-import':
            if not args.documents or not args.output:parser.error('narrative-import需要--documents文件列表和--output新目录')
            corpus=import_documents(args.documents,args.output,args.origin,print);print(json.dumps({'cases':len(corpus.cases),'records':len(corpus.records),'output':str(corpus.root)},ensure_ascii=False));return
        corpus=load_corpus(args.dataset)
        if args.action=='narrative-audit':
            from fourflow.narrative_validation import evaluate_reference,load_reference
            if not args.reference or not args.output:parser.error('narrative-audit需要--reference原文对照清单和--output核对结果JSON')
            if args.output.exists():parser.error('核对结果请保存到新文件，避免覆盖历史结果')
            result=evaluate_reference(corpus,load_reference(args.reference));atomic_json(args.output,result);print(json.dumps({'passed':result['passed'],'failed':result['failed'],'scope':result['scope'],'classification_accuracy':None},ensure_ascii=False));return
        if args.action=='narrative-search':
            if not args.query:parser.error('narrative-search需要--query')
            print(json.dumps([{'case_id':r['record']['case_id'],'locator':r['record']['locator'],'text':r['record']['text'],'similarity':r['similarity']} for r in search(corpus,args.query)],ensure_ascii=False,indent=2));return
        output,result=export_narrative(corpus,args.output or ROOT/'runtime/narrative_runs',Store(ROOT/'runtime/analysis.sqlite3'));print(json.dumps({'output':str(output),'cases':len(result['cases'])},ensure_ascii=False));return
    if args.action=='feature-audit':
        from fourflow.feature_audit import audit_features
        if not args.output or args.output.exists():parser.error('feature-audit需要--output新JSON文件')
        result=audit_features(load_dataset(args.dataset),args.case,args.as_of,args.input);atomic_json(args.output,result);print(json.dumps({'output':str(args.output),'sender_groups':len(result['sender_features']),'formula_records':len(result['formula_audit']),'scope':result['scope']},ensure_ascii=False));return
    if args.action=='inspect-table':
        from fourflow.intake import read_table,suggest_columns
        if not args.input:parser.error('inspect-table需要--input')
        result=read_table(args.input);print(json.dumps({'sha256':result['sha256'],'sheets':result['sheets'],'headers':result['headers'],'row_count':result['row_count'],'suggested_columns':suggest_columns(args.table,result['headers'])},ensure_ascii=False,indent=2));return
    if args.action=='import-tables':
        from fourflow.intake import import_tables
        if not args.plan or not args.output:parser.error('import-tables需要--plan与--output新目录')
        plan=json.loads(args.plan.read_text(encoding='utf-8-sig'));result=import_tables(plan['entries'],args.output,plan['metadata']);print(json.dumps(result,ensure_ascii=False,indent=2));return
    if args.action=='restore':
        from fourflow.operations import restore_backup
        if not args.input or not args.output:parser.error('restore需要--input备份及--output新目录')
        print(restore_backup(args.input,args.output));return
    if args.action in {'preflight','workflow','empirical','backup'}:
        dataset=load_dataset(args.dataset)
        if args.action=='preflight':
            from fourflow.operations import environment_check
            print(json.dumps(environment_check(ROOT,dataset),ensure_ascii=False,indent=2));return
        if args.action=='backup':
            from fourflow.operations import backup_workspace
            print(backup_workspace(ROOT,dataset,args.output or ROOT/'runtime/backups'));return
        if args.action=='empirical':
            from fourflow.empirical import load_model,evaluate_external
            pointer={'fund':'latest.json','roles':'roles_latest.json','joint':'joint_latest.json'}[args.model_kind];directory=args.model
            if directory is None:
                directory=Path(json.loads((ROOT/'runtime/models'/pointer).read_text())['path']);directory=directory if directory.is_absolute() else ROOT/directory
            criteria=json.loads(args.criteria.read_text(encoding='utf-8')) if args.criteria else None;output,result=evaluate_external(load_model(directory,args.model_kind),dataset,args.model_kind,args.output or ROOT/'runtime/empirical_runs',criteria,print);print(json.dumps({'output':str(output),'summary':result['summary'],'criteria':result['criteria']},ensure_ascii=False,indent=2));return
        from fourflow.orchestrator import run_analysis
        from fourflow.empirical import load_model
        models={}
        for kind,pointer in [('fund','latest.json'),('roles','roles_latest.json'),('joint','joint_latest.json')]:
            try:
                directory=Path(json.loads((ROOT/'runtime/models'/pointer).read_text())['path']);directory=directory if directory.is_absolute() else ROOT/directory
                if kind!='joint' or dataset.manifest.get('advanced'):models[kind]=load_model(directory,kind)
            except (OSError,ValueError,KeyError):continue
        store=Store(ROOT/'runtime/analysis.sqlite3');graph=analysis_graph(dataset,args.case,store,args.as_of);output,result=run_analysis(dataset,graph,store,args.output or ROOT/'runtime/analysis_runs',models,progress=print);print(json.dumps({'output':str(output),'status':result['status'],'record_coverage':result['record_coverage']},ensure_ascii=False));return
    if args.action=='generate-advanced':
        if not args.output:parser.error('generate-advanced需要--output新的空目录')
        from fourflow.advanced_data import create_advanced_dataset
        print(create_advanced_dataset(args.output,args.count or 60,args.seed,args.namespace));return
    if args.action=='joint-template':
        if not args.output:parser.error('joint-template需要--output新的空目录')
        from fourflow.advanced_templates import create_advanced_template
        print(create_advanced_template(args.output));return
    if args.action=='hd-eia-clean':
        if not args.output:parser.error('hd-eia-clean需要--output新的空目录')
        from fourflow.hd_eia import clean_and_validate
        result=clean_and_validate(args.dataset,args.output);print(json.dumps({'output':result['output'],'valid':result['cleaning']['valid'],'cases':len(result['audit']['cases'])},ensure_ascii=False));return
    if args.action in {'joint-train','joint-predict','domain-evaluate','hd-eia-audit'}:
        from fourflow.dual_model import DualFlowModel,train_joint
        if args.action=='domain-evaluate':
            from fourflow.domain_validation import evaluate_domains
            directories=args.domain_dataset or [args.dataset]
            path=args.model
            if path is None:
                path=Path(json.loads((ROOT/'runtime/models/joint_latest.json').read_text())['path']);path=path if path.is_absolute() else ROOT/path
            directory,result=evaluate_domains(DualFlowModel.load(path),{str(i+1):p for i,p in enumerate(directories)},args.output or ROOT/'runtime/domain_runs');print(json.dumps({'output':str(directory),'model_id':result['model_id'],'scope':result['scope']},ensure_ascii=False));return
        dataset=load_dataset(args.dataset)
        if args.action=='hd-eia-audit':
            from fourflow.hd_eia import quality_audit
            print(json.dumps(quality_audit(dataset),ensure_ascii=False,indent=2));return
        if args.action=='joint-train':
            output=args.output or ROOT/'runtime/models'/datetime.now(TZ).strftime('joint_%Y%m%d_%H%M%S_%f');model=train_joint(dataset,output,args.epochs,args.seed,print)
            point_model(ROOT,'joint_latest.json',output);print(json.dumps(model.metadata['metrics']['test'],ensure_ascii=False,indent=2));return
        path=args.model
        if path is None:
            path=Path(json.loads((ROOT/'runtime/models/joint_latest.json').read_text())['path']);path=path if path.is_absolute() else ROOT/path
        from fourflow.moid_xai import save_joint_report
        graph=build_case_graph(dataset,args.case,args.as_of);directory,result=save_joint_report(dataset,graph,DualFlowModel.load(path),args.output or ROOT/'runtime/joint_runs');print(json.dumps({'output':str(directory),'fund_score':result['fund_score'],'control_candidates':len(result['control_candidates'])},ensure_ascii=False));return
    if args.action=='simulate':
        from fourflow.simulation import run_simulation
        directory,result=run_simulation(ROOT,args.output,args.seed,args.count if args.count is not None else 12,not args.skip_media,print)
        print(json.dumps({'output':str(directory),'passed':result['passed'],'warnings':result['warnings'],'failed':result['failed'],'origin':result['origin']},ensure_ascii=False))
        if result['failed']:raise SystemExit(2)
        return
    if args.action=='causal-template':
        if not args.output:parser.error('causal-template需要--output新的空目录')
        print(create_causal_template(args.output));return
    if args.action=='extract':
        if not args.input:parser.error('extract需要--input本地材料路径')
        result=LocalMediaProcessor(ROOT).extract(args.input,args.origin,progress=print,pdf_mode=args.pdf_mode)
        output=save_extraction(result,args.output or ROOT/'runtime/extractions')
        print(json.dumps({'output':str(output),'segments':len(result['segments']),'candidates':len(result['candidate_facts']),'notes':result['notes']},ensure_ascii=False));return
    if args.action=='causal':
        if not args.input:parser.error('causal需要--input研究目录')
        study=load_study(args.input);output,result=save_causal_run(study,estimate_study(study,args.refutations),args.output or ROOT/'runtime/causal_runs')
        print(json.dumps({'output':str(output),'available':result['available'],'naive_difference':result['naive_difference'],'estimate':result.get('estimate'),'confidence_interval_95':result.get('confidence_interval_95'),'scope':result['scope']},ensure_ascii=False));return
    if args.action=='clean':
        if not args.output:parser.error('clean需要--output指定新的清洗目录')
        manifest=json.loads((args.dataset/'manifest.json').read_text(encoding='utf-8-sig'))
        if manifest.get('advanced'):
            from fourflow.hd_eia import clean_and_validate
            result=clean_and_validate(args.dataset,args.output)['cleaning']
        else:result=clean_dataset(args.dataset,args.output)
        print(json.dumps({'valid':result['valid'],'counts':result['counts'],'quarantined':len(result['quarantined']),'report':str(args.output/'cleaning_report.json')},ensure_ascii=False,indent=2))
        if not result['valid']:raise SystemExit(2)
        return
    if args.action=="templates":
        if not args.output:parser.error("templates 需要 --output 指定空目录")
        if args.output.exists() and any(args.output.iterdir()):parser.error("目标目录非空，停止以避免覆盖")
        print(create_templates(args.output));return
    if args.action in {"generate-synthetic","generate-extended"}:
        if not args.output:parser.error("需要 --output")
        if args.output.exists() and any(args.output.iterdir()):parser.error("目标目录非空，停止以避免覆盖")
        print((create_dataset if args.action=='generate-synthetic' else create_extended_dataset)(args.output,args.count if args.count is not None else 96));return
    dataset=load_dataset(args.dataset)
    if args.action=='roles-train':
        output=args.output or ROOT/'runtime/models'/datetime.now(TZ).strftime('roles_%Y%m%d_%H%M%S_%f')
        if output.exists() and any(output.iterdir()):parser.error('角色训练输出目录非空，请使用新目录')
        model=train_roles(dataset,output,args.epochs,progress=print);pointer=ROOT/'runtime/models/roles_latest.json';pointer.parent.mkdir(parents=True,exist_ok=True);point_model(ROOT,'roles_latest.json',output);print(json.dumps(model.metadata['metrics'],ensure_ascii=False,indent=2));return
    if args.action=="train":
        output=args.output or ROOT/'runtime/models'/datetime.now(TZ).strftime('model_%Y%m%d_%H%M%S_%f')
        if output.exists() and any(output.iterdir()):parser.error('训练输出目录非空，请使用新目录保留模型版本')
        model=train_model(dataset,output,args.epochs,progress=print)
        pointer=ROOT/'runtime/models/latest.json';pointer.parent.mkdir(parents=True,exist_ok=True)
        point_model(ROOT,"latest.json",output)
        print(json.dumps(model.metadata["metrics"],ensure_ascii=False,indent=2));return
    store=Store(ROOT/"runtime"/"analysis.sqlite3")
    graph=analysis_graph(dataset,args.case,store,args.as_of)
    roles=None;role_pointer=ROOT/'runtime/models/roles_latest.json'
    if args.action in {'export','roles-predict'} and role_pointer.is_file():
        try:
            role_directory=Path(json.loads(role_pointer.read_text(encoding='utf-8'))['path'])
            if not role_directory.is_absolute():role_directory=ROOT/role_directory
            roles=RoleHierarchyModel.load(role_directory).predict(dataset,graph)
        except (ValueError,OSError,KeyError) as exc:
            roles={'available':False,'reason':'角色模型不可用：'+str(exc)}
    if args.action=='roles-predict':
        print(json.dumps(roles or {'available':False,'reason':'尚未载入角色模型'},ensure_ascii=False,indent=2));return
    if args.action=='investigate':
        result={'network':network_analysis(graph),'industry_traces':industry_traces(dataset,graph),'pca':pca_projection(graph) if 0<len(graph)<=256 else {'available':False,'reason':'PCA限1至256节点'}}
        print(json.dumps(result,ensure_ascii=False,indent=2,default=safe_json));return
    paths=[]
    if args.source and args.target:
        paths=temporal_paths(graph,args.source,args.target,currency=args.currency)["paths"]
    if args.action=="analyze":
        print(json.dumps({"origin":dataset.manifest["origin"],"nodes":len(graph),"edges":graph.number_of_edges(),"amounts":amount_summary(graph),"findings":detect_patterns(graph),"paths":paths},ensure_ascii=False,indent=2,default=safe_json))
    else:
        model_dir=resolve_model_directory(ROOT)
        model=TemporalHeteroGNN.load(model_dir) if (model_dir/"model_card.json").is_file() else None
        assessment=model.assess(dataset,graph) if model else None
        print(export_report(dataset,graph,store,args.output or ROOT/"exports",paths,assessment,research={'roles':roles}))


if __name__=="__main__":main()
