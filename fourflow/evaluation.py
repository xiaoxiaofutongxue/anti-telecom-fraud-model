"""带核实标签的离线评估；同时报告覆盖率和未判定，不只挑高分样本。"""
import numpy as np

from .core import build_case_graph,is_true
from .model import metrics
from .roles import classification_metrics


def evaluate_models(dataset,fund_model,role_model):
    cases=[];fund_labels=[];fund_scores=[];role_labels=[];role_prob=[];level_labels=[];level_prob=[]
    eligible=correct=high_confidence_errors=unknown=unknown_abstained=0;classified=[]
    case_labels={r['case_id']:int(r['label']) for r in dataset.tables['case_labels'] if is_true(r['confirmed'])}
    for case in dataset.tables['cases']:
        cid=case['case_id'];graph=build_case_graph(dataset,cid);assessment=fund_model.assess(dataset,graph);prediction=role_model.predict(dataset,graph)
        if cid in case_labels and assessment['available']:fund_labels.append(case_labels[cid]);fund_scores.append(assessment['probability'])
        truth={r['actor_id']:r for r in dataset.rows('person_labels',cid) if is_true(r['confirmed'])}
        p,q=role_model.probabilities(graph);index={n:i for i,n in enumerate(sorted(graph))}
        for row in prediction.get('predictions',[]):
            if row['actor_id'] not in truth:
                unknown+=1;unknown_abstained+=not row.get('prediction_available',True);continue
            target=truth[row['actor_id']];i=index[row['node']]
            role_labels.append(role_model.task['roles'].index(target['role_label']));role_prob.append(p[i])
            level_labels.append(role_model.task['levels'].index(target['level_label']));level_prob.append(q[i])
            active=row.get('prediction_available',True);right=(row.get('candidate_role',row['role']),row.get('candidate_level',row['level']))==(target['role_label'],target['level_label'])
            eligible+=active;correct+=active and right;high_confidence_errors+=active and not right and row['joint_probability']>=.9
            classified.append({'case_id':cid,'actor_id':row['actor_id'],'truth_role':target['role_label'],'truth_level':target['level_label'],'role':row['role'],'level':row['level'],'eligible':active,'correct':right if active else None,'joint_score':row['joint_probability'],'reasons':row.get('abstention_reasons',[])})
        cases.append({'case_id':cid,'nodes':len(graph),'edges':graph.number_of_edges(),'label':case_labels.get(cid),'fund_score':assessment.get('probability'),'fund_available':assessment['available'],'fund_reason':assessment.get('reason',''),'person_candidates':prediction.get('eligible_count',len(prediction.get('predictions',[]))),'person_abstained':prediction.get('abstained_count',0),'known_or_related_training_records':bool(assessment.get('known_split') or assessment.get('related_splits') or prediction.get('known_split') or prediction.get('related_splits'))})
    if fund_scores and not np.isfinite(fund_scores).all():raise ValueError('评估发现非有限资金分数')
    total=len(role_labels)
    return {'origin':dataset.manifest['origin'],'dataset_fingerprint':dataset.fingerprint,'fund_model_id':fund_model.metadata['model_id'],'role_model_id':role_model.metadata['model_id'],'case_count':len(cases),'fund':metrics(fund_labels,fund_scores) if fund_scores else {'available':False},'fund_coverage':len(fund_scores)/max(1,len(case_labels)),'roles':classification_metrics(role_labels,np.array(role_prob),len(role_model.task['roles'])) if total else {'n':0},'levels':classification_metrics(level_labels,np.array(level_prob),len(role_model.task['levels'])) if total else {'n':0},'selective':{'labelled_persons':total,'eligible':eligible,'abstained':total-eligible,'coverage':eligible/max(1,total),'joint_accuracy_on_eligible':correct/eligible if eligible else None,'high_confidence_joint_errors':high_confidence_errors,'unlabelled_persons':unknown,'unlabelled_abstained':unknown_abstained},'cases':cases,'persons':classified,'interpretation':'真实来源标注并不等于真实效果已验证；合成来源指标仅反映指定生成机制。覆盖率与准确率须同时阅读，未标注不当作负类。'}
