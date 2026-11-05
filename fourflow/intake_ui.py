"""CSV/Excel接入向导：预览、列对应、默认值确认、只写新目录。"""
from pathlib import Path
from datetime import datetime
import json
import tkinter as tk
from tkinter import ttk,filedialog,messagebox,scrolledtext
from .core import SCHEMA,IDS,TZ,load_dataset
from .intake import TABLE_LABELS,read_table,suggest_columns,import_tables


class IntakeWindow(tk.Toplevel):
    def __init__(self,app):
        super().__init__(app);self.app=app;self.snapshot=None;self.entries=[];self.mapping_vars={};self.default_vars={};self.title('本地表格接入向导');width=min(1160,self.winfo_screenwidth()-80);height=min(850,max(640,self.winfo_screenheight()-110));self.geometry(f'{width}x{height}');self.minsize(min(width,960),min(height,680))
        self.columnconfigure(0,weight=1);self.rowconfigure(3,weight=1)
        ttk.Label(self,text='1 选择文件与工作表   →   2 核对列和默认值   →   3 加入接入队列   →   4 生成新数据集',padding=12,font=('Microsoft YaHei UI',12,'bold')).grid(row=0,column=0,sticky='ew')
        meta=ttk.LabelFrame(self,text='案件信息（输入多案编号列时，以下标题/地区/案型作为共同接入信息）',padding=10);meta.grid(row=1,column=0,sticky='ew',padx=12)
        self.meta={}
        for column,(key,title,value) in enumerate([('case_id','默认案件编号','CASE001'),('title','案件标题',''),('region','地区',''),('case_type','案型',''),('origin','数据来源','real')]):
            ttk.Label(meta,text=title).grid(row=0,column=column,sticky='w');var=tk.StringVar(value=value);self.meta[key]=var
            if key=='origin':widget=ttk.Combobox(meta,textvariable=var,values=['real','synthetic'],state='readonly',width=12)
            else:widget=ttk.Entry(meta,textvariable=var,width=20)
            widget.grid(row=1,column=column,sticky='ew',padx=(0,7));meta.columnconfigure(column,weight=1)
        bar=ttk.Frame(self,padding=10);bar.grid(row=2,column=0,sticky='ew');self.path=tk.StringVar();self.sheet=tk.StringVar();self.table=tk.StringVar(value='funds');self.demo_path=None;self.path.trace_add('write',self.path_changed)
        files=ttk.Frame(bar);files.pack(fill='x');options=ttk.Frame(bar);options.pack(fill='x',pady=(7,0))
        ttk.Entry(files,textvariable=self.path,width=30).pack(side='left',fill='x',expand=True);ttk.Button(files,text='选择CSV/Excel',command=self.choose).pack(side='left',padx=4)
        ttk.Button(files,text='合成演示',command=self.demo).pack(side='left',padx=4)
        ttk.Label(options,text='工作表').pack(side='left');self.sheet_combo=ttk.Combobox(options,textvariable=self.sheet,width=18,state='readonly');self.sheet_combo.pack(side='left',padx=5);self.sheet_combo.bind('<<ComboboxSelected>>',lambda e:self.preview())
        ttk.Label(options,text='接入类型').pack(side='left',padx=(10,0));self.table_label=tk.StringVar(value=TABLE_LABELS[self.table.get()])
        types=ttk.Combobox(options,textvariable=self.table_label,values=list(TABLE_LABELS.values()),width=16,state='readonly');types.pack(side='left',padx=5)
        types.bind('<<ComboboxSelected>>',lambda e:self.table.set(next(k for k,v in TABLE_LABELS.items() if v==self.table_label.get())))
        ttk.Button(options,text='预览/更新列对应',command=self.preview).pack(side='left',padx=5)
        panes=ttk.Panedwindow(self,orient='horizontal');panes.grid(row=3,column=0,sticky='nsew',padx=12,pady=8);left=ttk.Frame(panes);right=ttk.Frame(panes);panes.add(left,weight=2);panes.add(right,weight=3)
        ttk.Label(left,text='列对应：未映射字段才使用右侧默认值；金额/时间不得凭空补造。',wraplength=390,padding=6).pack(fill='x')
        canvas=tk.Canvas(left,highlightthickness=0,bg='#f4f6fa');scroll=ttk.Scrollbar(left,orient='vertical',command=canvas.yview);scroll.pack(side='right',fill='y');canvas.pack(fill='both',expand=True);canvas.configure(yscrollcommand=scroll.set);self.form=ttk.Frame(canvas);window=canvas.create_window((0,0),window=self.form,anchor='nw')
        self.form.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')));canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
        ttk.Label(right,text='原表前12行预览；编号应使用文本列，或Excel明确的零填充格式。',wraplength=550,padding=6).pack(fill='x')
        self.preview_text=scrolledtext.ScrolledText(right,height=12,wrap='none',font=('Consolas',10));self.preview_text.pack(fill='both',expand=True)
        self.info=tk.StringVar(value='支持UTF-8/GB18030 CSV、TSV及XLSX；公式不会被执行，映射到公式单元格时会要求核对。')
        ttk.Label(self,textvariable=self.info,wraplength=1080,padding=8).grid(row=4,column=0,sticky='ew')
        queue=ttk.Frame(self,padding=(12,0));queue.grid(row=5,column=0,sticky='ew');ttk.Button(queue,text='将当前表加入队列',command=self.add).pack(side='left');self.queue_label=tk.StringVar(value='队列为空');ttk.Label(queue,textvariable=self.queue_label,padding=8).pack(side='left');ttk.Button(queue,text='清空接入队列',command=self.clear).pack(side='right')
        bottom=ttk.Frame(self,padding=12);bottom.grid(row=6,column=0,sticky='ew');self.parent_path=tk.StringVar(value=str(app.root_dir/'data/imported'))
        ttk.Label(bottom,text='保存父目录').pack(side='left');ttk.Entry(bottom,textvariable=self.parent_path).pack(side='left',fill='x',expand=True,padx=7);ttk.Button(bottom,text='选择',command=self.choose_parent).pack(side='left');ttk.Button(bottom,text='生成并载入新数据集',command=self.commit,style='Accent.TButton').pack(side='right',padx=7)
    def choose(self):
        if self.app.busy:return
        value=filedialog.askopenfilename(parent=self,title='选择本地表格',filetypes=[('CSV/TSV/Excel','*.csv *.tsv *.xlsx')])
        if value:self.path.set(value);self.sheet.set('');self.meta['origin'].set('real');self.preview()
    def demo(self):
        if self.app.busy:return
        path=self.app.root_dir/'data/intake_demo/synthetic_transactions.xlsx'
        if not path.exists():return messagebox.showinfo('演示文件缺失','请检查data/intake_demo目录。',parent=self)
        self.demo_path=str(path.resolve());self.path.set(str(path));self.sheet.set('')
        for key,value in {'case_id':'SIM_IMPORT','title':'合成表格接入演示','region':'合成区域','case_type':'合成导入','origin':'synthetic'}.items():self.meta[key].set(value)
        self.preview()
    def path_changed(self,*args):
        self.snapshot=None
        if self.demo_path and str(Path(self.path.get()).resolve())!=self.demo_path:self.meta['origin'].set('real');self.demo_path=None
    def choose_parent(self):
        value=filedialog.askdirectory(parent=self,title='选择D盘保存父目录')
        if value:self.parent_path.set(value)
    def preview(self):
        if self.app.busy:return
        path=self.path.get();sheet=self.sheet.get() or None;table=self.table.get()
        if not path:return
        def done(snapshot):
            if not self.winfo_exists():return
            self.snapshot=snapshot;self.snapshot_table=table;self.sheet_combo.configure(values=snapshot['sheets']);self.sheet.set(snapshot['sheet']);self.mapping_vars.clear();self.default_vars.clear()
            for child in self.form.winfo_children():child.destroy()
            guessed=suggest_columns(table,snapshot['headers'])
            for row,field in enumerate(k for k in SCHEMA[table] if k not in {'source_id','label_source'}):
                ttk.Label(self.form,text=field,width=18).grid(row=row,column=0,sticky='w',pady=3);var=tk.StringVar(value=guessed.get(field,''));default=tk.StringVar()
                if field in {'verified','confirmed','role_verified'}:default.set('false')
                self.mapping_vars[field]=var;self.default_vars[field]=default
                ttk.Combobox(self.form,textvariable=var,values=['']+snapshot['headers'],state='readonly',width=18).grid(row=row,column=1,sticky='ew',padx=3);ttk.Entry(self.form,textvariable=default,width=14).grid(row=row,column=2,sticky='ew')
            self.form.columnconfigure(1,weight=1);self.form.columnconfigure(2,weight=1)
            lines=[' | '.join(snapshot['headers'])]+[' | '.join(c['value'][:100] for c in r['cells']) for r in snapshot['rows'][:12]];self.preview_text.delete('1.0','end');self.preview_text.insert('1.0','\n\n'.join(lines));self.info.set(f"{snapshot['row_count']} 行 · {TABLE_LABELS[table]} · 原件SHA256 {snapshot['sha256'][:20]}…；请核对列和默认值后加入队列。")
        self.app.run_job(lambda:read_table(path,sheet),done,'正在本地读取表格预览…')
    def add(self):
        if self.app.busy or self.snapshot is None:return
        if any(not value.get().strip() for value in self.meta.values()):return messagebox.showinfo('案件信息未完整','先填写案件标题、地区和案型，再加入队列。',parent=self)
        if self.table.get()!=self.snapshot_table:return messagebox.showinfo('需要重新预览','接入表类型变化，请先重新预览列对应。',parent=self)
        if str(Path(self.path.get()).resolve())!=self.snapshot['path'] or self.sheet.get()!=self.snapshot['sheet']:return messagebox.showinfo('需要重新预览','文件或工作表变化，请先重新预览。',parent=self)
        entry={'path':self.snapshot['path'],'sheet':self.snapshot['sheet'],'sha256':self.snapshot['sha256'],'table':self.table.get(),'columns':{k:v.get() for k,v in self.mapping_vars.items() if v.get()},'defaults':{k:v.get() for k,v in self.default_vars.items() if v.get()},'origin':self.meta['origin'].get(),'case_metadata':{k:v.get().strip() for k,v in self.meta.items()}}
        if self.table.get() not in TABLE_LABELS:return
        self.entries.append(entry);self.queue_label.set(f"已添加 {len(self.entries)} 张表："+'、'.join(TABLE_LABELS[r['table']] for r in self.entries));self.info.set('表已加入队列。可继续选择其他文件/工作表，最后统一生成新数据集。')
    def clear(self):self.entries.clear();self.queue_label.set('队列为空')
    def commit(self):
        if self.app.busy:return
        entries=list(self.entries);metadata={k:v.get().strip() for k,v in self.meta.items()};destination=Path(self.parent_path.get())/datetime.now(TZ).strftime('intake_%Y%m%d_%H%M%S_%f')
        def work():
            result=import_tables(entries,destination,metadata);return result,load_dataset(destination) if result['valid'] else None
        def done(value):
            result,dataset=value
            if dataset is not None:
                self.app.apply_dataset(dataset);self.app.tabs.select(self.app.pages['工作台']);self.app.status.set('表格接入完成，原件已保留：'+str(destination));self.destroy()
            elif self.winfo_exists():
                self.info.set(f"未载入：{len(result['errors'])}项需要核对；草稿和原因保留在 {result['report']}");self.preview_text.delete('1.0','end');self.preview_text.insert('1.0',json.dumps(result['errors'][:30],ensure_ascii=False,indent=2))
        self.app.run_job(work,done,'正在映射表格、保留原件并检查引用…')
