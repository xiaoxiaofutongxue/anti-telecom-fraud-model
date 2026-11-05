"""人员双头模型、假设驱动因果研究及本地原始材料识别的桌面界面。"""
from .io_utils import atomic_json,point_model
import hashlib
import json
import os
import tkinter as tk
from tkinter import ttk,filedialog,messagebox,scrolledtext
from pathlib import Path
from datetime import datetime

import networkx as nx

from .core import TZ,load_dataset
from .roles import RoleHierarchyModel,train_roles
from .causal import ASSUMPTIONS,load_study,estimate_study,save_causal_run
from .multimodal import LocalMediaProcessor,save_extraction,draft_communication


class ResearchPanel:
    def __init__(self,app,parent):
        self.app=app;self.root=app.root_dir;self.role_model=None;self.role_result={'available':False,'reason':'尚未加载角色模型'}
        self.study=None;self.causal_result=None;self.causal_output=None;self.material=None;self.material_output=None;self.media_context=None;self.processor=LocalMediaProcessor(self.root)
        tabs=ttk.Notebook(parent);tabs.pack(fill='both',expand=True)
        roles=ttk.Frame(tabs,padding=8);causal=ttk.Frame(tabs,padding=8);media=ttk.Frame(tabs,padding=8)
        tabs.add(roles,text='人员角色与层级');tabs.add(causal,text='因果分析与偏差');tabs.add(media,text='原始材料识别')
        self.build_roles(roles);self.build_causal(causal);self.build_media(media)
        from .advanced_ui import AdvancedPanel
        advanced=ttk.Frame(tabs,padding=8);tabs.add(advanced,text='双图·多模态联合研判');self.advanced=AdvancedPanel(app,advanced)
        pointer=self.root/'runtime/models/roles_latest.json'
        if pointer.is_file():
            try:
                directory=Path(json.loads(pointer.read_text(encoding='utf-8'))['path'])
                if not directory.is_absolute():directory=self.root/directory
                self.role_model=RoleHierarchyModel.load(directory)
            except Exception as exc:self.role_result={'available':False,'reason':'角色模型载入提示：'+str(exc)}

    def build_roles(self,page):
        bar=ttk.Frame(page);bar.pack(fill='x')
        ttk.Button(bar,text='载入角色合成样例',command=lambda:self.app.load_path(self.root/'data/enhanced_sample')).pack(side='left',padx=4)
        ttk.Button(bar,text='合成角色训练验证',command=lambda:self.start_roles(True)).pack(side='left',padx=4)
        ttk.Button(bar,text='用当前标签训练',command=lambda:self.start_roles(False),style='Accent.TButton').pack(side='left',padx=4)
        ttk.Button(bar,text='载入角色模型',command=self.choose_role_model).pack(side='left',padx=4)
        ttk.Label(page,text='双头图神经网络预测角色和层级，原始标注保持独立。预测及上下层联系均需人工核验，不能直接认定团伙。',padding=6).pack(anchor='w')
        self.role_tree=self.app.make_tree(page,[('alias','主体',160),('role','候选角色',160),('level','候选层级',140),('score','允许组合分数',150),('status','状态',320)],height=7)
        self.role_tree.bind('<<TreeviewSelect>>',self.select_role)
        self.role_text=scrolledtext.ScrolledText(page,height=12,wrap='word',relief='flat',padx=12,pady=10);self.role_text.pack(fill='both',expand=True)

    def refresh(self):
        app=self.app
        if not app.dataset:return
        self.advanced.refresh()
        context=(app.dataset.fingerprint,app.current_case(),app.graph.graph.get('as_of',''))
        if context!=self.media_context:
            self.media_context=context;self.reset_material_confirmation()
        if self.role_model:
            try:self.role_result=self.role_model.predict(app.dataset,app.graph)
            except Exception as exc:self.role_result={'available':False,'reason':str(exc)}
        rows=[{**r,'score':f"{r['joint_probability']:.1%}" if r.get('prediction_available',True) else '暂不输出','status':r['status']} for r in self.role_result.get('predictions',[])]
        app.fill_tree(self.role_tree,rows,['alias','role','level','score','status'])
        if not self.role_result.get('available'):
            app.set_text(self.role_text,self.role_result.get('reason','角色预测不可用')+'\n可点击“载入角色合成样例”验证流程；真实角色模型需提供person_labels.csv。')
        else:
            m=self.role_model.metadata;metrics=m.get('metrics',{}).get('test',{})
            lines=['模型：'+m['model_id'],'来源：'+('合成验证模型' if m['origin']=='synthetic' else '经提供方标注的数据训练模型'),f"独立留出集：{metrics.get('roles',{}).get('n',0)}个人员，角色宏F1 {metrics.get('roles',{}).get('macro_f1',0):.4f}，层级宏F1 {metrics.get('levels',{}).get('macro_f1',0):.4f}",self.role_result['interpretation']]
            if self.role_result.get('known_split') or self.role_result.get('related_splits'):lines.append('当前图或资产标识与训练/评估集合重合，不能作为独立外部验证。')
            lines.append('点击人员行查看两个输出头的分布和来源联系。')
            lines.append(f"当前可用候选 {self.role_result.get('eligible_count',len(rows))} 人；暂不分类 {self.role_result.get('abstained_count',0)} 人。")
            app.set_text(self.role_text,'\n\n'.join(lines))
        actors=app.dataset.rows('actors',app.current_case());values=[r['actor_id']+'  '+r['alias'] for r in actors]
        self.media_from_combo.configure(values=values);self.media_to_combo.configure(values=values)

    def reset_material_confirmation(self):
        self.media_confirm.set(False)
        for var in (self.media_from,self.media_to,self.media_time,self.media_reviewer):var.set('')

    def wait_for_job(self):
        if self.app.busy:
            messagebox.showinfo('正在处理','请等待当前任务完成，再切换研究或材料。');return True
        return False

    def select_role(self,event=None):
        selected=self.role_tree.selection()
        if not selected:return
        row=self.role_result['predictions'][int(selected[0])]
        lines=[row['alias']+'：'+row['role']+' / '+row['level'],row['status'],'；'.join(row.get('abstention_reasons',[])),f"有效关联 {row.get('support_records','未知')} 条；人员输入偏离值 {row.get('feature_shift','未知')}",'内部角色分布（暂不分类时不作为可用结论）：'+'；'.join(f'{k} {v:.1%}' for k,v in row['role_distribution'].items()),'内部层级分布：'+'；'.join(f'{k} {v:.1%}' for k,v in row['level_distribution'].items()),'','候选上下层联系（方向由预测层级组织，不等于控制关系）：']
        for link in self.role_result['hierarchy_links']:
            if row['node'] in (link['upper'],link['lower']):lines.append(f"{link['upper']} → {link['lower']}；记录 {', '.join(link['record_ids'])}；来源 {', '.join(link['source_ids'])}")
        self.app.set_text(self.role_text,'\n'.join(lines))

    def start_roles(self,synthetic):
        try:
            training=self.root/'data/stress_training_synthetic'
            dataset=load_dataset(training if training.exists() else self.root/'data/role_training_synthetic') if synthetic else self.app.dataset
            output=self.root/'runtime/models'/datetime.now(TZ).strftime('roles_%Y%m%d_%H%M%S_%f')
            def done(model):
                self.role_model=model;point_model(self.root,'roles_latest.json',output)
                self.app.store.audit('roles_trained',{'model_id':model.metadata['model_id'],'origin':dataset.manifest['origin'],'test':model.metadata['metrics']['test']});self.refresh()
            self.app.run_job(lambda:train_roles(dataset,output,100,lambda s:self.app.jobs.put(('progress',s,None))),done,'正在训练人员角色与层级模型…')
        except Exception as exc:messagebox.showerror('角色训练未开始',str(exc))

    def choose_role_model(self):
        if self.wait_for_job():return
        directory=filedialog.askdirectory(title='选择角色模型目录')
        if not directory:return
        try:
            self.role_model=RoleHierarchyModel.load(directory)
            point_model(self.root,'roles_latest.json',directory);self.refresh()
        except Exception as exc:messagebox.showerror('角色模型未加载',str(exc))

    def build_causal(self,page):
        bar=ttk.Frame(page);bar.pack(fill='x')
        ttk.Button(bar,text='载入合成因果研究',command=lambda:self.choose_study(self.root/'data/causal_demo')).pack(side='left',padx=4)
        ttk.Button(bar,text='选择研究目录',command=lambda:self.choose_study()).pack(side='left',padx=4)
        ttk.Button(bar,text='查看声明因果图',command=self.show_dag).pack(side='left',padx=4)
        ttk.Button(bar,text='执行调整与稳健性检查',command=self.run_causal,style='Accent.TButton').pack(side='left',padx=4)
        ttk.Label(page,text='队列层面的条件效应研究。声明因果图不是已证实的个案证据链；无未测混杂等假设不能由程序自动证明。',padding=8).pack(anchor='w')
        flags=ttk.Frame(page);flags.pack(fill='x');self.assumption_vars={}
        labels={'consistency':'处理定义一致','no_unmeasured_confounding':'无未测混杂','positivity':'存在可比处理/对照','pre_treatment_covariates':'协变量先于处理','independent_groups':'组间独立'}
        for key in ASSUMPTIONS:
            var=tk.BooleanVar(value=False);self.assumption_vars[key]=var;ttk.Checkbutton(flags,text=labels[key],variable=var).pack(side='left',padx=5)
        line=ttk.Frame(page);line.pack(fill='x',pady=6)
        ttk.Label(line,text='假设确认人').pack(side='left');self.causal_reviewer=tk.StringVar();ttk.Entry(line,textvariable=self.causal_reviewer,width=28).pack(side='left',padx=7)
        self.attach_causal=tk.BooleanVar(value=False);ttk.Checkbutton(line,text='报告附独立队列研究（不作为当前个案结论）',variable=self.attach_causal).pack(side='left',padx=12)
        ttk.Button(line,text='打开本次研究输出',command=lambda:self.open_output(self.causal_output)).pack(side='right')
        self.causal_text=scrolledtext.ScrolledText(page,wrap='word',relief='flat',padx=12,pady=10);self.causal_text.pack(fill='both',expand=True)
        self.app.set_text(self.causal_text,'请选择研究目录：causal_design.json、observations.csv、sources.csv。\n可先载入已知真实生成效应的合成研究，验证去偏估计流程。')

    def choose_study(self,directory=None):
        if self.wait_for_job():return
        directory=directory or filedialog.askdirectory(title='选择含causal_design.json的研究目录')
        if not directory:return
        try:
            self.study=load_study(directory,require_ack=False);d=self.study['design'];self.causal_result=None;self.attach_causal.set(False)
            for key,var in self.assumption_vars.items():var.set(d['origin']=='synthetic' and d.get('assumptions',{}).get(key) is True)
            self.causal_reviewer.set(d.get('acknowledged_by','') if d['origin']=='synthetic' else '')
            lines=['研究：'+d.get('question',d['task_id']),'来源：'+d['origin'],f"观测 {len(self.study['rows'])} 条，独立组 {len(set(self.study['groups']))} 个",'处理：'+d['treatment'],'结果：'+d['outcome'],'处理前调整变量：'+'、'.join(d['adjustment']),'','请复核因果图、变量时间和假设，再执行估计。真实研究的假设确认不会由程序代填。']
            self.app.set_text(self.causal_text,'\n\n'.join(lines))
        except Exception as exc:messagebox.showerror('研究未加载',str(exc))

    def show_dag(self):
        if not self.study:return
        graph=nx.DiGraph();graph.add_nodes_from(self.study['design']['dag']['nodes']);graph.add_edges_from(self.study['design']['dag']['edges'])
        window=tk.Toplevel(self.app);window.title('声明的因果图及假设');window.geometry('1000x620')
        ttk.Label(window,text='这是待业务确认的结构假设，不是从图谱自动证明的真实因果关系。',padding=12).pack(fill='x')
        canvas=tk.Canvas(window,bg='white');canvas.pack(fill='both',expand=True);pos=nx.spring_layout(graph,seed=19)
        coordinates={n:(110+(p[0]+1)*370,80+(p[1]+1)*190) for n,p in pos.items()}
        for a,b in graph.edges():canvas.create_line(*coordinates[a],*coordinates[b],arrow=tk.LAST,fill='#64748b',width=2)
        for node,(x,y) in coordinates.items():canvas.create_oval(x-38,y-22,x+38,y+22,fill='#dbeafe',outline='#94a3b8');canvas.create_text(x,y,text=node,font=('Microsoft YaHei UI',9))

    def run_causal(self):
        if not self.study:return messagebox.showinfo('因果研究','请先载入研究数据。')
        study=dict(self.study);design=dict(study['design']);design['assumptions']={k:v.get() for k,v in self.assumption_vars.items()};design['acknowledged_by']=self.causal_reviewer.get().strip();study['design']=design
        study['fingerprint']=hashlib.sha256((self.study['fingerprint']+json.dumps(design,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()
        def work():return save_causal_run(study,estimate_study(study,12),self.root/'runtime/causal_runs')
        def done(value):
            self.causal_output,self.causal_result=value;r=self.causal_result
            lines=['研究：'+r['question'],r['scope'],r['identification'],f"观测 {r['n']} 条，独立组 {r['independent_groups']} 个",f"未调整差异：{r['naive_difference']:.4f}"]
            if r['available']:
                lines.extend([f"AIPW调整后总体效应：{r['estimate']:.4f}",f"近似95%区间：{r['confidence_interval_95'][0]:.4f} 至 {r['confidence_interval_95'][1]:.4f}",f"相对未调整差异的修正：{r['confounding_adjustment_delta']:.4f}"])
                placebo=r['refutations']['placebo_mean'];lines.append('置换处理压力检查均值：'+(f'{placebo:.4f}' if placebo is not None else '未执行'))
            else:lines.append(r['reason'])
            lines.extend(['','协变量平衡（标准化差异，越接近0通常越平衡）：'])
            lines.extend(f"{b['variable']}：调整前 {b['smd_before']:.3f} → 调整后 {b['smd_after']:.3f}" for b in r['diagnostics']['balance'])
            lines+=['',*r['warnings'],'','版本输出：'+str(self.causal_output)]
            self.app.set_text(self.causal_text,'\n\n'.join(lines));self.app.store.audit('causal_study_estimated',{'task_id':r['task_id'],'origin':r['origin'],'fingerprint':r['study_fingerprint'],'available':r['available'],'estimate':r.get('estimate')})
        self.app.run_job(work,done,'正在进行后门检查、分组交叉拟合与稳健性检查…')

    def build_media(self,page):
        page.columnconfigure(0,weight=1);page.rowconfigure(3,weight=1)
        bar=ttk.Frame(page);bar.grid(row=0,column=0,sticky='ew');bar.columnconfigure(0,weight=1)
        self.media_path=tk.StringVar();self.media_origin=tk.StringVar(value='real')
        ttk.Entry(bar,textvariable=self.media_path,width=30).grid(row=0,column=0,columnspan=3,sticky='ew',padx=4)
        ttk.Button(bar,text='选择材料',command=self.choose_material).grid(row=0,column=3,padx=4)
        ttk.Button(bar,text='合成图片演示',command=self.demo_material).grid(row=1,column=0,sticky='w',padx=4,pady=3)
        ttk.Combobox(bar,textvariable=self.media_origin,values=['real','synthetic'],state='readonly',width=10).grid(row=1,column=1,padx=4)
        ttk.Button(bar,text='本地识别',command=self.extract_material,style='Accent.TButton').grid(row=1,column=3,padx=4,pady=3)
        support=ttk.Label(page,text='支持图片、PDF、DOCX、文本、音频、视频抽帧/音轨。识别结果可能有误；所有原始材料留在本机，不自动生成交易或身份绑定。',padding=5);support.grid(row=1,column=0,sticky='ew');support.bind('<Configure>',lambda event:support.configure(wraplength=max(260,event.width-14)))
        self.pdf_hybrid=tk.BooleanVar(value=True);ttk.Checkbutton(page,text='PDF图文联读：图像页同时OCR，双视图仍属一份材料',variable=self.pdf_hybrid).grid(row=2,column=0,sticky='w',padx=7)
        self.media_text=scrolledtext.ScrolledText(page,height=4,wrap='word',relief='flat',padx=12,pady=7);self.media_text.grid(row=3,column=0,sticky='nsew')
        self.media_info=tk.StringVar(value='尚未识别材料。识别文本可人工修订，候选字段及位置另存输出目录。');info=ttk.Label(page,textvariable=self.media_info,padding=4);info.grid(row=4,column=0,sticky='ew');info.bind('<Configure>',lambda event:info.configure(wraplength=max(260,event.width-14)))
        form=ttk.Frame(page);form.grid(row=5,column=0,sticky='ew',pady=3);form.columnconfigure(1,weight=1);form.columnconfigure(3,weight=1)
        self.media_from=tk.StringVar();self.media_to=tk.StringVar();self.media_time=tk.StringVar();self.media_reviewer=tk.StringVar()
        for index,(title,var,width) in enumerate([('发送主体',self.media_from,18),('接收主体',self.media_to,18),('事件时间',self.media_time,23),('核验人',self.media_reviewer,16)]):
            row,column=divmod(index,2);column*=2;ttk.Label(form,text=title).grid(row=row,column=column,padx=3,sticky='w')
            if title in {'发送主体','接收主体'}:
                combo=ttk.Combobox(form,textvariable=var,state='readonly',width=width);combo.grid(row=row,column=column+1,sticky='ew',padx=3,pady=2)
                if title=='发送主体':self.media_from_combo=combo
                else:self.media_to_combo=combo
            else:ttk.Entry(form,textvariable=var,width=width).grid(row=row,column=column+1,sticky='ew',padx=3,pady=2)
        bottom=ttk.Frame(page);bottom.grid(row=6,column=0,sticky='ew');self.media_confirm=tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom,text='我已核对文本、双方主体及事件时间',variable=self.media_confirm).pack(side='left')
        ttk.Button(bottom,text='确认并生成/载入通信草稿',command=self.import_material).pack(side='right',padx=4)
        ttk.Button(bottom,text='打开识别输出',command=lambda:self.open_output(self.material_output)).pack(side='right',padx=4)

    def choose_material(self):
        if self.wait_for_job():return
        path=filedialog.askopenfilename(title='选择本地原始材料',filetypes=[('支持材料','*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.pdf *.docx *.txt *.csv *.json *.log *.wav *.mp3 *.m4a *.flac *.ogg *.aac *.wma *.mp4 *.mkv *.avi *.mov *.webm'),('所有文件','*.*')])
        if path:self.media_path.set(path);self.media_confirm.set(False)

    def demo_material(self):
        if self.wait_for_job():return
        self.media_path.set(str(self.root/'data/media_demo/synthetic_record.png'));self.media_origin.set('synthetic');self.extract_material()

    def extract_material(self):
        path=self.media_path.get().strip();origin=self.media_origin.get();pdf_mode='hybrid' if self.pdf_hybrid.get() else 'auto'
        if not path:return
        def work():
            result=self.processor.extract(path,origin,lambda s:self.app.jobs.put(('progress',s,None)),pdf_mode=pdf_mode)
            return result,save_extraction(result,self.root/'runtime/extractions')
        def done(value):
            self.material,self.material_output=value;self.reset_material_confirmation()
            self.media_path.set(self.material['source_path']);self.media_origin.set(self.material['origin'])
            self.media_text.delete('1.0','end');self.media_text.insert('1.0',self.material['text'])
            self.media_info.set(f"来源：{origin}；文本片段 {len(self.material['segments'])} 条，候选字段 {len(self.material['candidate_facts'])} 项。"+'；'.join(self.material['notes'])+' 输出：'+str(self.material_output))
            self.app.store.audit('local_material_extracted',{'source_sha256':self.material['source_sha256'],'origin':origin,'segments':len(self.material['segments']),'output':str(self.material_output)})
        self.app.run_job(work,done,'正在本地识别原始材料…')

    def import_material(self):
        if self.wait_for_job():return
        if not self.material or not self.media_confirm.get():return messagebox.showinfo('需要人工核对','请先识别材料，并核对文本、主体和时间后勾选确认。')
        if Path(self.media_path.get()).resolve()!=Path(self.material['source_path']).resolve() or self.media_origin.get()!=self.material['origin']:
            self.reset_material_confirmation();return messagebox.showinfo('材料选择已变化','当前选择与已识别材料不一致，请重新识别和核对。')
        parent=filedialog.askdirectory(title='选择保存父文件夹，系统将新建通信导入草稿目录')
        if not parent:return
        destination=Path(parent)/datetime.now(TZ).strftime('材料导入草稿_%Y%m%d_%H%M%S_%f')
        dataset=self.app.dataset;cid=self.app.current_case();sender=self.media_from.get().split('  ',1)[0];receiver=self.media_to.get().split('  ',1)[0];time=self.media_time.get();reviewer=self.media_reviewer.get();text=self.media_text.get('1.0','end').strip()
        def done(directory):self.app.apply_dataset(load_dataset(directory));self.media_confirm.set(False);self.app.status.set('已载入人工确认的通信草稿；原始数据集未改写：'+str(directory))
        self.app.run_job(lambda:draft_communication(dataset,self.material,destination,cid,sender,receiver,time,reviewer,text),done,'正在生成保留原始材料与核验依据的新数据草稿…')

    @staticmethod
    def open_output(path):
        if path and Path(path).exists():os.startfile(path)

    def report_additions(self):
        additions={'roles':self.role_result,'joint':self.advanced.report_additions()}
        if self.attach_causal.get() and self.causal_result:additions['cohort_causal']={'case_linked':False,'scope':'独立队列研究附件，不是当前个案或个人的因果结论','result':self.causal_result}
        return additions
