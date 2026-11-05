"""单机实证与运维工作台：接入、流程、留出评测、版本及可恢复备份。"""
from pathlib import Path
from datetime import datetime
import json,os,threading
import tkinter as tk
from tkinter import ttk,filedialog,messagebox,scrolledtext
from .core import TZ,load_dataset
from .empirical import criteria_template

CRITERIA_LABELS={'min_cases':'最少有标签案件','min_positive_cases':'最少正类案件','min_negative_cases':'最少负类案件','min_independent_groups':'最少独立关联组','min_labelled_persons':'最少有标签人员','min_coverage':'最低评分覆盖率','min_precision':'最低精确率','min_recall':'最低召回率','min_joint_accuracy':'最低角色与层级同时正确率','min_role_macro_f1':'最低角色宏F1','min_level_macro_f1':'最低层级宏F1','min_person_coverage':'最低人员覆盖率','min_control_precision':'最低控制候选精确率','min_control_recall':'最低控制候选召回率'}


def analysis_text(state,directory):
    lines=['本地全流程已完成',f"案件：{state['case_id']} · 来源：{'合成演示' if state['origin']=='synthetic' else '真实数据'}",f"事件覆盖率：{state['record_coverage']:.1%} · 模型窗口：{state['model_windows']}",f"模式提示：{state['finding_count']} · 产业订单：{state['industry_orders']} · IP：{state['ip_count']}",'','完成步骤：']
    lines.extend('✓ '+r['name'] for r in state['stages'] if r['status']=='passed')
    lines+=['','需要继续核对：',*(state['next_actions'] or ['请对模型候选与来源逐项进行业务核验。']),'','点击“打开输出”查看图谱、统计和完整来源报告。','输出目录：'+str(directory)]
    return '\n'.join(lines)


def evaluation_text(result,directory):
    labels={'labelled_cases':'有标签案件','positive_cases':'正类案件','negative_cases':'负类案件','independent_groups':'独立关联组','coverage':'评分覆盖率','precision':'精确率','recall':'召回率','f1':'F1','labelled_persons':'有标签人员','eligible_persons':'可用人员候选','joint_accuracy':'角色与层级同时正确率','role_macro_f1':'角色宏F1','level_macro_f1':'层级宏F1','person_coverage':'人员覆盖率','control_precision':'控制候选精确率','control_recall':'控制候选召回率'}
    lines=['冻结模型外部评测',f"来源：{'合成演示，不能登记真实实证通过' if result['origin']=='synthetic' else '真实数据'}",'模型：'+result['model_id'],'']
    for key,label in labels.items():
        value=result['summary'].get(key)
        if value is not None:lines.append(label+'：'+(f'{value:.1%}' if isinstance(value,float) else str(value)))
    lines+=['','业务规则：'+('达到本次填写的标准' if result['criteria'].get('passed') else '尚未达到/尚未设置标准')]
    for row in result['criteria'].get('checks',[]):lines.append(('✓ ' if row['passed'] else '需关注 ')+CRITERIA_LABELS.get(row['criterion'],row['criterion'])+f"：实际 {row['actual']}，最低 {row['minimum']}")
    lines+=['',f"资金误报/漏报：{len(result['fund_errors'])} 条",'拒判、人员错例、分地区/案型结果与区间见完整报告。',result['scope'],'输出目录：'+str(directory)]
    return '\n'.join(lines)


class PracticalPanel:
    def __init__(self,app,parent):
        self.app=app;self.root=app.root_dir;self.output=None;self.evaluation_output=None;self.evaluation_model=None;self.cancel_event=threading.Event();self.models=[]
        tabs=ttk.Notebook(parent);tabs.pack(fill='both',expand=True)
        run=ttk.Frame(tabs,padding=10);evaluation=ttk.Frame(tabs,padding=10);versions=ttk.Frame(tabs,padding=10);backup=ttk.Frame(tabs,padding=10)
        for page,title in [(run,'接入与全流程'),(evaluation,'外部实证评测'),(versions,'模型版本'),(backup,'备份与恢复')]:tabs.add(page,text=title)
        bar=ttk.Frame(run);bar.pack(fill='x')
        for label,command in [('CSV/Excel接入',self.intake),('任务与分组配置',self.configure),('一键本地全流程',self.analyze),('停止编排',self.cancel),('环境自检',self.health),('打开输出',self.open_output)]:ttk.Button(bar,text=label,command=command).pack(side='left',padx=3)
        ttk.Label(run,text='流程包含来源检查、金额、模式、IP/产业溯源、适配模型、分窗与报告。不会自动调证、绑定身份或处置人员。',padding=8,wraplength=1100).pack(fill='x')
        self.run_text=scrolledtext.ScrolledText(run,wrap='word',padx=12,pady=10);self.run_text.pack(fill='both',expand=True)
        app.set_text(self.run_text,'先接入并核对数据，再运行完整流程。真实数据可先使用统计、图谱和人工核验；自带合成模型不会给真实数据评分。\n大图按事件分窗，并报告覆盖率，不把窗口分数合成整案概率。')
        self.build_evaluation(evaluation);self.build_versions(versions)
        row=ttk.Frame(backup);row.pack(fill='x')
        for label,command in [('创建当前工作区备份',self.backup),('校验已有备份',self.verify_backup),('恢复到新的空目录',self.restore),('打开输出目录',self.open_output)]:ttk.Button(row,text=label,command=command).pack(side='left',padx=3)
        ttk.Label(backup,text='备份包含当前数据原件、核验库、程序和已选模型；不会覆盖现用目录。备份是本地私有文件，未加密，请按单位数据要求保管。',wraplength=1100,padding=10).pack(fill='x')
        self.backup_text=scrolledtext.ScrolledText(backup,wrap='word',padx=12,pady=10);self.backup_text.pack(fill='both',expand=True)
    def intake(self):
        if self.app.busy:return
        from .intake_ui import IntakeWindow
        IntakeWindow(self.app)
    def configure(self):
        if self.app.busy or self.app.dataset is None:return
        from .config_ui import ConfigurationWindow
        ConfigurationWindow(self.app)
    def health(self):
        from .operations import environment_check
        self.app.run_job(lambda:environment_check(self.root,self.app.dataset),lambda r:self.app.set_text(self.run_text,'环境自检：'+('通过' if r['ready'] else '有项目需处理')+'\n\n'+'\n'.join(f"{row['status']} · {row['item']}：{row['detail']}" for row in r['checks'])+'\n\n'+r['scope']),'正在核对依赖、模型、来源与审计…')
    def analyze(self):
        if self.app.busy or self.app.dataset is None:return
        dataset=self.app.dataset;graph=self.app.graph.copy();self.cancel_event.clear()
        models={'fund':self.app.model,'roles':self.app.research.role_model}
        def work():
            from .orchestrator import run_analysis
            if dataset.manifest.get('advanced'):
                try:models['joint']=self.app.research.advanced.ensure_model()
                except (OSError,ValueError,KeyError):pass
            return run_analysis(dataset,graph,self.app.store,self.root/'runtime/analysis_runs',models,self.cancel_event,lambda s:self.app.jobs.put(('progress',s,None)))
        def done(value):self.output,state=value;self.app.set_text(self.run_text,analysis_text(state,self.output));self.app.status.set('全流程完成：'+str(self.output))
        self.app.run_job(work,done,'正在编排本地分析步骤…')
    def cancel(self):
        if self.app.busy:self.cancel_event.set();self.app.status.set('已申请停止编排，将在当前步骤完成后退出；独立训练/识别任务不受此按钮影响。')
    def build_evaluation(self,page):
        bar=ttk.Frame(page);bar.pack(fill='x');self.kind=tk.StringVar(value='fund');combo=ttk.Combobox(bar,textvariable=self.kind,values=['fund','roles','joint'],state='readonly',width=12);combo.pack(side='left');combo.bind('<<ComboboxSelected>>',lambda e:self.set_criteria())
        self.model_path=tk.StringVar();ttk.Entry(bar,textvariable=self.model_path).pack(side='left',fill='x',expand=True,padx=5);ttk.Button(bar,text='选择冻结模型',command=self.choose_eval_model).pack(side='left');ttk.Button(bar,text='生成实证报告',command=self.evaluate,style='Accent.TButton').pack(side='left',padx=4)
        row=ttk.Frame(page);row.pack(fill='x',pady=7);self.dataset_path=tk.StringVar();ttk.Label(row,text='独立留出数据').pack(side='left');ttk.Entry(row,textvariable=self.dataset_path).pack(side='left',fill='x',expand=True,padx=5);ttk.Button(row,text='选择数据目录',command=self.choose_eval_data).pack(side='left')
        ttk.Label(page,text='下列阈值只是可编辑示例，需按实证目的由业务人员确定。评测不训练、不调阈值；真实评测需声明关联案件/人员分组，未知标签不作负类。',wraplength=1100,padding=6).pack(fill='x')
        self.criteria_frame=ttk.Frame(page);self.criteria_frame.pack(fill='x');self.criteria_vars={};self.set_criteria()
        review=ttk.Frame(page);review.pack(fill='x',pady=7);self.reviewer=tk.StringVar();self.note=tk.StringVar();self.accept=tk.BooleanVar(value=False)
        ttk.Label(review,text='复核人').grid(row=0,column=0);ttk.Entry(review,textvariable=self.reviewer,width=17).grid(row=0,column=1,padx=4);ttk.Label(review,text='依据/限制').grid(row=0,column=2);ttk.Entry(review,textvariable=self.note).grid(row=0,column=3,sticky='ew',padx=4);review.columnconfigure(3,weight=1)
        ttk.Checkbutton(review,text='确认真实外部标签与业务规则已核对',variable=self.accept).grid(row=1,column=0,columnspan=4,sticky='w');ttk.Button(review,text='登记人工复核',command=self.review).grid(row=1,column=4);ttk.Button(review,text='打开评测输出',command=lambda:self.open_output(self.evaluation_output)).grid(row=0,column=4)
        self.eval_text=scrolledtext.ScrolledText(page,wrap='word',padx=10,pady=8);self.eval_text.pack(fill='both',expand=True)
    def set_criteria(self):
        for widget in self.criteria_frame.winfo_children():widget.destroy()
        self.criteria_vars={}
        for i,(key,value) in enumerate(criteria_template(self.kind.get()).items()):
            row=i//3;column=(i%3)*2;var=tk.StringVar(value=str(value));self.criteria_vars[key]=var
            ttk.Label(self.criteria_frame,text=CRITERIA_LABELS[key]).grid(row=row,column=column,sticky='w',padx=(5,3),pady=3)
            ttk.Entry(self.criteria_frame,textvariable=var,width=7).grid(row=row,column=column+1,sticky='w',padx=(0,10))
    def choose_eval_model(self):
        value=filedialog.askdirectory(title='选择模型卡与权重所在目录')
        if value:self.model_path.set(value)
    def choose_eval_data(self):
        value=filedialog.askdirectory(title='选择完全独立的有标签留出数据目录')
        if value:self.dataset_path.set(value)
    def evaluate(self):
        if self.app.busy:return
        try:
            kind=self.kind.get();criteria={k:int(v.get()) if isinstance(criteria_template(kind)[k],int) else float(v.get()) for k,v in self.criteria_vars.items()};model_path=Path(self.model_path.get()).resolve();data_path=Path(self.dataset_path.get()).resolve()
        except ValueError as exc:return messagebox.showerror('标准格式有误',str(exc))
        def work():
            from .empirical import load_model,evaluate_external
            model=load_model(model_path,kind);return evaluate_external(model,load_dataset(data_path),kind,self.root/'runtime/empirical_runs',criteria,lambda s:self.app.jobs.put(('progress',s,None)))
        def done(value):
            self.evaluation_output,result=value;self.evaluation_model=model_path;self.accept.set(False);self.app.set_text(self.eval_text,evaluation_text(result,self.evaluation_output))
        self.app.run_job(work,done,'正在冻结模型上执行独立留出实证…')
    def review(self):
        if self.app.busy or not self.evaluation_output:return
        try:
            from .empirical import record_review
            path=record_review(self.root,self.evaluation_output,self.evaluation_model,self.reviewer.get(),self.note.get(),self.accept.get());self.app.store.audit('empirical_review',{'record':str(path),'reviewer':self.reviewer.get(),'accepted':self.accept.get()});self.app.status.set('人工复核记录已保存：'+str(path))
        except (ValueError,OSError) as exc:messagebox.showerror('未登记复核',str(exc))
    def build_versions(self,page):
        bar=ttk.Frame(page);bar.pack(fill='x');ttk.Button(bar,text='刷新版本列表',command=self.refresh_models).pack(side='left');ttk.Button(bar,text='使用所选版本/回退',command=self.use_model).pack(side='left',padx=6)
        ttk.Label(page,text='只切换模型引用，不删除旧权重；数据、任务和来源不匹配时仍拒绝评分。合成模型不能通过人工勾选变成真实验证模型。',wraplength=1100,padding=8).pack(fill='x')
        self.model_tree=self.app.make_tree(page,[('id','模型编号',240),('kind','类型',110),('origin','来源',100),('status','实证记录状态',280)],height=10)
        self.model_detail=scrolledtext.ScrolledText(page,height=8,wrap='word',padx=10,pady=8);self.model_detail.pack(fill='both',expand=True);self.model_tree.bind('<<TreeviewSelect>>',self.model_selection)
    def refresh_models(self):
        if self.app.busy:return
        def work():
            from .empirical import model_status
            result=[]
            for path in (self.root/'runtime/models').glob('*/model_card.json'):
                try:
                    card=json.loads(path.read_text(encoding='utf-8'));kind='joint' if card.get('kind')=='dual_flow_joint' else 'roles' if card.get('kind')=='role_hierarchy_gnn' else 'fund';result.append({'id':card.get('model_id',path.parent.name),'kind':kind,'origin':card.get('origin','未知'),'status':model_status(self.root,card),'path':path.parent,'card':card})
                except (ValueError,OSError):continue
            return sorted(result,key=lambda r:r['id'],reverse=True)
        def done(rows):self.models=rows;self.app.fill_tree(self.model_tree,rows,['id','kind','origin','status'])
        self.app.run_job(work,done,'正在读取模型版本清单…')
    def model_selection(self,event=None):
        selected=self.model_tree.selection()
        if selected:self.app.set_text(self.model_detail,json.dumps(self.models[int(selected[0])]['card'],ensure_ascii=False,indent=2))
    def use_model(self):
        selected=self.model_tree.selection()
        if self.app.busy or not selected:return
        row=self.models[int(selected[0])]
        def work():
            from .empirical import load_model
            return load_model(row['path'],row['kind'])
        def done(model):
            from .io_utils import point_model
            pointer={'fund':'latest.json','roles':'roles_latest.json','joint':'joint_latest.json'}[row['kind']];point_model(self.root,pointer,row['path'])
            if row['kind']=='fund':self.app.model=model
            elif row['kind']=='roles':self.app.research.role_model=model
            else:self.app.research.advanced.model=model;self.app.research.advanced.clear_result('联合模型版本变化，请重新分析。')
            self.app.store.audit('model_version_selected',{'model_id':row['id'],'kind':row['kind'],'origin':row['origin']});self.app.refresh()
        self.app.run_job(work,done,'正在校验并切换所选模型版本…')
    def backup(self):
        if self.app.busy or self.app.dataset is None:return
        parent=filedialog.askdirectory(title='选择私有备份保存父目录')
        if not parent:return
        from .operations import backup_workspace
        dataset=self.app.dataset
        def done(path):self.output=path.parent;self.app.set_text(self.backup_text,'备份已完成并校验：\n'+str(path)+'\n保留了数据、核验库和已选模型；恢复时选择新的空目录。')
        self.app.run_job(lambda:backup_workspace(self.root,dataset,parent),done,'正在生成一致的SQLite与工作区备份…')
    def verify_backup(self):
        if self.app.busy:return
        path=filedialog.askopenfilename(title='选择工作区备份',filetypes=[('ZIP备份','*.zip')])
        if not path:return
        from .operations import verify_backup
        self.app.run_job(lambda:verify_backup(path),lambda r:self.app.set_text(self.backup_text,json.dumps(r,ensure_ascii=False,indent=2)),'正在逐文件校验备份…')
    def restore(self):
        if self.app.busy:return
        path=filedialog.askopenfilename(title='选择需要恢复的工作区备份',filetypes=[('ZIP备份','*.zip')])
        if not path:return
        parent=filedialog.askdirectory(title='选择恢复父目录，将自动新建恢复文件夹')
        if not parent:return
        destination=Path(parent)/datetime.now(TZ).strftime('FourFlowAnalysis_restored_%Y%m%d_%H%M%S_%f')
        from .operations import restore_backup
        def done(result):self.output=result;self.app.set_text(self.backup_text,'已恢复到新目录：\n'+str(result)+'\n原工作区未改写。当前本机可用原项目解释器运行恢复目录的app.py；其他电脑先按锁定依赖安装。')
        self.app.run_job(lambda:restore_backup(path,destination),done,'正在校验并恢复到新的目录…')
    def open_output(self,path=None):
        path=path or self.output
        if path and Path(path).exists():os.startfile(path)
