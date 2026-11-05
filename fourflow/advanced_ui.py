"""1.4双图研究工作台。耗时编码/训练/解释均在应用工作线程执行。"""
from .io_utils import atomic_json,point_model
from pathlib import Path
from datetime import datetime
import json,os
import tkinter as tk
from tkinter import ttk,scrolledtext,filedialog,messagebox
from .core import TZ


class AdvancedPanel:
    def __init__(self,app,parent):
        self.app=app;self.root=app.root_dir;self.model=None;self.result=None;self.output=None;self.context=None
        first=ttk.Frame(parent);first.pack(fill='x')
        for label,command in [('载入双图样例',lambda:app.load_path(self.root/'data/advanced_sample_synthetic')),('联合训练合成集',lambda:self.train(True)),('用当前标签联合训练',lambda:self.train(False)),('载入联合模型',self.load_model),('高级数据模板',self.template)]:ttk.Button(first,text=label,command=command).pack(side='left',padx=3,pady=4)
        second=ttk.Frame(parent);second.pack(fill='x')
        for label,command in [('清洗与对齐自查',self.audit),('联合研判与解释',self.predict),('地区/案型合成验证',self.validate),('选择外部数据评测',self.validate_external),('打开本次输出',self.open_output)]:ttk.Button(second,text=label,command=command).pack(side='left',padx=3,pady=4)
        ttk.Label(parent,text='实体图与事件图共同训练；仅启用训练中实际提供的原始模态。未训练的媒体分支不参与评分。控制方向需独立标签，输出仅作人工核验候选。',wraplength=1100,padding=7).pack(fill='x')
        self.summary=tk.StringVar(value='尚未运行。可载入双图样例后执行联合研判；正式数据须有经核对的媒体对应及标签。')
        ttk.Label(parent,textvariable=self.summary,wraplength=1300,padding=7).pack(fill='x')
        self.tree=app.make_tree(parent,[('alias','主体',140),('role','角色候选',140),('level','层级候选',110),('display_score','分数',90),('reason_text','状态/待补原因',480)],height=6)
        self.text=scrolledtext.ScrolledText(parent,height=13,wrap='word',padx=12,pady=10,relief='flat');self.text.pack(fill='both',expand=True)
        app.set_text(self.text,'本页为独立定义的HD-EIA/MOID-XAI工程版，不冒充未提供公式的外部算法复现。\n缺少真实数据时只能验证架构、流程及合成域表现。')
    def busy(self):
        if self.app.busy:messagebox.showinfo('正在处理','请等待当前任务完成。');return True
        return False
    def current_context(self):return (self.app.dataset.fingerprint,self.app.current_case(),self.app.graph.graph.get('as_of',''))
    def clear_result(self,message):
        self.result=None;self.output=None;self.app.fill_tree(self.tree,[],[]);self.summary.set(message)
        self.app.set_text(self.text,'当前快照尚无联合研判结果。请执行联合研判；历史输出仍保留在runtime/joint_runs和runtime/domain_runs。')
    def refresh(self):
        if self.context!=self.current_context():
            self.context=self.current_context();self.clear_result('数据或截至时间已变化，请重新联合研判。旧输出仍在原目录保留。')
    def ensure_model(self):
        from .dual_model import DualFlowModel
        if self.model is None:
            pointer=json.loads((self.root/'runtime/models/joint_latest.json').read_text(encoding='utf-8'));path=Path(pointer['path']);self.model=DualFlowModel.load(path if path.is_absolute() else self.root/path)
        return self.model
    def load_model(self):
        if self.busy():return
        path=filedialog.askdirectory(title='选择联合模型目录')
        if not path:return
        def work():
            from .dual_model import DualFlowModel
            return DualFlowModel.load(path)
        def done(model):
            self.model=model;self.clear_result('联合模型已载入：'+model.metadata['model_id'])
            point_model(self.root,'joint_latest.json',path)
        self.app.run_job(work,done,'正在载入联合模型…')
    def train(self,synthetic):
        if self.busy():return
        dataset=self.app.dataset;output=self.root/'runtime/models'/datetime.now(TZ).strftime('joint_%Y%m%d_%H%M%S_%f')
        def work():
            from .core import load_dataset
            from .dual_model import train_joint
            data=load_dataset(self.root/'data/advanced_training_synthetic') if synthetic else dataset
            return train_joint(data,output,60,progress=lambda s:self.app.jobs.put(('progress',s,None)))
        def done(model):
            self.model=model;self.clear_result('新模型训练完成，旧研判已清除，请重新分析。');point_model(self.root,'joint_latest.json',output)
            self.summary.set('联合训练完成：'+model.metadata['model_id']);self.app.set_text(self.text,json.dumps(model.metadata['metrics'],ensure_ascii=False,indent=2));self.app.store.audit('joint_trained',{'model_id':model.metadata['model_id'],'origin':model.metadata['origin']})
        self.app.run_job(work,done,'正在准备实体/事件双图与原始媒体联合训练…')
    def predict(self):
        if self.busy():return
        dataset=self.app.dataset;graph=self.app.graph.copy();context=self.current_context()
        def work():
            from .moid_xai import save_joint_report
            return save_joint_report(dataset,graph,self.ensure_model(),self.root/'runtime/joint_runs')
        def done(value):
            self.output,result=value
            if context!=self.current_context():self.summary.set('旧快照研判已输出，请在当前快照重新运行。');return
            self.result=result;self.context=context
            rows=[{**r,'display_score':f"{r['score']:.1%}" if r['prediction_available'] else '暂不输出','reason_text':'；'.join(r['reasons']) or '模型候选，待核验'} for r in result['predictions']]
            self.app.fill_tree(self.tree,rows,['alias','role','level','display_score','reason_text'])
            self.summary.set(f"联合模型 {result['model_id']} · 资金模式分数 {result['fund_score']:.3f} · 控制方向候选 {len(result['control_candidates'])} 条 · 原始媒体 {len(result['media'])} 份 · 结果来源 {result['origin']}")
            lines=[result['scope'],'未见域字段：'+str(result['unseen_domain_fields']),'缺少已训练模态：'+str(result['missing_modalities']),'未训练而忽略的输入模态：'+str(result.get('ignored_untrained_modalities',[])),'','控制方向候选（有监督，不证明控制）：']
            lines.extend(f"{r['from']} → {r['to']}；分数 {r['score']:.3f}；事件依据 {len(r['evidence'])} 条" for r in result['control_candidates'])
            lines+=['','四流Shapley模型贡献：',json.dumps(result['explanation']['flow_shapley'],ensure_ascii=False,indent=2),'','多目标核验优先级：']
            lines.extend(f"{r['question']}；优先级 {r['weighted_priority']:.3f}；偏好扰动名次 {r['rank_range']}" for r in result['multiobjective']['candidates'][:15])
            lines+=['',result['explanation']['limitations'],'','完整报告：'+str(self.output)];self.app.set_text(self.text,'\n'.join(lines));self.app.store.audit('joint_predicted',{'model_id':result['model_id'],'dataset':dataset.fingerprint,'case_id':result['case_id']})
        self.app.run_job(work,done,'正在联合研判、计算四流解释和多目标排序…')
    def audit(self):
        if self.busy():return
        dataset=self.app.dataset
        def work():
            from .hd_eia import quality_audit
            return quality_audit(dataset)
        self.app.run_job(work,lambda r:self.app.set_text(self.text,json.dumps(r,ensure_ascii=False,indent=2)),'正在检查双图、媒体锚点、指纹及采样范围…')
    def validate(self):
        datasets={label:self.root/'data'/folder for label,folder in [('未见地区','advanced_region_holdout_synthetic'),('未见案型','advanced_type_holdout_synthetic'),('同时未见地区案型','advanced_joint_holdout_synthetic')]}
        self.run_validation(datasets)
    def validate_external(self):
        if self.busy():return
        path=filedialog.askdirectory(title='选择有独立标签的外部评测数据目录')
        if path:self.run_validation({'用户提供的外部数据':Path(path)})
    def run_validation(self,datasets):
        if self.busy():return
        def work():
            from .domain_validation import evaluate_domains
            return evaluate_domains(self.ensure_model(),datasets,self.root/'runtime/domain_runs')
        def done(value):
            self.output,result=value;self.app.set_text(self.text,json.dumps({**result,'runs':[{'name':r['name'],'origin':r['origin'],'fund':r['metrics']['fund'],'role_macro_f1':r['metrics']['roles']['macro_f1'],'control':r['metrics']['displayed_control_candidates'],'coverage':r['metrics']['selective']['coverage']} for r in result['runs']]},ensure_ascii=False,indent=2));self.summary.set('独立域评测已完成，错例和拒判保留在输出中；合成表现不等于真实泛化。')
        self.app.run_job(work,done,'正在冻结模型条件下评测外部地区/案型…')
    def template(self):
        if self.busy():return
        parent=filedialog.askdirectory(title='选择高级模板保存父目录')
        if not parent:return
        from .advanced_templates import create_advanced_template
        path=create_advanced_template(Path(parent)/datetime.now(TZ).strftime('双图高级模板_%Y%m%d_%H%M%S_%f'));self.output=path;self.summary.set('高级空模板已导出：'+str(path))
    def open_output(self):
        if self.output and Path(self.output).exists():os.startfile(self.output)
    def report_additions(self):
        if self.context!=self.current_context():return None
        return self.result
