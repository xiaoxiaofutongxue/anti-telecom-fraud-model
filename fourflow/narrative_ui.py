"""案件文本资料桌面工作台：批量导入、四流研判、检索、来源及人工核验。"""
from pathlib import Path
from datetime import datetime
import json,os,math
import tkinter as tk
from tkinter import ttk,filedialog,messagebox,scrolledtext
from .core import TZ
from .io_utils import atomic_json
from .narrative_extract import FLOW_NAMES
from .narrative_reconstruction_view import source_name
from .narrative_session import remember,restore_report,validate_report
from . import __version__

KIND_LABELS={'person_mention':'主体称谓','industry_mention':'产业/机构称谓','wallet':'完整钱包文本','wallet_prefix':'钱包前缀','wallet_fragment':'掩码钱包片段','domain':'域名','ip':'IP提及','coordinate':'原文坐标'}


class NarrativePanel:
    def __init__(self,app,parent):
        self.app=app;self.root=app.root_dir;self.corpus=None;self.result=None;self.output=None;self.displayed=[];self.case=tk.StringVar();self.flow=tk.StringVar(value='全部资料');self.query=tk.StringVar();self.state=tk.StringVar(value='支持整本XLSX、CSV、TSV、DOCX、TXT、MD；自动识别分析表、合并标题和多段表头。')
        bar=ttk.Frame(parent);bar.pack(fill='x')
        for text,command in [('导入案件文本并研判',self.import_files),('合成四流演示',self.demo),('粘贴案件文本',self.paste),('打开已接入资料',self.choose_corpus),('更新并导出报告',self.export),('打开研判报告',self.open_report),('打开报告目录',self.open_output)]:ttk.Button(bar,text=text,command=command,style='Accent.TButton' if text=='导入案件文本并研判' else 'TButton').pack(side='left',padx=3,pady=3)
        row=ttk.Frame(parent);row.pack(fill='x',pady=5);ttk.Label(row,text='当前资料').pack(side='left');self.case_combo=ttk.Combobox(row,textvariable=self.case,state='readonly',width=40);self.case_combo.pack(side='left',fill='x',expand=True,padx=5);self.case_combo.bind('<<ComboboxSelected>>',lambda e:self.show_case())
        ttk.Combobox(row,textvariable=self.flow,values=['全部资料',*FLOW_NAMES.values()],state='readonly',width=12).pack(side='left',padx=5);ttk.Button(row,text='按流筛选',command=self.apply_filter).pack(side='left')
        ttk.Label(parent,textvariable=self.state,wraplength=1050,padding=(5,5)).pack(fill='x')
        ttk.Style(parent).configure('Narrative.TNotebook.Tab',padding=(9,8),font=('Microsoft YaHei UI',9))
        tabs=ttk.Notebook(parent,style='Narrative.TNotebook');tabs.pack(fill='both',expand=True);summary=ttk.Frame(tabs,padding=8);events=ttk.Frame(tabs,padding=8);assets=ttk.Frame(tabs,padding=8);money=ttk.Frame(tabs,padding=8)
        requirements=ttk.Frame(tabs,padding=8);reconstruction=ttk.Frame(tabs,padding=8);fusion=ttk.Frame(tabs,padding=8)
        for frame,title in [(summary,'四流研判摘要'),(requirements,'需求输出与核验'),(reconstruction,'结构还原与核算'),(fusion,'四流关联与产业溯源'),(events,'资料检索与原行'),(assets,'主体与资产提及'),(money,'金额与时间口径')]:tabs.add(frame,text=title)
        self.fusion=scrolledtext.ScrolledText(fusion,wrap='word',padx=14,pady=12,relief='flat');self.fusion.pack(fill='both',expand=True)
        self.reconstruction=scrolledtext.ScrolledText(reconstruction,wrap='word',padx=14,pady=12,relief='flat');self.reconstruction.pack(fill='both',expand=True)
        self.requirements=scrolledtext.ScrolledText(requirements,wrap='word',padx=12,pady=10);self.requirements.pack(fill='both',expand=True)
        self.tabs=tabs;self.events_page=events;self.summary=scrolledtext.ScrolledText(summary,wrap='word',padx=12,pady=10);self.summary.pack(fill='both',expand=True);app.set_text(self.summary,'选择“导入案件文本并研判”，可一次选中多个案件工作簿。\n系统读取原表文字、提取事件/主体/资产线索，生成可复核报告；不会要求你补造逐笔流水或训练标签。\n\n这里使用文书解析、提及图和资料检索，输出为辅助核验线索。已有逐笔流水模型仍在其他板块使用。')
        search=ttk.Frame(events);search.pack(fill='x');query_entry=ttk.Entry(search,textvariable=self.query);query_entry.pack(side='left',fill='x',expand=True,padx=3);query_entry.bind('<Return>',lambda e:self.search());ttk.Button(search,text='检索本批资料',command=self.search).pack(side='left');ttk.Button(search,text='恢复当前资料列表',command=self.apply_filter).pack(side='left',padx=4)
        panes=ttk.Panedwindow(events,orient='vertical');panes.pack(fill='both',expand=True);upper=ttk.Frame(panes);lower=ttk.Frame(panes);panes.add(upper,weight=1);panes.add(lower,weight=1)
        self.tree=app.make_tree(upper,[('flow_text','四流标签',110),('review_text','核验状态',110),('time_text','原文时间',150),('location','资料/原行',220),('title','原文内容',390)],height=4);self.tree.bind('<<TreeviewSelect>>',self.select_record)
        self.detail=scrolledtext.ScrolledText(lower,height=6,wrap='word',padx=10,pady=8);self.detail.pack(fill='both',expand=True)
        review=ttk.Frame(events);review.pack(fill='x',pady=4);self.review_status=tk.StringVar(value='待核验');self.reviewer=tk.StringVar();self.note=tk.StringVar()
        ttk.Combobox(review,textvariable=self.review_status,values=['待核验','已核实线索','已排除','待补充材料'],state='readonly',width=12).grid(row=0,column=0,padx=3);ttk.Label(review,text='核验人').grid(row=0,column=1);ttk.Entry(review,textvariable=self.reviewer,width=13).grid(row=0,column=2,padx=3);ttk.Button(review,text='保存本条核验',command=self.review).grid(row=0,column=3,padx=3);ttk.Button(review,text='打开原件',command=self.open_source).grid(row=0,column=4)
        ttk.Label(review,text='依据/备注').grid(row=1,column=0,sticky='w',pady=4);ttk.Entry(review,textvariable=self.note).grid(row=1,column=1,columnspan=4,sticky='ew',pady=4);review.columnconfigure(3,weight=1)
        review.pack_forget();review.pack(side='bottom',fill='x',pady=4,before=search)
        self.asset_tree=app.make_tree(assets,[('kind_text','类型',125),('label','原文提及',300),('namespace_text','文书范围',155),('count','关联内容行',95),('checksum_text','编码校验范围',220)],height=9)
        ttk.Label(assets,text='匿名编号与掩码片段按文书隔离；同源材料不算独立印证。同一文本记录中的提及仍需核对身份和资金同一性。完整关系见报告。',wraplength=1000,padding=8).pack(fill='x')
        self.amount_tree=app.make_tree(money,[('raw','金额/数量原文',230),('value','提取数值',120),('currency','币种',100),('category','口径',150),('qualifier','原文限定',140),('unit_source','单位依据',190),('locator','来源行',200)],height=9)
        self.time_text=scrolledtext.ScrolledText(money,height=7,wrap='word',padx=10,pady=8);self.time_text.pack(fill='both',expand=True)
        app.after(350,self.restore_latest)
    def cid(self):
        if not self.corpus:return None
        index=self.case_combo.current();return self.corpus.cases[index]['case_id'] if 0<=index<len(self.corpus.cases) else None
    def case_result(self):return next((c for c in (self.result or {}).get('cases',[]) if c['case_id']==self.cid()),None)
    def restore_latest(self):
        if self.app.busy:self.app.after(400,self.restore_latest);return
        pointer=self.root/'runtime/narrative_latest.json'
        if not pointer.is_file():return
        try:
            path=Path(json.loads(pointer.read_text(encoding='utf-8'))['path']);path=path if path.is_absolute() else self.root/path
            self.load(path,select_page=False)
        except (ValueError,OSError) as exc:self.state.set('上次案件文本未载入：'+str(exc))
    def _remember(self):
        remember(self.root,self.corpus,self.result,self.output)
    def import_files(self):
        if self.app.busy:return
        paths=filedialog.askopenfilenames(title='选择案件工作簿、数据表或文字资料，可单选或多选',filetypes=[('案件资料','*.xlsx *.csv *.tsv *.docx *.txt *.md')])
        if paths:self.import_paths(paths)
    def demo(self):
        path=self.root/'data/narrative_demo_synthetic/四流关联演示.csv'
        if not path.is_file():return messagebox.showinfo('演示文件未找到','请使用完整项目包中的data/narrative_demo_synthetic目录。')
        self.import_paths([path],origin='synthetic')
    def import_paths(self,paths,origin='real'):
        if self.app.busy:return
        destination=self.root/'data/narrative'/datetime.now(TZ).strftime('corpus_%Y%m%d_%H%M%S_%f')
        def work():
            from .narrative import import_documents
            from .narrative_report import export_narrative
            corpus=import_documents(paths,destination,origin,progress=lambda s:self.app.jobs.put(('progress',s,None)));output,result=export_narrative(corpus,self.root/'runtime/narrative_runs',self.app.store);return corpus,output,result
        def done(value):
            self.corpus,self.output,self.result=value;self._remember();self.fill_cases();self.app.tabs.select(self.app.pages['案件文本研判']);self.state.set(f'已完成：{len(self.corpus.cases)}份资料、{len(self.corpus.records)}条内容行。原件和原表定位已保留；金额不作为逐笔流水加总。');self.app.status.set('案件文本研判完成，可查看摘要、检索原行或打开报告。')
        self.app.run_job(work,done,'正在读取整本资料并生成四流文本研判…')
    def load(self,path,select_page=True):
        def work():
            from .narrative import load_corpus
            from .narrative_analysis import analyze
            corpus=load_corpus(path);result=analyze(corpus,self.app.store);output,notice=restore_report(self.root,corpus,result);return corpus,result,output,notice
        def done(value):
            self.corpus,self.result,self.output,notice=value;self._remember();self.fill_cases();self.state.set(f'已载入{len(self.corpus.cases)}份案件文本；'+('已恢复匹配当前核验意见的报告，可直接打开。' if self.output else notice or '点击更新并导出报告可保存当前核验意见。'));self.app.status.set('案件文本与原件校验完成。')
            if select_page:self.app.tabs.select(self.app.pages['案件文本研判'])
        self.app.run_job(work,done,'正在校验案件文本与原件…')
    def choose_corpus(self):
        if self.app.busy:return
        path=filedialog.askdirectory(title='选择包含narrative_manifest.json的案件文本资料目录')
        if path:self.load(path)
    def fill_cases(self):
        self.case_combo.configure(values=[c['title'] for c in self.corpus.cases]);self.case_combo.current(0);self.show_case()
    def show_case(self):
        current=self.case_result()
        if not current:return
        labels='\n'.join(FLOW_NAMES[k]+'：'+str(v)+'条相关内容' for k,v in current['flows'].items())
        lines=[current['title'],'',f"内容行 {current['records']} · 工作表 {current['sheet_count']} · 主体/资产提及 {len(current['entities'])}",labels,'同一行可涉及多流，各流计数不能相加为独立事件数。','','本次发现与缺口：',*['• '+s for s in current['observations']],'','待核验提示 '+str(len(current['issues']))+' 项（同一行可有多项）；人工排除 '+str(len(current['excluded_records']))+' 行。','解析结果保留表内分析和文书陈述，不输出违法概率。原始文书真实性仍需核对。','',self.result['method']]
        lines+=['','表内资金/产业回流路径（陈述摘录，完整路径见报告）：']
        for p in current['path_statements'][:5]:lines.append(p['path'][:200]+'\n  '+p['locator']+' / '+p['source_citation'])
        lines+=['','跨流会合提及（同源资料内联系，需核验）：']
        for p in current['cross_flow_mentions'][:8]:lines.append(p['label']+' / '+source_name(p.get('namespace'))+'：'+'、'.join(FLOW_NAMES[f] for f in p['flows'])+'；'+str(len(p['record_ids']))+'条相关原行')
        lines.insert(1,'资料来源：'+('合成验证资料' if self.corpus.manifest['origin']=='synthetic' else '用户提供资料（文书内容未外部核验）'))
        version=self.corpus.manifest.get('version','旧版本')
        lines.insert(2,'资料解析版本：'+version+('；如需使用新提取规则，请重新导入原文件。历史核验仍保留在原批次。' if version!=__version__ else '；当前版本。'))
        checksums={'valid':'编码通过，归属待核验','invalid':'编码未通过，核对原件','not_checked':'未校验此格式编码'}
        self.app.set_text(self.summary,'\n'.join(lines));self.app.fill_tree(self.asset_tree,[{**a,'kind_text':KIND_LABELS.get(a['kind'],a['kind']),'namespace_text':source_name(a.get('namespace')),'count':len(a['record_ids']),'checksum_text':checksums.get(a.get('checksum_status'),'仅原文提及')} for a in current['entities']],['kind_text','label','namespace_text','count','checksum_text']);self.app.fill_tree(self.amount_tree,[{**a,'value':a['value'] if a['value'] is not None else str(a['lower'])+'～'+str(a['upper']) if 'lower' in a else '未明确','currency':a['currency'] or '未明确','unit_source':a.get('unit_source','原文')} for a in current['amount_mentions']],['raw','value','currency','category','qualifier','unit_source','locator'])
        task_lines=[current['title'],'大纲八项输出与当前证据条件','']
        for i,t in enumerate(current['requirement_outputs'],1):task_lines += [str(i)+'. '+t['name']+' — '+t['status'],t['detail'],'关联原行 '+str(len(t['record_ids']))+' 条；完整来源在报告中查看。','']
        task_lines+=['优先核验与补充材料：']
        for q in current['investigation_checklist']:task_lines += [q['priority']+' / '+q['category']+' / '+q['locator'],q['action'],'']
        self.app.set_text(self.requirements,'\n'.join(task_lines))
        from .narrative_reconstruction_view import text_view
        self.app.set_text(self.reconstruction,text_view(current))
        from .narrative_fusion_view import text_view as fusion_text
        self.app.set_text(self.fusion,fusion_text(current))
        time_lines=['时间线使用原文日期和区间，未知时间不补成具体日期；边界仅用于资料排序。','']
        for row in current['timeline']:time_lines.append(row['time']['raw']+'  /  '+row['locator']+'  /  '+row['source_citation'])
        time_lines+=['',f"未明确时间：{current['unknown_time_records']}条；完整原文及所有核验项见报告。"]
        self.app.set_text(self.time_text,'\n'.join(time_lines));self.filter()
    def display(self,records):
        self.displayed=records;titles={c['case_id']:c['title'] for c in self.corpus.cases};statuses={(c['case_id'],v['finding_id']):v['status'] for c in (self.result or {}).get('cases',[]) for v in c.get('reviews',[])}
        self.app.fill_tree(self.tree,[{**r,'flow_text':'/'.join(FLOW_NAMES.get(k,'未分类') for k in r['flows']),'review_text':statuses.get((r['case_id'],'narrative:'+r['id']),'待核验'),'time_text':r['time']['raw'][:60],'location':titles[r['case_id']][:12]+' / '+r['locator'],'title':r['text'].replace('\n','；')[:220]} for r in records],['flow_text','review_text','time_text','location','title']);self.app.set_text(self.detail,f'显示{len(records)}条。点击一行查看原文、提取口径与来源；检索结果可能来自本批其他资料。已排除原行保留供复核，不参与当前分析。')
        self.review_status.set('待核验');self.reviewer.set('');self.note.set('')
    def refresh_analysis_view(self):
        rows=list(self.displayed);selected=self.selected();self.show_case();self.display(rows)
        if selected:
            index=next((i for i,r in enumerate(rows) if r['id']==selected['id']),None)
            if index is not None:self.tree.selection_set(str(index));self.tree.see(str(index));self.select_record()
    def filter(self):
        if not self.corpus:return
        key=next((k for k,v in FLOW_NAMES.items() if v==self.flow.get()),None);rows=[r for r in self.corpus.rows(self.cid()) if key is None or key in r['flows']];rows.sort(key=lambda r:(r['kind'] in {'note','overview'},r['kind']=='evidence_index'));self.display(rows)
    def apply_filter(self):
        if not self.corpus:return
        self.filter();self.tabs.select(self.events_page);self.state.set(f'当前资料 / {self.flow.get()}：显示{len(self.displayed)}条原行。筛选只作用于记录列表，摘要与导出仍覆盖当前整批资料。')
    def search(self):
        if self.app.busy or not self.corpus:return
        query=self.query.get().strip()
        if not query:return self.apply_filter()
        corpus=self.corpus
        def work():
            from .narrative import search
            return search(corpus,query,limit=50)
        def done(rows):
            self.display([r['record'] for r in rows]);mode=rows[0].get('match_mode') if rows else None;method='完整数值/编号匹配' if mode else '文字相似度检索';self.state.set(f'本批资料检索返回{len(rows)}条，范围为整批文件；{method}不表示事实正确率或违法概率。');self.app.status.set('资料检索完成，可选择原行核验。')
        self.app.run_job(work,done,'正在本地检索原文与来源…')
    def selected(self):
        selection=self.tree.selection();return self.displayed[int(selection[0])] if selection else None
    def select_record(self,event=None):
        r=self.selected()
        if not r:return
        case=next(c for c in self.corpus.cases if c['case_id']==r['case_id']);reviews=self.app.store.reviews(self.corpus.fingerprint,r['case_id']);review=next((v for v in reviews if v['finding_id']=='narrative:'+r['id']),{})
        lines=[case['title'],r['locator']+' · 原件 '+case['original_name'],'文书出处：'+(r['source_citation'] or '原表未注明'),'','原文：',r['text'],'','解析提示：','；'.join(r['issues']) or '仍需按原件核验','',r['statement_status']+'；尚未外部核对。']
        if r.get('source_citation_scope')=='sheet_context':lines+=['引注位置：'+r.get('source_context',{}).get('locator','')+'（工作表来源说明）']
        if r['amounts']:lines+=['','本行金额提取及单位依据：',*[(a.get('source_column') or a['field'])+'：'+a['raw']+' → '+str(a['value'] if a['value'] is not None else '区间/未量化')+' '+str(a['currency'] or '未知')+'；'+a['category']+'；'+a.get('unit_source','原文') for a in r['amounts']]]
        self.app.set_text(self.detail,'\n'.join(lines));self.review_status.set(review.get('status','待核验'));self.reviewer.set(review.get('reviewer',''));self.note.set(review.get('note',''));self.app.status.set('当前选择：'+case['title']+' / '+r['locator'])
    def review(self):
        if self.app.busy or not self.corpus:return
        r=self.selected()
        if not r:return messagebox.showinfo('先选择内容','在列表中选择需要核验的一行。')
        try:
            self.corpus.validate();self.app.store.review(self.corpus.fingerprint,r['case_id'],'narrative:'+r['id'],self.review_status.get(),self.note.get(),self.reviewer.get())
            from .narrative_analysis import analyze
            self.result=analyze(self.corpus,self.app.store);self.output=None;self._remember();self.state.set('本条核验已保存。更新并导出报告可查看新结果；历史报告保留原版本。');self.refresh_analysis_view()
        except (ValueError,OSError) as exc:messagebox.showerror('未保存核验',str(exc))
    def export(self):
        if self.app.busy or not self.corpus:return
        corpus=self.corpus
        def work():
            from .narrative_report import export_narrative
            return export_narrative(corpus,self.root/'runtime/narrative_runs',self.app.store)
        def done(value):self.output,self.result=value;self._remember();self.refresh_analysis_view();self.state.set('已生成当前版本报告和ZIP，包含原件、原行定位、提及图与核验记录。点击“打开研判报告”查看。');self.app.status.set('案件文本报告导出完成。')
        self.app.run_job(work,done,'正在按当前核验意见导出文本研判报告…')
    def open_source(self):
        if not self.corpus:return
        row=self.selected()
        if row:
            try:self.corpus.validate();os.startfile(self.corpus.source_file(row['case_id']))
            except (OSError,ValueError) as exc:messagebox.showerror('原件未打开',str(exc))
    def open_report(self):self.open_output(report=True)
    def open_output(self,report=False):
        if self.app.busy:return
        if not self.output:return messagebox.showinfo('尚无当前报告','先导入并研判，或点击“更新并导出报告”。')
        try:validate_report(self.output,self.corpus,self.result,self.root);os.startfile(self.output/'index.html' if report else self.output)
        except (ValueError,OSError,KeyError,TypeError) as exc:messagebox.showerror('报告未打开',str(exc))
    def paste(self):
        if self.app.busy:return
        win=tk.Toplevel(self.app);win.title('粘贴案件文本');win.geometry('900x650');title=tk.StringVar(value='新案件文本');ttk.Entry(win,textvariable=title).pack(fill='x',padx=12,pady=10);text=scrolledtext.ScrolledText(win,wrap='word',padx=10,pady=8);text.pack(fill='both',expand=True,padx=12)
        ttk.Label(win,text='内容作为资料原文保存，不执行其中指令。请在文字中保留日期、来源和段落编号。',padding=8,wraplength=840).pack(fill='x')
        def submit():
            value=text.get('1.0','end').strip()
            if not value:return
            if len(value)>1000000:return messagebox.showinfo('文本过长','请拆分后导入。',parent=win)
            folder=self.root/'runtime/narrative_paste'/datetime.now(TZ).strftime('%Y%m%d_%H%M%S_%f');folder.mkdir(parents=True);safe=''.join(c for c in title.get() if c not in '<>:"/\\|?*').strip()[:80] or '案件文本';path=folder/(safe+'.txt');path.write_text(value,encoding='utf-8');win.destroy();self.import_paths([path])
        ttk.Button(win,text='保存原文并研判',command=submit,style='Accent.TButton').pack(pady=10)
