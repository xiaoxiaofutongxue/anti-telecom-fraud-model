"""任务定义与隔离分组配置；写新副本以保留原始数据。"""
from pathlib import Path
from datetime import datetime
import json
import tkinter as tk
from tkinter import ttk,scrolledtext,messagebox
from .core import TZ
from .dataset_config import group_template,configure_copy


class ConfigurationWindow(tk.Toplevel):
    def __init__(self,app):
        super().__init__(app);self.app=app;self.dataset=app.dataset;self.title('配置监督任务与实证隔离分组')
        width=min(1100,self.winfo_screenwidth()-80);height=min(820,max(640,self.winfo_screenheight()-110));self.geometry(f'{width}x{height}');self.minsize(min(width,950),min(height,650))
        ttk.Label(self,text='定义标签含义，并按已知关联填写分组。同团伙/同一人员跨案件应使用相同组号；不要把未知关系当作独立。',wraplength=1000,padding=12).pack(fill='x')
        bottom=ttk.Frame(self,padding=12);bottom.pack(side='bottom',fill='x');self.reviewer=tk.StringVar();self.note=tk.StringVar()
        ttk.Label(bottom,text='配置人').grid(row=0,column=0);ttk.Entry(bottom,textvariable=self.reviewer,width=18).grid(row=0,column=1,padx=5);ttk.Label(bottom,text='依据/范围说明').grid(row=0,column=2);ttk.Entry(bottom,textvariable=self.note).grid(row=0,column=3,sticky='ew',padx=5);bottom.columnconfigure(3,weight=1);ttk.Button(bottom,text='保存到新副本并载入',command=self.save,style='Accent.TButton').grid(row=1,column=0,columnspan=4,sticky='e',pady=8)
        tabs=ttk.Notebook(self);tabs.pack(fill='both',expand=True,padx=12,pady=5);task=ttk.Frame(tabs,padding=10);groups=ttk.Frame(tabs,padding=10);tabs.add(task,text='标签任务定义');tabs.add(groups,text='关联分组CSV')
        self.task=scrolledtext.ScrolledText(task,wrap='word',padx=10,pady=8);self.task.pack(fill='both',expand=True)
        config={k:self.dataset.manifest.get(k,{}) for k in ('label_definition','role_task')}
        if self.dataset.manifest.get('advanced'):config['control_task']=self.dataset.manifest['advanced']['control_task']
        self.task.insert('1.0',json.dumps(config,ensure_ascii=False,indent=2))
        ttk.Label(groups,text='每个真实案件至少填写一条case_group。group_id填经核实的关联组号，确认后将confirmed改为true；可追加person/organization/source_batch分组。source_id须来自当前数据。',wraplength=1000,padding=6).pack(fill='x')
        self.groups=scrolledtext.ScrolledText(groups,wrap='none',font=('Consolas',10),padx=10,pady=8);self.groups.pack(fill='both',expand=True)
        value=self.dataset.manifest.get('evaluation_groups');self.groups.insert('1.0',(self.dataset.root/value).read_text(encoding='utf-8-sig') if value else group_template(self.dataset))
    def save(self):
        if self.app.busy:return
        try:config=json.loads(self.task.get('1.0','end'));group_csv=self.groups.get('1.0','end').strip();reviewer=self.reviewer.get();note=self.note.get()
        except ValueError as exc:return messagebox.showerror('配置格式错误',str(exc),parent=self)
        destination=self.app.root_dir/'data/configured'/datetime.now(TZ).strftime('dataset_%Y%m%d_%H%M%S_%f')
        def done(dataset):
            self.app.apply_dataset(dataset)
            if self.winfo_exists():self.destroy()
            self.app.status.set('任务与关联分组已保存到新副本：'+str(destination))
        self.app.run_job(lambda:configure_copy(self.dataset,destination,config,group_csv,reviewer,note),done,'正在校验任务、分组并保留原数据副本…')
