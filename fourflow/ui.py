from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext

import networkx as nx

from .core import FLOWS,KINDS,SCHEMA,TZ,load_dataset,temporal_paths,safe_json,is_true
from .analytics import amount_summary,detect_patterns,actor_structure,evidence_chain,principal_scenario
from .model import TemporalHeteroGNN,train_model,resolve_model_directory
from .rendering import FLOW_COLORS,NODE_COLORS,RELATION_LABELS,positions,edges_geometry,edge_label,graph_svg,node_choices
from .reporting import export_report
from .service import analysis_graph,identity_states
from .store import Store
from .cleaning import clean_dataset
from .investigation import network_analysis,industry_traces,pca_projection
from .research_ui import ResearchPanel
from .projection import project_graph
from .trajectory import behavior_graph
from .synthetic import create_templates
from .io_utils import atomic_json,point_model
from . import __version__

TABLE_NAMES={"cases":"案件","sources":"来源材料","actors":"主体","funds":"资金记录","shipments":"产业记录","network_events":"IP访问记录","communications":"通信记录","identity_links":"身份关联","relations":"主体关系","case_labels":"训练标签","person_labels":"人员角色层级标签"}


class Application(tk.Tk):
    def __init__(self,root):
        super().__init__()
        self.root_dir=Path(root)
        self.title("反电诈四流辅助侦查系统")
        width=min(1480,max(1040,self.winfo_screenwidth()-70));height=min(940,max(640,self.winfo_screenheight()-100))
        self.geometry(f'{width}x{height}');self.minsize(min(1180,width),min(720,height))
        self.configure(bg="#eef2f7")
        self.option_add("*Font",("Microsoft YaHei UI",10))
        self.store=Store(self.root_dir/"runtime"/"analysis.sqlite3")
        self.dataset=None; self.graph=nx.MultiDiGraph(); self.view_graph=nx.MultiDiGraph()
        self.paths=[]; self.active_path={"nodes":[],"edges":[]}; self.findings=[]; self.assessment={}; self.model=None
        self.current_source_id=None; self.selected_node=None; self.jobs=queue.Queue(); self.busy=False;self.simulation_output=None
        self.origin_var=tk.StringVar(); self.status=tk.StringVar(value="准备就绪")
        self.case_var=tk.StringVar(); self.cutoff_var=tk.StringVar()
        self.flow_vars={flow:tk.BooleanVar(value=True) for flow in FLOWS}
        self.pending_var=tk.BooleanVar(value=False)
        self._resize_timer=None
        self.setup_styles(); self.setup_layout()
        self.protocol("WM_DELETE_WINDOW",self.close)
        self.after(80,self.poll_jobs)
        self.after(150,self.initial_load)

    def setup_styles(self):
        style=ttk.Style(self); style.theme_use("clam")
        style.configure("TFrame",background="#f4f6fa")
        style.configure("TLabel",background="#f4f6fa",foreground="#243248")
        style.configure("TButton",padding=(12,7),font=("Microsoft YaHei UI",10))
        style.configure("Accent.TButton",background="#2563eb",foreground="white")
        style.map("Accent.TButton",background=[("active","#1d4ed8")])
        style.configure("TNotebook",background="#f4f6fa",borderwidth=0)
        style.configure("TNotebook.Tab",padding=(18,10))
        style.configure("Treeview",rowheight=29,font=("Microsoft YaHei UI",9),background="white",fieldbackground="white")
        style.configure("Treeview.Heading",font=("Microsoft YaHei UI",9,"bold"),padding=7)
        style.map("Treeview",background=[("selected","#dbeafe")],foreground=[("selected","#142439")])
        style.configure("Card.TLabel",background="white",padding=12)
        style.configure("Metric.TLabel",background="white",foreground="#1d4ed8",font=("Microsoft YaHei UI",22,"bold"),padding=(12,0,12,14))
        style.layout('Main.TNotebook.Tab',[])
        style.configure('Nav.TButton',padding=(12,6),anchor='w',font=('Microsoft YaHei UI',10),background='#e7edf5',foreground='#334155')
        style.configure('NavSelected.TButton',padding=(12,6),anchor='w',font=('Microsoft YaHei UI',10,'bold'),background='#dbeafe',foreground='#1d4ed8')
        style.configure('Sidebar.TFrame',background='#e7edf5')

    def setup_layout(self):
        header=tk.Frame(self,bg="#142439",height=70); header.pack(fill="x")
        tk.Label(header,text="反电诈四流辅助研判",bg="#142439",fg="white",font=("Microsoft YaHei UI",18,"bold")).pack(side="left",padx=24,pady=16)
        tk.Label(header,text="资金 · 信息与IP · 人员 → 产业溯源",bg="#142439",fg="#a8bdd8").pack(side="left",padx=16)
        tk.Label(header,text="单机本地  /  "+__version__,bg="#142439",fg="#a8bdd8").pack(side="right",padx=22)
        toolbar=ttk.Frame(self,padding=(16,10)); toolbar.pack(fill="x")
        ttk.Button(toolbar,text="流水数据目录",command=self.choose_dataset,style="Accent.TButton").pack(side="left",padx=4)
        ttk.Button(toolbar,text='CSV/Excel接入',command=self.open_intake).pack(side='left',padx=4)
        ttk.Button(toolbar,text='案件文本导入',command=self.open_narrative,style='Accent.TButton').pack(side='left',padx=4)
        ttk.Button(toolbar,text="载入合成样例",command=lambda:self.load_path(self.root_dir/"data"/"enhanced_sample")).pack(side="left",padx=4)
        ttk.Button(toolbar,text="导出数据模板",command=self.export_templates).pack(side="left",padx=4)
        ttk.Button(toolbar,text="导出分析报告",command=self.export,style="Accent.TButton").pack(side="right",padx=4)
        toolbar=ttk.Frame(self,padding=(16,0,16,8));toolbar.pack(fill='x')
        self.scope_toolbar=toolbar
        ttk.Label(toolbar,text="流水案件").pack(side="left",padx=(4,6))
        self.case_combo=ttk.Combobox(toolbar,textvariable=self.case_var,state="readonly",width=27); self.case_combo.pack(side="left")
        self.case_combo.bind("<<ComboboxSelected>>",lambda e:self.refresh())
        ttk.Label(toolbar,text="截至时间").pack(side="left",padx=(14,5))
        ttk.Entry(toolbar,textvariable=self.cutoff_var,width=20).pack(side="left")
        ttk.Button(toolbar,text="应用",command=self.refresh).pack(side="left",padx=4)
        self.origin_label=ttk.Label(self,textvariable=self.origin_var,background="#fff7ed",foreground="#92400e",padding=(20,9));self.origin_label.pack(fill="x",padx=20,pady=(0,8))
        footer=ttk.Frame(self,padding=(16,5),style='Sidebar.TFrame');footer.pack(side='bottom',fill='x')
        self.busy_progress=ttk.Progressbar(footer,mode='indeterminate',length=120);self.busy_progress.pack(side='right',padx=6)
        ttk.Label(footer,textvariable=self.status,wraplength=max(800,self.winfo_screenwidth()-300),foreground='#52647d').pack(side='left',fill='x',expand=True)
        body=ttk.Frame(self);body.pack(fill='both',expand=True,padx=12,pady=(0,8));navigation=ttk.Frame(body,padding=8,style='Sidebar.TFrame');navigation.pack(side='left',fill='y',padx=(0,8))
        ttk.Label(navigation,text='分析工作区',padding=(8,9),font=('Microsoft YaHei UI',11,'bold')).pack(fill='x')
        nav_canvas=tk.Canvas(navigation,width=176,highlightthickness=0,bg='#e7edf5')
        nav_scroll=ttk.Scrollbar(navigation,orient='vertical',command=nav_canvas.yview)
        nav_scroll.pack(side='right',fill='y');nav_canvas.pack(side='left',fill='both',expand=True)
        nav_canvas.configure(yscrollcommand=nav_scroll.set)
        nav_items=ttk.Frame(nav_canvas,style='Sidebar.TFrame');nav_window=nav_canvas.create_window((0,0),window=nav_items,anchor='nw')
        nav_items.bind('<Configure>',lambda e:nav_canvas.configure(scrollregion=nav_canvas.bbox('all')))
        nav_canvas.bind('<Configure>',lambda e:nav_canvas.itemconfigure(nav_window,width=e.width))
        self.tabs=ttk.Notebook(body,style='Main.TNotebook');self.tabs.pack(side='left',fill='both',expand=True)
        self.pages={name:ttk.Frame(self.tabs,padding=12) for name in ["案件文本研判","工作台","四流图谱","清洗与质量","IP与产业溯源","人员·因果·材料","数据记录","模式核验","主体与身份","金额情景","模型与迭代","实证与运维","审计日志"]}
        self.nav_buttons={name:ttk.Button(nav_items,text=name,width=16,style='Nav.TButton',command=lambda n=name:self.tabs.select(self.pages[n])) for name in self.pages}
        for button in self.nav_buttons.values():button.pack(fill='x',pady=2)
        for widget in (nav_canvas,nav_items,*self.nav_buttons.values()):widget.bind('<MouseWheel>',lambda e:nav_canvas.yview_scroll(int(-e.delta/120),'units'))
        for name,page in self.pages.items():self.tabs.add(page,text=name)
        self.setup_overview(); self.setup_graph(); self.setup_data(); self.setup_findings(); self.setup_identities(); self.setup_money(); self.setup_model(); self.setup_audit()
        self.setup_cleaning();self.setup_investigation()
        self.research=ResearchPanel(self,self.pages['人员·因果·材料'])
        from .practical_ui import PracticalPanel
        self.practical=PracticalPanel(self,self.pages['实证与运维'])
        from .narrative_ui import NarrativePanel
        self.narrative=NarrativePanel(self,self.pages['案件文本研判'])
        self.tabs.bind('<<NotebookTabChanged>>',self.navigation_changed)

    def navigation_changed(self,event=None):
        selected=self.tabs.select()
        for name,button in self.nav_buttons.items():button.configure(style='NavSelected.TButton' if str(self.pages[name])==selected else 'Nav.TButton')
        if selected==str(self.pages['案件文本研判']):
            self.scope_toolbar.pack_forget();self.origin_var.set('案件文本研判：来源陈述、明示支付、局部层次与人工核验分别保留；计算条件在“结构还原与核算”查看。')
        else:
            if not self.scope_toolbar.winfo_manager():self.scope_toolbar.pack(fill='x',before=self.origin_label)
            if self.dataset:self.origin_var.set(('合成验证数据：全部主体、事件及标签为虚构。' if self.dataset.synthetic else '本地结构化数据：来源与核验状态分别保留。')+'  当前案件和截至时间以本页上方选择为准。')
        if selected==str(self.pages['四流图谱']):self.after(80,self.draw_graph)

    def open_intake(self):
        if self.busy:return
        from .intake_ui import IntakeWindow
        IntakeWindow(self)

    def open_narrative(self):
        if self.busy:return
        self.tabs.select(self.pages['案件文本研判']);self.narrative.import_files()

    def make_tree(self,parent,columns,height=12,auto_pack=True):
        frame=ttk.Frame(parent)
        if auto_pack:frame.pack(fill="both",expand=True,pady=5)
        tree=ttk.Treeview(frame,columns=[c[0] for c in columns],show="headings",height=height)
        for key,label,width in columns:
            tree.heading(key,text=label); tree.column(key,width=width,minwidth=65,stretch=True)
        vertical=ttk.Scrollbar(frame,orient="vertical",command=tree.yview); vertical.pack(side="right",fill="y")
        horizontal=ttk.Scrollbar(frame,orient="horizontal",command=tree.xview); horizontal.pack(side="bottom",fill="x")
        tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set); tree.pack(fill="both",expand=True)
        return tree

    def fill_tree(self,tree,rows,fields):
        tree.delete(*tree.get_children())
        for index,row in enumerate(rows):
            values=[]
            for field in fields:
                value=row.get(field,"")
                if field=='node' and isinstance(value,str) and value in self.graph:
                    data=self.graph.nodes[value];value=data['label']+' · '+KINDS[data['kind']]+(' / '+data['chain'] if data.get('chain') else '')
                if isinstance(value,bool):value='是' if value else '否'
                elif isinstance(value,list) and all(isinstance(v,str) for v in value):value='；'.join(value)
                values.append(json.dumps(value,ensure_ascii=False,default=safe_json) if isinstance(value,(dict,list)) else str(value))
            tree.insert("","end",iid=str(index),values=values)

    def set_text(self,widget,text):
        widget.configure(state="normal"); widget.delete("1.0","end"); widget.insert("1.0",text); widget.configure(state="disabled")

    def setup_overview(self):
        page=self.pages["工作台"]
        cards=ttk.Frame(page); cards.pack(fill="x")
        self.metrics={}
        for title in ["图谱节点","关联边","模式提示","已核实身份"]:
            box=ttk.Frame(cards); box.pack(side="left",fill="x",expand=True,padx=6)
            ttk.Label(box,text=title,style="Card.TLabel").pack(fill="x")
            variable=tk.StringVar(value="—"); self.metrics[title]=variable
            ttk.Label(box,textvariable=variable,style="Metric.TLabel",font=("Microsoft YaHei UI",22,"bold")).pack(fill="x")
        ttk.Label(page,text="案件摘要与数据覆盖",font=("Microsoft YaHei UI",13,"bold"),padding=(4,18,4,4)).pack(anchor="w")
        self.summary_text=scrolledtext.ScrolledText(page,height=11,wrap="word",relief="flat",bg="white",padx=15,pady=12)
        self.summary_text.pack(fill="x",pady=5)
        ttk.Label(page,text="逐币种交易额与模型状态",font=("Microsoft YaHei UI",13,"bold"),padding=(4,12,4,4)).pack(anchor="w")
        self.overview_text=scrolledtext.ScrolledText(page,height=10,wrap="word",relief="flat",bg="white",padx=15,pady=12)
        self.overview_text.pack(fill="both",expand=True,pady=5)

    def setup_graph(self):
        page=self.pages["四流图谱"]
        filters=ttk.Frame(page); filters.pack(fill="x")
        for flow,label in FLOWS.items():
            ttk.Checkbutton(filters,text=label,variable=self.flow_vars[flow],command=self.update_view).pack(side="left",padx=7)
        ttk.Checkbutton(filters,text="显示候选身份/关系",variable=self.pending_var,command=self.update_view).pack(side="left",padx=10)
        self.amount_labels_var=tk.BooleanVar(value=False)
        ttk.Checkbutton(filters,text="金额标签",variable=self.amount_labels_var,command=self.draw_graph).pack(side="left",padx=4)
        self.node_labels_var=tk.BooleanVar(value=False)
        ttk.Checkbutton(filters,text='全部节点名称',variable=self.node_labels_var,command=self.draw_graph).pack(side='left',padx=4)
        ttk.Button(filters,text="适应画布",command=self.draw_graph).pack(side="right",padx=4)
        ttk.Button(filters,text="事件轨迹图",command=self.show_trajectory).pack(side="right",padx=4)
        ttk.Button(filters,text="导出当前图 SVG",command=self.export_svg).pack(side="right",padx=4)
        pathbar=ttk.Frame(page); pathbar.pack(fill="x",pady=8)
        self.start_var=tk.StringVar(); self.target_var=tk.StringVar(); self.currency_var=tk.StringVar(value="USDT"); self.hops_var=tk.IntVar(value=6); self.chron_var=tk.BooleanVar(value=True)
        ttk.Label(pathbar,text="起点").pack(side="left")
        self.start_combo=ttk.Combobox(pathbar,textvariable=self.start_var,width=24); self.start_combo.pack(side="left",padx=4)
        ttk.Label(pathbar,text="终点").pack(side="left")
        self.target_combo=ttk.Combobox(pathbar,textvariable=self.target_var,width=24); self.target_combo.pack(side="left",padx=4)
        self.currency_combo=ttk.Combobox(pathbar,textvariable=self.currency_var,width=10,state="readonly"); self.currency_combo.pack(side="left",padx=4)
        ttk.Label(pathbar,text="最多跳数").pack(side="left",padx=(8,2)); ttk.Spinbox(pathbar,from_=1,to=12,textvariable=self.hops_var,width=3).pack(side="left")
        ttk.Checkbutton(pathbar,text="按时间顺序",variable=self.chron_var).pack(side="left",padx=6)
        ttk.Button(pathbar,text="查询路径",command=self.query_path,style="Accent.TButton").pack(side="left",padx=5)
        self.path_choice=tk.StringVar(); self.path_combo=ttk.Combobox(page,textvariable=self.path_choice,state="readonly"); self.path_combo.pack(fill="x",pady=(0,6)); self.path_combo.bind("<<ComboboxSelected>>",lambda e:self.select_path())
        pane=ttk.Panedwindow(page,orient="horizontal"); pane.pack(fill="both",expand=True)
        left=ttk.Frame(pane); right=ttk.Frame(pane); pane.add(left,weight=4); pane.add(right,weight=2)
        self.canvas=tk.Canvas(left,bg="white",highlightthickness=0); self.canvas.pack(fill="both",expand=True)
        self.canvas.bind("<Configure>",self.schedule_draw); self.canvas.bind("<MouseWheel>",self.zoom)
        self.canvas.bind("<ButtonPress-3>",lambda e:self.canvas.scan_mark(e.x,e.y)); self.canvas.bind("<B3-Motion>",lambda e:self.canvas.scan_dragto(e.x,e.y,gain=1))
        ttk.Label(left,text="滚轮缩放 · 右键拖动 · 点击节点或边查看来源。颜色表示数据类型，节点位置不表示地理坐标。",foreground="#64748b",padding=5).pack(fill="x")
        legend=ttk.Frame(right); legend.pack(fill="x",pady=3)
        for flow,label in FLOWS.items():ttk.Label(legend,text="● "+label,foreground=FLOW_COLORS[flow]).pack(side="left",padx=3)
        self.detail=scrolledtext.ScrolledText(right,width=40,wrap="word",relief="flat",padx=12,pady=12); self.detail.pack(fill="both",expand=True,padx=(10,0))
        actions=ttk.Frame(right); actions.pack(fill="x",pady=6)
        ttk.Button(actions,text="设为起点",command=lambda:self.assign_node(True)).pack(side="left",padx=3)
        ttk.Button(actions,text="设为终点",command=lambda:self.assign_node(False)).pack(side="left",padx=3)
        ttk.Button(actions,text="打开来源材料",command=self.open_source).pack(side="left",padx=3)

    def schedule_draw(self,event=None):
        if self._resize_timer:self.after_cancel(self._resize_timer)
        self._resize_timer=self.after(180,self.draw_graph)

    def draw_graph(self):
        self._resize_timer=None
        canvas=self.canvas; canvas.delete("all")
        graph=self.view_graph
        if not graph:
            canvas.create_text(200,100,text="当前筛选下没有可显示的关系",fill="#64748b"); return
        if len(graph)>150:
            selected=set(self.active_path.get("nodes",[])); chosen=list(selected)+[n for n in graph if n not in selected]
            graph=graph.subgraph(chosen[:150]).copy()
        width=max(600,canvas.winfo_width()); height=max(420,canvas.winfo_height())
        pos=positions(graph,width,height)
        selected={tuple(e) for e in self.active_path.get("edges",[])}
        for i,(u,v,key,data,points,label,path) in enumerate(edges_geometry(graph,pos)):
            tag=f"edge_{i}"; highlight=(u,v,key) in selected
            color="#ea580c" if highlight else ("#94a3b8" if data.get("verified")=="pending" else FLOW_COLORS[data["flow"]])
            canvas.create_line(*points,smooth=True,arrow=tk.LAST,arrowshape=(10,12,5),fill=color,width=2.8 if highlight else 1.4,dash=(4,3) if data.get("verified")=="pending" else (),tags=tag)
            if highlight or (self.amount_labels_var.get() and data["relation"]=="transfer" and len(graph)<45):
                text=canvas.create_text(*label,text=edge_label(data),font=("Microsoft YaHei UI",8),fill="#334155",tags=tag)
                box=canvas.bbox(text)
                rect=canvas.create_rectangle(box,fill="white",outline="white",tags=tag); canvas.tag_lower(rect,text)
            canvas.tag_bind(tag,"<Button-1>",lambda e,a=u,b=v,k=key:self.show_edge(a,b,k))
        for i,(node,data) in enumerate(graph.nodes(data=True)):
            x,y=pos[node]; tag=f"node_{i}"
            fill="#fed7aa" if node in self.active_path.get("nodes",[]) else NODE_COLORS[data["kind"]]
            canvas.create_oval(x-18,y-18,x+18,y+18,fill=fill,outline="#94a3b8",tags=tag)
            canvas.create_text(x,y,text=KINDS[data["kind"]][:2],font=("Microsoft YaHei UI",8),fill="#334155",tags=tag)
            label=data["label"] if len(data["label"])<=16 else data["label"][:13]+"…"
            if len(graph)<=35 or self.node_labels_var.get() or data['kind'] in {'actor','order'} or node in self.active_path.get('nodes',[]):
                canvas.create_text(x,y+30,text=label,font=("Microsoft YaHei UI",9),fill="#243248",tags=tag)
            canvas.tag_bind(tag,"<Button-1>",lambda e,n=node:self.show_node(n))
        canvas.configure(scrollregion=canvas.bbox("all"))

    def zoom(self,event):
        factor=1.12 if event.delta>0 else 1/1.12
        x=self.canvas.canvasx(event.x); y=self.canvas.canvasy(event.y)
        self.canvas.scale("all",x,y,factor,factor); self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def show_node(self,node):
        self.selected_node=node; self.current_source_id=None
        data=self.view_graph.nodes[node]
        related=[{"from":u,"to":v,"record":d["record_id"],"source":d["source_id"],"relation":RELATION_LABELS.get(d["relation"]),"time":d["timestamp"]} for u,v,k,d in self.view_graph.edges(keys=True,data=True) if node in (u,v)]
        if related:self.current_source_id=related[0]["source"]
        lines=[f"对象：{data['label']}",f"类型：{KINDS[data['kind']]}",f"标识：{node}"]
        if data.get("role_verified") and data.get("role"):lines.append("来源中已核实角色："+data['role'])
        lines.extend(["",f"相关记录 {len(related)} 条："])
        for r in related:lines.append(f"{r['time']}\n  {r['relation']} · {r['record']} · 来源 {r['source']}")
        self.set_text(self.detail,"\n".join(lines))

    def show_edge(self,u,v,key):
        self.selected_node=None; data=self.view_graph[u][v][key]; self.current_source_id=data["source_id"]
        source=self.dataset.source(self.current_case(),data["source_id"])
        lines=["关系："+RELATION_LABELS.get(data["relation"],data["relation"]),"起点："+self.view_graph.nodes[u]["label"],"终点："+self.view_graph.nodes[v]["label"],"时间："+data["timestamp"]]
        if data.get("amount") is not None:lines.append(f"金额：{data['amount']} {data.get('currency','')}")
        lines.extend(["",f"原始记录：{data['table']}.csv 第 {data['line']} 行",f"记录编号：{data['record_id']}",f"来源编号：{data['source_id']}","来源名称："+(source["title"] if source else "缺失"),"材料引用："+(source["reference"] if source else ""),"核验状态："+{"pending":"候选，未参与推理","verified":"已核实关联","recorded":"来源中记录，仍需业务核验"}.get(data.get("verified"),"未知")])
        if data.get("review_note"):lines.append("人工核验依据："+data['review_note'])
        self.set_text(self.detail,"\n".join(lines))

    def assign_node(self,start):
        if self.selected_node:(self.start_var if start else self.target_var).set(self.node_to_display.get(self.selected_node,self.selected_node))

    def open_source(self):
        if not self.current_source_id:return messagebox.showinfo("来源材料","请先点击节点、交易边或来源记录。")
        try:
            path=self.dataset.evidence_file(self.current_case(),self.current_source_id)
            if not path:raise ValueError("该来源为引用记录，尚未附带可打开的本地材料。")
            os.startfile(path)
        except Exception as exc:messagebox.showerror("来源材料",str(exc))

    def setup_data(self):
        page=self.pages["数据记录"]
        bar=ttk.Frame(page);bar.pack(fill="x")
        self.table_var=tk.StringVar(value="资金记录"); self.search_var=tk.StringVar()
        selector=ttk.Combobox(bar,textvariable=self.table_var,values=list(TABLE_NAMES.values()),state="readonly",width=18); selector.pack(side="left")
        selector.bind("<<ComboboxSelected>>",lambda e:self.update_data())
        ttk.Label(bar,text="检索当前案件记录").pack(side="left",padx=(15,5)); entry=ttk.Entry(bar,textvariable=self.search_var,width=40);entry.pack(side="left");entry.bind("<Return>",lambda e:self.update_data())
        ttk.Button(bar,text="检索",command=self.update_data).pack(side="left",padx=5)
        self.data_tree=self.make_tree(page,[("value","记录",200)])
        self.data_tree.bind("<<TreeviewSelect>>",self.select_record)
        self.record_text=scrolledtext.ScrolledText(page,height=7,wrap="word",relief="flat",padx=12,pady=8);self.record_text.pack(fill="x")

    def update_data(self):
        if not self.dataset:return
        table=next(k for k,v in TABLE_NAMES.items() if v==self.table_var.get())
        fields=SCHEMA[table]
        self.data_tree.configure(columns=fields)
        for key in fields:self.data_tree.heading(key,text=key);self.data_tree.column(key,width=130,minwidth=90)
        query=self.search_var.get().strip().casefold()
        self.visible_records=[r for r in self.dataset.snapshot_rows(table,self.current_case(),self.graph.graph.get("as_of") or None) if not query or query in " ".join(str(v) for v in r.values()).casefold()][:1000]
        self.fill_tree(self.data_tree,self.visible_records,fields)
        self.set_text(self.record_text,f"{TABLE_NAMES[table]}：当前案件及已应用截至时间内显示 {len(self.visible_records)} 条，最多显示 1000 条。点击记录可查看原始字段与来源行号。")

    def select_record(self,event=None):
        selection=self.data_tree.selection()
        if not selection:return
        row=self.visible_records[int(selection[0])]
        self.current_source_id=row.get("source_id")
        self.set_text(self.record_text,json.dumps(row,ensure_ascii=False,indent=2,default=safe_json))

    def setup_findings(self):
        page=self.pages["模式核验"]
        page.columnconfigure(0,weight=1);page.rowconfigure(1,weight=3);page.rowconfigure(2,weight=1)
        ttk.Label(page,text="模式提示由交易和关联记录产生。核验人需填写依据；系统不自动作出违法认定。",padding=5).grid(row=0,column=0,sticky='w')
        self.finding_tree=self.make_tree(page,[("id","编号",130),("title","模式",170),("records","相关记录",350),("status","核验状态",140)],height=8,auto_pack=False)
        self.finding_tree.master.grid(row=1,column=0,sticky='nsew',pady=5)
        self.finding_tree.bind("<<TreeviewSelect>>",self.select_finding)
        self.finding_text=scrolledtext.ScrolledText(page,height=4,wrap="word",relief="flat",padx=12,pady=8);self.finding_text.grid(row=2,column=0,sticky='nsew',pady=4)
        editor=ttk.Frame(page);editor.grid(row=3,column=0,sticky='ew')
        bar=ttk.Frame(editor);bar.pack(fill="x",pady=5)
        self.review_status=tk.StringVar(value="待核验");self.reviewer=tk.StringVar()
        ttk.Label(bar,text="状态").pack(side="left");ttk.Combobox(bar,textvariable=self.review_status,values=["待核验","已核实线索","已排除","待补充材料"],state="readonly",width=15).pack(side="left",padx=5)
        ttk.Label(bar,text="核验人").pack(side="left",padx=5);ttk.Entry(bar,textvariable=self.reviewer,width=20).pack(side="left")
        ttk.Button(bar,text="保存核验记录",command=self.save_review,style="Accent.TButton").pack(side="right")
        ttk.Label(editor,text="依据说明（必填，引用原始材料编号）").pack(anchor="w")
        self.review_note=scrolledtext.ScrolledText(editor,height=3,wrap="word",relief="flat");self.review_note.pack(fill="x",pady=5)

    def select_finding(self,event=None):
        selection=self.finding_tree.selection()
        if not selection:return
        finding=self.findings[int(selection[0])]
        self.set_text(self.finding_text,f"{finding['title']}\n\n{finding['detail']}\n\n相关记录：{', '.join(finding['record_ids'])}\n来源编号：{', '.join(finding['source_ids'])}\n\n该提示尚需人工核验。")
        self.active_path={"nodes":finding["nodes"],"edges":finding["edges"]};self.draw_graph()
        review=next((r for r in self.store.reviews(self.dataset.fingerprint,self.current_case(),self.graph.graph.get("as_of") or None) if r["finding_id"]==finding["id"]),None)
        self.review_status.set(review["status"] if review else "待核验")
        self.review_note.delete("1.0","end");self.review_note.insert("1.0",review["note"] if review else "")

    def save_review(self):
        if self.busy:return
        selection=self.finding_tree.selection()
        if not selection:return messagebox.showinfo("核验记录","请先选择一条模式提示。")
        try:
            self.store.review(self.dataset.fingerprint,self.current_case(),self.findings[int(selection[0])]["id"],self.review_status.get(),self.review_note.get("1.0","end").strip(),self.reviewer.get())
            self.refresh();self.status.set("核验已按当前时间保存；历史视图不回填，所有版本写入审计")
        except Exception as exc:messagebox.showerror("核验记录",str(exc))

    def setup_identities(self):
        page=self.pages["主体与身份"]
        page.columnconfigure(0,weight=1);page.rowconfigure(1,weight=1);page.rowconfigure(3,weight=1)
        ttk.Label(page,text="主体结构：连接度和社区用于理解关系结构，不能自动等同于团伙层级或犯罪角色。",padding=4).grid(row=0,column=0,sticky='w')
        self.actor_tree=self.make_tree(page,[("alias","主体",140),("community","社区",80),("distinct_neighbours","不同邻居",100),("recorded_role","来源已核实角色",260)],height=5,auto_pack=False);self.actor_tree.master.grid(row=1,column=0,sticky='nsew',pady=4)
        ttk.Label(page,text="身份关联：原始标注与人工核验分别保留；候选对应默认不参与推理。",padding=4).grid(row=2,column=0,sticky='w')
        self.identity_tree=self.make_tree(page,[("link_id","编号",90),("actor_id","主体",80),("entity_id","地址/账户",220),("original","来源核实标记",130),("effective","当前有效核验",130),("source_id","来源",100)],height=5,auto_pack=False);self.identity_tree.master.grid(row=3,column=0,sticky='nsew',pady=4)
        self.identity_tree.bind("<<TreeviewSelect>>",self.select_identity)
        editor=ttk.Frame(page);editor.grid(row=4,column=0,sticky='ew')
        bar=ttk.Frame(editor);bar.pack(fill="x",pady=4)
        self.identity_status=tk.StringVar(value="待核验");self.identity_reviewer=tk.StringVar()
        ttk.Combobox(bar,textvariable=self.identity_status,values=["待核验","已核实线索","已排除","待补充材料"],state="readonly",width=15).pack(side="left")
        ttk.Label(bar,text="核验人").pack(side="left",padx=6);ttk.Entry(bar,textvariable=self.identity_reviewer,width=18).pack(side="left")
        ttk.Button(bar,text="保存身份核验",command=self.save_identity,style="Accent.TButton").pack(side="right")
        ttk.Label(editor,text='核验依据（必填，保留原始来源标注）').pack(anchor='w')
        self.identity_note=scrolledtext.ScrolledText(editor,height=3,wrap="word",relief="flat");self.identity_note.pack(fill="x",pady=4)

    def select_identity(self,event=None):
        selection=self.identity_tree.selection()
        if not selection:return
        row=self.identity_rows[int(selection[0])]
        review=next((r for r in self.store.reviews(self.dataset.fingerprint,self.current_case(),self.graph.graph.get("as_of") or None) if r["finding_id"]=="identity:"+row["link_id"]),None)
        self.identity_status.set(review["status"] if review else "待核验")
        self.identity_note.delete("1.0","end");self.identity_note.insert("1.0",review["note"] if review else row["verification_note"])
        self.current_source_id=row["source_id"]

    def save_identity(self):
        if self.busy:return
        selected=self.identity_tree.selection()
        if not selected:return messagebox.showinfo("身份核验","请选择候选身份关联。")
        row=self.identity_rows[int(selected[0])]
        try:
            self.store.review(self.dataset.fingerprint,self.current_case(),"identity:"+row["link_id"],self.identity_status.get(),self.identity_note.get("1.0","end").strip(),self.identity_reviewer.get())
            self.refresh();self.status.set("身份核验已保存；生效时间采用本次核验时间，未回写原始来源文件")
        except Exception as exc:messagebox.showerror("身份核验",str(exc))

    def setup_money(self):
        page=self.pages["金额情景"]
        ttk.Label(page,text="账户/地址统计按币种分别计算，重复流转不会被误称为本金。",padding=5).pack(anchor="w")
        self.amount_tree=self.make_tree(page,[("node","地址/账户",350),("currency","币种",90),("incoming","转入",140),("outgoing","转出",140),("net","净流入",140)],height=10)
        form=ttk.LabelFrame(page,text="已核实本金与覆盖率情景",padding=12);form.pack(fill="x",pady=8)
        self.principal_vars={key:tk.StringVar(value=value) for key,value in [("principal",""),("currency","USDT"),("lo",""),("hi",""),("sources","")]}
        for i,(key,label) in enumerate([("principal","已核实去重本金"),("currency","币种"),("lo","覆盖率下限(0—1)"),("hi","覆盖率上限(0—1)"),("sources","来源编号(逗号分隔)")]):
            ttk.Label(form,text=label).grid(row=0,column=i,padx=5,sticky="w")
            ttk.Entry(form,textvariable=self.principal_vars[key],width=21 if key=="sources" else 17).grid(row=1,column=i,padx=5,pady=5,sticky="ew")
            form.columnconfigure(i,weight=1)
        ttk.Button(form,text="计算并记录假设",command=self.calculate_scenario,style="Accent.TButton").grid(row=1,column=5,padx=6)
        self.scenario_text=scrolledtext.ScrolledText(page,height=6,wrap="word",relief="flat",padx=12,pady=10);self.scenario_text.pack(fill="x",pady=5)

    def calculate_scenario(self):
        try:
            values={k:v.get().strip() for k,v in self.principal_vars.items()}
            refs=[r.strip() for r in values["sources"].replace("，",",").split(",") if r.strip()]
            if any(self.dataset.source(self.current_case(),sid) is None for sid in refs):raise ValueError("来源编号必须存在于当前案件的 sources 表")
            result=principal_scenario(values["principal"],values["lo"],values["hi"],values["currency"],refs)
            result["scope_as_of"]=self.graph.graph.get("as_of","")
            self.store.save_scenario(self.dataset.fingerprint,self.current_case(),result)
            self.set_text(self.scenario_text,self.scenario_description(result));self.update_audit()
        except Exception as exc:messagebox.showerror("金额情景",str(exc))

    def setup_model(self):
        page=self.pages["模型与迭代"]
        bar=ttk.Frame(page);bar.pack(fill="x")
        ttk.Button(bar,text="使用当前数据训练",command=lambda:self.start_training(False),style="Accent.TButton").pack(side="left",padx=4)
        ttk.Button(bar,text="合成数据流程验证",command=lambda:self.start_training(True)).pack(side="left",padx=4)
        ttk.Button(bar,text="载入本地模型",command=self.choose_model).pack(side="left",padx=4)
        ttk.Label(bar,text="训练轮数").pack(side="left",padx=(16,5));self.epochs=tk.IntVar(value=70);ttk.Spinbox(bar,from_=10,to=500,textvariable=self.epochs,width=6).pack(side="left")
        testbar=ttk.Frame(page);testbar.pack(fill='x',pady=6)
        ttk.Button(testbar,text='全流程模拟自测',command=self.start_simulation,style='Accent.TButton').pack(side='left',padx=4)
        ttk.Button(testbar,text='打开自测报告',command=self.open_simulation).pack(side='left',padx=4)
        ttk.Label(testbar,text='独立合成目录：检查清洗、分析、核验、模型、材料和报告，不修改当前案件。').pack(side='left',padx=8)
        ttk.Label(page,text="模型：两层时序异构图卷积，逐关系学习变换矩阵；CPU 本地训练。标签仅用于已核实异常模式，未标注不当作负类。",padding=(4,12)).pack(anchor="w")
        self.model_text=scrolledtext.ScrolledText(page,height=14,wrap="word",relief="flat",padx=12,pady=10);self.model_text.pack(fill="both",expand=True)
        self.training_log=scrolledtext.ScrolledText(page,height=8,wrap="word",relief="flat",bg="#152338",fg="#dbeafe",padx=12,pady=10);self.training_log.pack(fill="x",pady=8)

    def setup_audit(self):
        page=self.pages["审计日志"]
        ttk.Button(page,text="刷新日志",command=self.update_audit).pack(anchor="e")
        self.audit_tree=self.make_tree(page,[("created","时间",230),("action","操作",200),("payload","内容",700)])

    def setup_cleaning(self):
        page=self.pages['清洗与质量']
        ttk.Label(page,text='清洗保留原始文件。规范空白、时间、币种、布尔及IP；完全重复可去重，冲突编号与缺失关键值隔离。',padding=8).pack(anchor='w')
        ttk.Button(page,text='选择原始数据并清洗到新目录',command=self.clean_import,style='Accent.TButton').pack(anchor='w',pady=8)
        self.quality_text=scrolledtext.ScrolledText(page,wrap='word',relief='flat',padx=14,pady=12);self.quality_text.pack(fill='both',expand=True)

    def clean_import(self):
        source=filedialog.askdirectory(title='选择符合模板列名的原始数据目录')
        if not source:return
        destination=filedialog.askdirectory(title='选择新的空目录存放清洗结果（不能包含原始目录）')
        if not destination:return
        def done(result):
            self.store.audit('dataset_cleaned',{'source':source,'output':destination,'quarantined':len(result['quarantined']),'valid':result['valid']})
            if result['valid']:self.apply_dataset(load_dataset(destination))
            text='清洗完成；原始数据未改写。\n'+json.dumps({'valid':result['valid'],'counts':result['counts'],'changes':len(result['changes']),'quarantined':len(result['quarantined']),'validation_error':result.get('validation_error',''),'report':str(Path(destination)/'cleaning_report.json')},ensure_ascii=False,indent=2)
            self.set_text(self.quality_text,text)
            self.tabs.select(self.pages['清洗与质量'])
        def work():
            manifest=json.loads((Path(source)/'manifest.json').read_text(encoding='utf-8-sig'))
            if manifest.get('advanced'):
                from .hd_eia import clean_and_validate
                return clean_and_validate(source,destination)['cleaning']
            return clean_dataset(source,destination)
        self.run_job(work,done,'正在清洗、隔离问题记录并生成质量报告…')

    def setup_investigation(self):
        page=self.pages['IP与产业溯源'];bar=ttk.Frame(page);bar.pack(fill='x')
        self.network_window=tk.IntVar(value=30);self.industry_window=tk.IntVar(value=168)
        ttk.Label(bar,text='IP窗口/分钟').pack(side='left');ttk.Spinbox(bar,from_=1,to=1440,textvariable=self.network_window,width=5).pack(side='left',padx=5)
        ttk.Label(bar,text='产业关联窗口/小时').pack(side='left');ttk.Spinbox(bar,from_=1,to=720,textvariable=self.industry_window,width=5).pack(side='left',padx=5)
        ttk.Button(bar,text='重新分析',command=self.update_investigation,style='Accent.TButton').pack(side='left',padx=8)
        ttk.Button(bar,text='特征与公式核验',command=self.show_feature_audit).pack(side='left',padx=4)
        ttk.Button(bar,text='查看节点投影',command=self.show_pca).pack(side='right')
        self.projection_method=tk.StringVar(value='PCA');ttk.Combobox(bar,textvariable=self.projection_method,values=['PCA','LDA','LLE','t-SNE'],width=8,state='readonly').pack(side='right',padx=5)
        ttk.Label(page,text='共享IP不等于同一人；产业分数表示核验优先级。订单关联来自来源记录，背景通信/IP不证明交易用途。',padding=8).pack(anchor='w')
        self.ip_tree=self.make_tree(page,[('ip','IP',220),('scope','网络域',160),('event_count','事件数',80),('anchor_count','主体/账户数',100),('first_seen','首次时间',200),('last_seen','末次时间',200)],height=5)
        self.trace_tree=self.make_tree(page,[('order','产业订单',140),('service_types','来源产业类别',190),('priority_score','核验优先级/100',130),('pareto_front','帕累托前沿',100),('flows','关联流',240),('missing','待补材料',350)],height=4)
        self.trace_tree.bind('<<TreeviewSelect>>',self.select_trace)
        self.investigation_text=scrolledtext.ScrolledText(page,height=10,wrap='word',relief='flat',padx=12,pady=10);self.investigation_text.pack(fill='both',expand=True)

    def update_investigation(self):
        if not self.dataset:return
        try:
            network=network_analysis(self.graph,self.network_window.get())
            self.findings=[f for f in self.findings if f['kind'] not in {'shared_ip','multi_ip'}]+network['findings']
            self.update_findings();self.metrics['模式提示'].set(str(len(self.findings)))
            self.traces=industry_traces(self.dataset,self.graph,self.industry_window.get())
            self.fill_tree(self.ip_tree,network['ips'],['ip','scope','event_count','anchor_count','first_seen','last_seen'])
            self.fill_tree(self.trace_tree,self.traces,['order','service_types','priority_score','pareto_front','flows','missing'])
            lines=[f"网络观测 {len(network['observations'])} 条，IP {len(network['ips'])} 个，网络模式提示 {len(network['findings'])} 项。",network['interpretation'],'','排序目标：四流覆盖50%、来源多样性25%、付款与发运时间接近度25%。前沿表示没有其他候选在全部目标上更好，并非已核实结论。','点击产业订单查看逐条来源和缺口。']
            lines.extend(r['title']+'：'+r['detail'] for r in network['findings'])
            self.set_text(self.investigation_text,'\n'.join(lines))
        except (ValueError,tk.TclError) as exc:messagebox.showerror('溯源分析未完成',str(exc))

    def select_trace(self,event=None):
        selection=self.trace_tree.selection()
        if not selection:return
        trace=self.traces[int(selection[0])]
        labels={'coverage':'四流覆盖','source_diversity':'来源多样性','temporal':'时间接近度'}
        lines=[f"订单：{trace['order']}；优先级 {trace['priority_score']}/100（非风险概率）",trace['interpretation'],'排序目标：'+'；'.join(f'{labels[k]} {v:.1%}' for k,v in trace['objectives'].items()),'待补材料：'+'；'.join(trace['missing']),'']
        for step in trace['steps']:lines.extend([f"{step['timestamp']}  {step['flow']} / {step['association']}",f"  {step['table']}/{step['record_id']} · 来源 {step['source_id']} · 当前CSV行 {step['csv_line']} / 原始行 {step['original_line']}"])
        self.set_text(self.investigation_text,'\n'.join(lines))

    def show_feature_audit(self):
        if self.busy or not self.dataset:return
        from .feature_audit import audit_features,readable_audit
        dataset=self.dataset;cid=self.current_case();cutoff=self.graph.graph.get('as_of') or None
        def done(result):
            window=tk.Toplevel(self);window.title('观测特征与大纲示例公式核验');window.geometry('1000x700')
            text=scrolledtext.ScrolledText(window,wrap='word',padx=18,pady=15,relief='flat');text.pack(fill='both',expand=True);self.set_text(text,readable_audit(result))
            self.status.set('特征与公式核验完成；缺失的观测量已逐项列出。')
        self.run_job(lambda:audit_features(dataset,cid,cutoff),done,'正在按当前快照核对特征和公式缺口…')

    def show_pca(self):
        graph=self.graph.copy();dataset=self.dataset;method=self.projection_method.get()
        def done(projection):
            window=tk.Toplevel(self);window.title(f"{graph.graph['case_id']} {method} 特征探索");window.geometry('850x650')
            info=projection.get('explained_variance_ratio',projection.get('fit_scope',projection.get('interpretation','')))
            ttk.Label(window,text=f"{method}：{info}。投影接近不表示同一主体或团伙。",padding=12,wraplength=810).pack(fill='x')
            canvas=tk.Canvas(window,bg='white');canvas.pack(fill='both',expand=True)
            points=projection['points'];xs=[p['pc1'] for p in points];ys=[p['pc2'] for p in points]
            for p in points:
                x=70+(p['pc1']-min(xs))/max(1e-9,max(xs)-min(xs))*680;y=70+(p['pc2']-min(ys))/max(1e-9,max(ys)-min(ys))*380
                canvas.create_oval(x-5,y-5,x+5,y+5,fill=NODE_COLORS[graph.nodes[p['node']]['kind']],outline='#334155')
                canvas.create_text(x,y+15,text=graph.nodes[p['node']]['label'][:18],font=('Microsoft YaHei UI',8))
            def save():
                filename=filedialog.asksaveasfilename(title='保存投影坐标',defaultextension='.csv',initialfile=graph.graph['case_id']+'_'+method+'.csv')
                if filename:
                    from .reporting import write_csv
                    write_csv(filename,points,['node','pc1','pc2'])
            ttk.Button(window,text='导出坐标CSV',command=save).pack(pady=8)
        self.run_job(lambda:project_graph(dataset,graph,method),done,'正在计算'+method+'特征投影…')

    def show_trajectory(self):
        result=behavior_graph(self.graph)
        if not result['available']:return messagebox.showinfo('事件轨迹图',result['reason'])
        dataset=self.dataset;case=self.current_case();window=tk.Toplevel(self);window.title('事件轨迹图 '+case);window.geometry('1180x800')
        ttk.Label(window,text=result['interpretation'],padding=10,wraplength=1120).pack(fill='x')
        pane=ttk.Panedwindow(window,orient='horizontal');pane.pack(fill='both',expand=True);left=ttk.Frame(pane);right=ttk.Frame(pane);pane.add(left,weight=3);pane.add(right,weight=2)
        canvas=tk.Canvas(left,bg='white');scroll=ttk.Scrollbar(left,orient='vertical',command=canvas.yview);scroll.pack(side='right',fill='y');canvas.configure(yscrollcommand=scroll.set);canvas.pack(fill='both',expand=True)
        detail=scrolledtext.ScrolledText(right,width=34,wrap='word');detail.pack(fill='both',expand=True);selected={'source':None}
        nodes=sorted(result['nodes'],key=lambda r:(r['timestamp'],r['id']))[:200];locations={n['id']:(80+list(FLOWS).index(n['flow'])*165,65+i*43) for i,n in enumerate(nodes)}
        for index,(flow,label) in enumerate(FLOWS.items()):canvas.create_text(80+index*165,20,text=label,fill=FLOW_COLORS[flow])
        for edge in result['edges']:
            if edge['from'] in locations and edge['to'] in locations:
                x,y=locations[edge['from']];xx,yy=locations[edge['to']];canvas.create_line(x,y+12,xx,yy-12,arrow=tk.LAST,fill='#cbd5e1')
        for index,node in enumerate(nodes):
            x,y=locations[node['id']];tag='event'+str(index)
            canvas.create_rectangle(x-75,y-13,x+75,y+13,fill='#f1f5f9',outline=FLOW_COLORS[node['flow']],tags=tag);canvas.create_text(x,y,text=node['record_id'][:12]+' '+node['timestamp'][11:16],font=('Microsoft YaHei UI',8),tags=tag)
            def pick(event,row=node):selected['source']=row['source_id'];self.set_text(detail,json.dumps(row,ensure_ascii=False,indent=2))
            canvas.tag_bind(tag,'<Button-1>',pick)
        canvas.configure(scrollregion=canvas.bbox('all'))
        def open_material():
            if selected['source']:
                path=dataset.evidence_file(case,selected['source'])
                if path:os.startfile(path)
        ttk.Button(right,text='打开所选事件来源',command=open_material).pack(pady=5)
        ttk.Label(window,text=f"窗口显示前{len(nodes)}/{len(result['nodes'])}条事件；完整事件关系可随分析报告导出。",padding=7).pack(fill='x')

    def current_case(self):
        return self.case_var.get().split("  ",1)[0]

    def initial_load(self):
        try:
            settings_path=self.root_dir/"runtime"/"settings.json"
            settings=json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
            root=Path(settings.get("dataset",str(self.root_dir/"data"/"enhanced_sample")))
            if not root.is_absolute():root=self.root_dir/root
            if root.resolve()==(self.root_dir/'data/sample').resolve():root=self.root_dir/'data/enhanced_sample'
            self.apply_dataset(load_dataset(root if root.exists() else self.root_dir/"data"/"enhanced_sample"))
            model_dir=resolve_model_directory(self.root_dir)
            if (model_dir/"model_card.json").is_file():
                self.model=TemporalHeteroGNN.load(model_dir);self.refresh()
        except Exception as exc:self.status.set("初始化提示："+str(exc))

    def load_path(self,path):
        def done(dataset):self.apply_dataset(dataset);self.tabs.select(self.pages['工作台'])
        self.run_job(lambda:load_dataset(path),done,"正在校验并导入数据…")

    def choose_dataset(self):
        path=filedialog.askdirectory(title="选择包含 manifest.json 和各流 CSV 的数据目录")
        if path:self.load_path(path)

    def apply_dataset(self,dataset):
        self.dataset=dataset
        choices=[f"{r['case_id']}  {r['title']}" for r in dataset.tables["cases"]]
        self.case_combo.configure(values=choices);self.case_var.set(choices[0]);self.cutoff_var.set("")
        self.store.audit("dataset_loaded",{"dataset":dataset.fingerprint,"path":str(dataset.root),"origin":dataset.manifest["origin"],"cases":len(choices)})
        self.refresh()

    def refresh(self):
        if not self.dataset:return
        if getattr(self,'busy',False):
            case=next((r for r in self.dataset.tables['cases'] if r['case_id']==self.graph.graph.get('case_id')),None)
            if case:self.case_var.set(case['case_id']+'  '+case['title']);self.cutoff_var.set(self.graph.graph.get('as_of',''))
            self.status.set('当前任务处理中，完成后再切换分析快照。');return
        try:
            self.graph=analysis_graph(self.dataset,self.current_case(),self.store,self.cutoff_var.get().strip() or None)
            self.selected_node=None;self.current_source_id=None;self.set_text(self.detail,'请在当前案件图谱中点击节点或关系查看来源。')
            self.paths=[];self.active_path={"nodes":[],"edges":[]};self.path_combo.configure(values=[]);self.path_choice.set("")
            self.pattern_diagnostics={};self.findings=detect_patterns(self.graph,diagnostics=self.pattern_diagnostics)
            try:self.assessment=self.model.assess(self.dataset,self.graph) if self.model and 0<len(self.graph)<=256 else {"available":False,"reason":"尚未加载可用模型，或当前图为空/超过本地模型的256节点上限"}
            except (ValueError,FloatingPointError) as exc:self.assessment={'available':False,'reason':'资金模型未完成评分：'+str(exc)}
            self.origin_var.set(("合成验证数据：全部主体、事件及标签为虚构，结果仅验证系统流程。" if self.dataset.synthetic else "已导入本地数据：核验状态与来源分别保留，所有模型输出需人工复核。")+"  截至时间示例：2026-01-01 08:30；留空显示全部。")
            self.metrics["图谱节点"].set(str(len(self.graph)));self.metrics["关联边"].set(str(self.graph.number_of_edges()));self.metrics["模式提示"].set(str(len(self.findings)))
            self.metrics["已核实身份"].set(str(sum(d["relation"]=="identity" for u,v,k,d in self.graph.edges(keys=True,data=True))))
            case=next(r for r in self.dataset.tables["cases"] if r["case_id"]==self.current_case())
            summary={"案件":{k:v for k,v in case.items() if not k.startswith("_")},"数据目录":str(self.dataset.root),"数据指纹":self.dataset.fingerprint,"四流边数":{FLOWS[f]:sum(d["flow"]==f for u,v,k,d in self.graph.edges(keys=True,data=True)) for f in FLOWS},"校验提示":self.dataset.warnings or ["CSV结构、编号、金额、日期和来源引用检查通过"]}
            summary_lines=[f"案件：{case['case_id']}  {case['title']}",f"区域：{case['region']}    类型：{case['case_type']}",f"数据目录：{self.dataset.root}","", "已导入记录："+"  /  ".join(f"{TABLE_NAMES[t]} {len(self.dataset.rows(t,self.current_case()))} 条" for t in ["funds","shipments","communications","network_events","actors"]),"四流关联边："+"  /  ".join(f"{k} {v}" for k,v in summary["四流边数"].items()),"", "来源材料："+str(len(self.dataset.rows("sources",self.current_case())))+" 份；每条图谱关系保留来源编号和原始记录行号。", "校验结果："+"；".join(summary["校验提示"])]
            if not self.pattern_diagnostics['search_complete']:summary_lines.append(self.pattern_diagnostics['interpretation'])
            self.set_text(self.summary_text,"\n".join(summary_lines))
            amounts=amount_summary(self.graph)
            model_lines=[]
            if self.assessment.get("available"):
                model_lines=[f"模型：{self.assessment['model_id']}","来源："+("合成数据验证模型" if self.assessment['origin']=="synthetic" else "导入数据训练模型"),f"目标异常模式分数：{self.assessment['probability']:.4f}（非违法概率）"]
                if self.assessment.get("known_split"):model_lines.append("该样本与模型的 "+self.assessment['known_split']+" 集共享图记录或资产标识，不能作为独立外部评估。")
                if self.assessment.get("shifted_features"):model_lines.append("检测到特征偏离训练参考，建议复核数据分布并补充标注。")
                model_lines.append("独立测试集指标、基准比较及训练记录可在“模型与迭代”查看。")
            else:model_lines=[self.assessment.get("reason","尚未载入模型")]
            self.set_text(self.overview_text,"逐币种交易额合计：\n"+"\n".join(f"{c}  {v}" for c,v in amounts["gross_by_currency"].items())+"\n\n"+amounts["interpretation"]+"\n\n"+"\n".join(model_lines))
            self.fill_tree(self.amount_tree,amounts["addresses"],["node","currency","incoming","outgoing","net"])
            scenario=self.store.scenario(self.dataset.fingerprint,self.current_case())
            if scenario and scenario.get("scope_as_of","")!=self.graph.graph.get("as_of",""):
                self.set_text(self.scenario_text,"此前金额情景对应不同的截至时间，请为当前快照重新填写假设。")
            else:self.set_text(self.scenario_text,self.scenario_description(scenario) if scenario else "尚未输入本金和覆盖率假设，不输出金额区间。交易额统计不可直接替代本金。")
            for variable in self.principal_vars.values():variable.set("")
            self.principal_vars["currency"].set("USDT")
            self.update_view();self.update_data();self.update_findings();self.update_identities();self.update_model_card();self.update_audit();self.update_investigation();self.research.refresh()
            self.navigation_changed()
            quality=self.dataset.root/'cleaning_report.json'
            self.set_text(self.quality_text,('此数据集附带清洗报告：'+str(quality)+'\n原始行号与文件SHA256随来源链保留。') if quality.is_file() else '当前为严格导入数据，未执行自动修复。需要清洗时点击上方按钮；系统不会自动补造缺失金额、主体、IP、时间或标签。')
            self.status.set(f"{self.current_case()} · {len(self.graph)} 个节点 · {self.graph.number_of_edges()} 条关联 · 所有操作在本地执行")
        except Exception as exc:
            if self.graph.graph.get('dataset_fingerprint')==self.dataset.fingerprint:
                active=self.graph.graph.get('case_id');case=next((r for r in self.dataset.tables['cases'] if r['case_id']==active),None)
                if case:self.case_var.set(case['case_id']+'  '+case['title']);self.cutoff_var.set(self.graph.graph.get('as_of',''))
            messagebox.showerror("分析失败",str(exc))

    def update_view(self):
        if not self.dataset:return
        candidate=analysis_graph(self.dataset,self.current_case(),self.store,self.graph.graph.get("as_of") or None,include_pending=self.pending_var.get())
        self.paths=[];self.active_path={"nodes":[],"edges":[]};self.path_combo.configure(values=[]);self.path_choice.set("")
        allowed={f for f,v in self.flow_vars.items() if v.get()}
        self.view_graph=candidate.edge_subgraph([(u,v,k) for u,v,k,d in candidate.edges(keys=True,data=True) if d["flow"] in allowed]).copy()
        nodes=sorted(self.view_graph.nodes())
        previous=getattr(self,'display_to_node',{});start=previous.get(self.start_var.get(),self.start_var.get());target=previous.get(self.target_var.get(),self.target_var.get())
        self.node_to_display,self.display_to_node=node_choices(self.view_graph)
        self.start_combo.configure(values=[self.node_to_display[n] for n in nodes]);self.target_combo.configure(values=[self.node_to_display[n] for n in nodes])
        wallets=[n for n in nodes if self.view_graph.nodes[n]["kind"]=="wallet"]
        first_transfer=next((u for u,v,k,d in self.view_graph.edges(keys=True,data=True) if d['relation']=='transfer' and u!=v),None)
        if start not in nodes:start=first_transfer or (wallets[0] if wallets else (nodes[0] if nodes else ''))
        if target not in nodes:target=wallets[-1] if wallets else (nodes[-1] if nodes else '')
        self.start_var.set(self.node_to_display.get(start,''));self.target_var.set(self.node_to_display.get(target,''))
        currencies=sorted({d.get("currency") for u,v,k,d in self.graph.edges(keys=True,data=True) if d["relation"]=="transfer"})
        self.currency_combo.configure(values=["全部关系",*currencies])
        if self.currency_var.get() not in ["全部关系",*currencies]:self.currency_var.set(currencies[0] if currencies else "全部关系")
        self.draw_graph()

    def query_path(self):
        try:
            start=self.display_to_node.get(self.start_var.get(),self.start_var.get());target=self.display_to_node.get(self.target_var.get(),self.target_var.get())
            result=temporal_paths(self.view_graph,start,target,self.hops_var.get(),3,None if self.currency_var.get()=="全部关系" else self.currency_var.get(),self.chron_var.get())
            self.paths=result["paths"]
            values=[f"路径 {i+1} · {len(p['edges'])} 条关系 · "+" → ".join(self.view_graph.nodes[n]["label"] for n in p["nodes"]) for i,p in enumerate(self.paths)]
            self.path_combo.configure(values=values)
            if values:self.path_choice.set(values[0]);self.select_path()
            else:self.path_choice.set("未找到符合时间、币种和跳数条件的路径");self.active_path={"nodes":[],"edges":[]};self.draw_graph()
            self.store.audit("path_query",{"dataset":self.dataset.fingerprint,"case_id":self.current_case(),"source":start,"target":target,"chronological":self.chron_var.get(),"paths":len(self.paths),"truncated":result["truncated"]})
            self.status.set(f"找到 {len(self.paths)} 条候选路径。"+("已达到返回或搜索上限，不代表穷尽。" if result["truncated"] else "")+"连接路径不证明同一笔资金归属。")
        except Exception as exc:messagebox.showerror("路径查询",str(exc))

    def select_path(self):
        index=self.path_combo.current()
        if not 0<=index<len(self.paths):return
        self.active_path=self.paths[index];self.draw_graph()
        chain=evidence_chain(self.dataset,self.view_graph,self.active_path)
        if chain:self.current_source_id=chain[0]["source_id"]
        lines=["候选时序关联链","连接和时间顺序不等于因果证明或同一笔资金认定。",""]
        for r in chain:
            lines.extend([f"第 {r['step']} 步",self.view_graph.nodes[r['from']]['label']+" → "+self.view_graph.nodes[r['to']]['label'],"时间："+r['timestamp'],"关系："+RELATION_LABELS.get(r['relation'],r['relation'])])
            if r['amount']:lines.append(f"金额：{r['amount']} {r['currency']}")
            lines.extend([f"记录：{r['record_id']} / {r['table']}.csv 第{r['csv_line']}行",f"来源：{r['source_id']} / {r['source_title']}",""])
        self.set_text(self.detail,"\n".join(lines))

    def scenario_description(self,result):
        return f"条件情景区间：{result['lower']} — {result['upper']} {result['currency']}\n已核实去重本金：{result['confirmed_principal']} {result['currency']}\n覆盖率假设：{float(result['coverage_min']):.1%} — {float(result['coverage_max']):.1%}\n来源编号：{', '.join(result['source_ids'])}\n\n{result['interpretation']}"

    def update_findings(self):
        reviews={r["finding_id"]:r for r in self.store.reviews(self.dataset.fingerprint,self.current_case(),self.graph.graph.get("as_of") or None)}
        rows=[{"id":r["id"],"title":r["title"],"records":", ".join(r["record_ids"]),"status":reviews.get(r["id"],{}).get("status","待核验")} for r in self.findings]
        self.fill_tree(self.finding_tree,rows,["id","title","records","status"])

    def update_identities(self):
        self.fill_tree(self.actor_tree,actor_structure(self.graph),["alias","community","distinct_neighbours","recorded_role"])
        self.identity_rows=identity_states(self.dataset,self.graph,self.store)
        reviews={r["finding_id"]:r for r in self.store.reviews(self.dataset.fingerprint,self.current_case(),self.graph.graph.get("as_of") or None)}
        rows=[]
        for r in self.identity_rows:
            effective=any(d["relation"]=="identity" and d["record_id"]==r["link_id"] for u,v,k,d in self.graph.edges(keys=True,data=True))
            rows.append({**r,"original":"已核实" if is_true(r["verified"]) else "候选", "effective":"已核实" if effective else reviews.get("identity:"+r["link_id"],{}).get("status","未生效")})
        self.fill_tree(self.identity_tree,rows,["link_id","actor_id","entity_id","original","effective","source_id"])

    def update_model_card(self):
        if not self.model:return self.set_text(self.model_text,"尚未载入模型。可使用合成数据验证训练流程，或导入至少20个有已核实正负标签的案件训练。")
        m=self.model.metadata
        if not m.get('trained'):return self.set_text(self.model_text,'当前权重没有完整训练标记，不能输出分析分数。请通过训练入口生成正式模型。')
        display={"模型编号":m["model_id"],"数据来源":"合成数据（未证明真实识别能力）" if m["origin"]=="synthetic" else "导入数据，标签由提供者核实", "架构":m["architecture"],"运行方式":m["engine"],"训练轮数":m["epochs"],"选定轮次":m["selected_epoch"],"温度校准":m["temperature"],"评估":m["metrics"],"逻辑回归基准测试":m["baseline_test"],"限制":m["limitations"]}
        lines=[f"模型编号：{m['model_id']}","模型来源："+display['数据来源'],"架构：两层时序异构图卷积 + 全局均值池化 + 二分类输出","运行：本地 CPU；卷积层和输出层均参与反向传播",f"训练轮数：{m['epochs']}    选定轮次：{m['selected_epoch']}    校准温度：{m['temperature']:.2f}","", "评估结果（只针对该数据集和标签）："]
        for name,label in [("train","训练集"),("validation","验证集"),("test","内部时间留出测试")]:
            r=m['metrics'][name];lines.append(f"{label}：{r['n']} 例 | 精确率 {r['precision']:.4f} | 召回率 {r['recall']:.4f} | F1 {r['f1']:.4f} | Brier {r['brier']:.6f}")
        definition=m.get('label_definition',{})
        lines.extend(['','预测任务：'+definition.get('task_id','未声明'),'正标签：'+definition.get('positive','未声明'),'负标签：'+definition.get('negative','未声明')])
        baseline=m['baseline_test'];lines.extend([f"逻辑回归基准（测试）：F1 {baseline['f1']:.4f}，准确率 {baseline['accuracy']:.4f}","",m['limitations'],"","四流删除敏感性和特征偏离用于复核，不等于因果解释或因果偏差修正。"])
        self.set_text(self.model_text,"\n".join(lines))

    def choose_model(self):
        if self.busy:return
        path=filedialog.askdirectory(title="选择包含 weights.npz 和 model_card.json 的模型目录")
        if not path:return
        try:
            self.model=TemporalHeteroGNN.load(path)
            point_model(self.root_dir,"latest.json",path)
            self.store.audit("model_loaded",{"model_id":self.model.metadata["model_id"],"origin":self.model.metadata["origin"]})
            self.refresh()
        except Exception as exc:messagebox.showerror("模型载入",str(exc))

    def start_training(self,synthetic):
        try:
            epochs=self.epochs.get()
            if not 10<=epochs<=500:raise ValueError("训练轮数应在10—500之间")
            training_path=self.root_dir/'data/stress_training_synthetic'
            dataset=load_dataset(training_path if training_path.exists() else self.root_dir/"data"/"training_synthetic") if synthetic else self.dataset
            output=self.root_dir/"runtime"/"models"/datetime.now(TZ).strftime("model_%Y%m%d_%H%M%S_%f")
            def progress(text):self.jobs.put(("progress",text,None))
            def done(model):
                self.model=model
                pointer=self.root_dir/"runtime"/"models"/"latest.json";pointer.parent.mkdir(parents=True,exist_ok=True)
                point_model(self.root_dir,"latest.json",output)
                self.store.audit("model_trained",{"model_id":model.metadata["model_id"],"origin":dataset.manifest["origin"],"dataset":dataset.fingerprint,"test_metrics":model.metadata["metrics"]["test"]})
                self.refresh()
            self.run_job(lambda:train_model(dataset,output,epochs,progress),done,"正在训练，界面可以继续查看数据…")
        except Exception as exc:messagebox.showerror("训练",str(exc))

    def start_simulation(self):
        from .simulation import run_simulation
        def done(value):
            self.simulation_output,result=value
            self.set_text(self.model_text,f"全流程模拟：{result['passed']}项通过，{result['warnings']}项需关注，{result['failed']}项失败。\n\n"+result['scope']+'\n\n'+str(self.simulation_output/'report.html')+'\n\n'+ '\n'.join(r['name']+'：'+r['status'] for r in result['checks']))
            self.status.set('全流程模拟已完成，可点击“打开自测报告”查看实际输出和错例。')
        self.run_job(lambda:run_simulation(self.root_dir,progress=lambda s:self.jobs.put(('progress',s,None))),done,'正在独立目录执行全流程模拟…')

    def open_simulation(self):
        if self.simulation_output:os.startfile(self.simulation_output/'report.html')
        else:messagebox.showinfo('流程模拟','请先点击“全流程模拟自测”。')

    def update_audit(self):
        self.fill_tree(self.audit_tree,self.store.recent_audit(),["created","action","payload"])

    def export(self):
        if self.tabs.select()==str(self.pages['案件文本研判']):return self.narrative.export()
        if not self.dataset:return
        try:
            # 报告使用完整分析图及其时间范围，不把显示筛选误当作全量分析。
            output=export_report(self.dataset,self.graph,self.store,self.root_dir/"exports",self.paths,self.assessment,{'network_window_minutes':self.network_window.get(),'industry_window_hours':self.industry_window.get()},self.research.report_additions())
            self.status.set("报告已导出："+str(output));self.update_audit()
            messagebox.showinfo("报告已导出",f"保存位置：\n{output}\n\n包含 HTML 报告、SVG 图谱、JSON、核验线索 CSV 和校验清单。旁边另有 ZIP。")
            os.startfile(output)
        except Exception as exc:messagebox.showerror("报告导出",str(exc))

    def export_svg(self):
        path=filedialog.asksaveasfilename(title="保存当前图谱",defaultextension=".svg",initialdir=str(self.root_dir/"exports"),initialfile=self.current_case()+"_graph.svg",filetypes=[("SVG图像","*.svg")])
        if path:
            try:Path(path).write_text(graph_svg(self.view_graph,self.active_path),encoding="utf-8");self.status.set("图谱已保存："+path)
            except Exception as exc:messagebox.showerror("图谱导出",str(exc))

    def export_templates(self):
        directory=filedialog.askdirectory(title="选择模板导出位置")
        if not directory:return
        destination=Path(directory)/("四流数据模板_"+datetime.now(TZ).strftime("%Y%m%d_%H%M%S"))
        create_templates(destination)
        self.status.set("模板已导出："+str(destination));os.startfile(destination)

    def run_job(self,function,done,message):
        if self.busy:return messagebox.showinfo("正在处理","请等待当前导入或训练完成。")
        self.busy=True;self.status.set(message)
        if hasattr(self,'busy_progress'):self.busy_progress.start(12)
        def worker():
            try:self.jobs.put(("done",function(),done))
            except Exception as exc:
                from .orchestrator import Cancelled
                self.jobs.put(('cancelled' if isinstance(exc,Cancelled) else 'error',str(exc),None))
        threading.Thread(target=worker,daemon=True).start()

    def poll_jobs(self):
        try:
            while True:
                kind,value,callback=self.jobs.get_nowait()
                try:
                    if kind=="progress":self.training_log.insert("end",value+"\n");self.training_log.see("end");self.status.set(value)
                    elif kind=="done":
                        self.busy=False
                        if hasattr(self,'busy_progress'):self.busy_progress.stop()
                        callback(value)
                    elif kind=='cancelled':
                        self.busy=False;self.status.set(value)
                        if hasattr(self,'busy_progress'):self.busy_progress.stop()
                    else:
                        self.busy=False;self.status.set('操作未完成：'+value)
                        if hasattr(self,'busy_progress'):self.busy_progress.stop()
                        messagebox.showerror('操作未完成',value)
                except Exception as exc:
                    self.busy=False;logging.exception('Background result application failed')
                    if hasattr(self,'busy_progress'):self.busy_progress.stop()
                    self.status.set('结果应用失败，可修正后重新操作：'+str(exc));messagebox.showerror('操作未完成',str(exc))
        except queue.Empty:pass
        self.after(80,self.poll_jobs)

    def close(self):
        if self.busy:
            return messagebox.showinfo("正在处理","导入或训练仍在运行，请等待完成后关闭。")
        settings={"dataset":str(self.dataset.root)} if self.dataset else {}
        atomic_json(self.root_dir/'runtime/settings.json',settings)
        self.destroy()
