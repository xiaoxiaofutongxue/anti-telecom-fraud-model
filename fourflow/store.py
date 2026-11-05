import json
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .core import TZ,parse_time


class Store:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, created TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reviews(dataset TEXT, case_id TEXT, finding_id TEXT, status TEXT, note TEXT, reviewer TEXT, updated TEXT, PRIMARY KEY(dataset,case_id,finding_id));
            CREATE TABLE IF NOT EXISTS scenarios(dataset TEXT,case_id TEXT,payload TEXT,PRIMARY KEY(dataset,case_id));''')
            conn.executescript('''CREATE TABLE IF NOT EXISTS review_events(id INTEGER PRIMARY KEY, dataset TEXT NOT NULL,case_id TEXT NOT NULL,finding_id TEXT NOT NULL,status TEXT NOT NULL,note TEXT NOT NULL,reviewer TEXT NOT NULL,updated TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS review_events_lookup ON review_events(dataset,case_id,finding_id,updated);
            CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY);''')
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE name='review_history_v1'").fetchone():
                # 旧版保留过审计事件：在不改写原记录的前提下恢复可得版本。
                for created,payload in conn.execute("SELECT created,payload FROM audit WHERE action='review_saved' ORDER BY id").fetchall():
                    try:
                        record=json.loads(payload)
                        values=[record[k] for k in ('dataset','case_id','finding_id','status','note','reviewer')]+[parse_time(created).isoformat()]
                        conn.execute('INSERT INTO review_events(dataset,case_id,finding_id,status,note,reviewer,updated) VALUES (?,?,?,?,?,?,?)',values)
                    except (ValueError,KeyError,TypeError):continue
                for record in conn.execute('SELECT dataset,case_id,finding_id,status,note,reviewer,updated FROM reviews').fetchall():
                    conn.execute('INSERT INTO review_events(dataset,case_id,finding_id,status,note,reviewer,updated) VALUES (?,?,?,?,?,?,?)',record)
                conn.execute("INSERT INTO schema_migrations VALUES ('review_history_v1')")
            conn.execute('CREATE TABLE IF NOT EXISTS audit_chain(audit_id INTEGER PRIMARY KEY,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL)')
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE name='audit_chain_v1'").fetchone():
                if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
                previous=''
                for row in conn.execute('SELECT id,created,action,payload FROM audit ORDER BY id').fetchall():
                    digest=self._event_hash(row,previous);conn.execute('INSERT INTO audit_chain VALUES (?,?,?)',(row[0],previous,digest));previous=digest
                conn.execute("INSERT INTO schema_migrations VALUES ('audit_chain_v1')")
                conn.execute('CREATE TABLE IF NOT EXISTS integrity_metadata(key TEXT PRIMARY KEY,value TEXT)')
                conn.execute('INSERT OR REPLACE INTO integrity_metadata VALUES (?,?)',('legacy_sealed_at',datetime.now(TZ).isoformat()))

    @contextmanager
    def connect(self):
        connection=sqlite3.connect(self.path,timeout=20)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def audit(self,action,payload):
        with self.connect() as conn:
            self._append_audit(conn,datetime.now(TZ).isoformat(),action,json.dumps(payload,ensure_ascii=False,default=str))

    @staticmethod
    def _event_hash(row,previous):return hashlib.sha256(json.dumps([*tuple(row),previous],ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()

    def _check_audit(self,conn):
        rows=conn.execute('SELECT id,created,action,payload FROM audit ORDER BY id').fetchall();chain={r[0]:(r[1],r[2]) for r in conn.execute('SELECT audit_id,previous_hash,event_hash FROM audit_chain')};previous='';errors=[]
        if len(rows)!=len(chain):errors.append('审计记录与校验链数量不一致，可能有旧窗口未封存写入或文件修改')
        for row in rows:
            digest=self._event_hash(row,previous)
            if chain.get(row[0])!=(previous,digest):errors.append('审计记录 '+str(row[0])+' 校验不一致')
            previous=digest
        return {'valid':not errors,'events':len(rows),'head':previous,'errors':errors,'scope':'检测封存后的记录不一致；升级时封存旧记录，不证明升级前未被修改。此本地校验链不是数字签章或可信时间戳。'}

    def _append_audit(self,conn,created,action,payload):
        if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
        integrity=self._check_audit(conn)
        if not integrity['valid']:raise ValueError('审计一致性检查失败，请先备份并核对：'+integrity['errors'][0])
        cursor=conn.execute('INSERT INTO audit(created,action,payload) VALUES (?,?,?)',(created,action,payload));row=(cursor.lastrowid,created,action,payload);digest=self._event_hash(row,integrity['head']);conn.execute('INSERT INTO audit_chain VALUES (?,?,?)',(cursor.lastrowid,integrity['head'],digest))

    def verify_audit(self):
        with self.connect() as conn:return self._check_audit(conn)

    def review(self,dataset,case_id,finding_id,status,note,reviewer):
        if status not in {"待核验","已核实线索","已排除","待补充材料"} or not reviewer.strip() or not note.strip():
            raise ValueError("请选择有效状态，并填写核验人及依据说明")
        updated=datetime.now(TZ).isoformat()
        with self.connect() as conn:
            values=(dataset,case_id,finding_id,status,note.strip(),reviewer.strip(),updated)
            conn.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?,?,?)",values)
            conn.execute('INSERT INTO review_events(dataset,case_id,finding_id,status,note,reviewer,updated) VALUES (?,?,?,?,?,?,?)',values)
            payload={"dataset":dataset,"case_id":case_id,"finding_id":finding_id,"status":status,"reviewer":reviewer.strip(),"note":note.strip()}
            self._append_audit(conn,updated,'review_saved',json.dumps(payload,ensure_ascii=False))

    def reviews(self,dataset,case_id,as_of=None):
        with self.connect() as conn:
            conn.row_factory=sqlite3.Row
            # 兼容升级期间尚未退出的旧窗口写入最新状态，不覆盖已有历史。
            conn.execute('''INSERT INTO review_events(dataset,case_id,finding_id,status,note,reviewer,updated)
            SELECT r.dataset,r.case_id,r.finding_id,r.status,r.note,r.reviewer,r.updated FROM reviews r
            WHERE r.dataset=? AND r.case_id=? AND NOT EXISTS
            (SELECT 1 FROM review_events e WHERE e.dataset=r.dataset AND e.case_id=r.case_id AND e.finding_id=r.finding_id
            AND e.updated=r.updated AND e.status=r.status AND e.note=r.note AND e.reviewer=r.reviewer)''',(dataset,case_id))
            params=[dataset,case_id]
            condition=''
            if as_of:
                condition=' AND updated<=?';params.append(parse_time(as_of).isoformat())
            query='''SELECT dataset,case_id,finding_id,status,note,reviewer,updated FROM
            (SELECT *,ROW_NUMBER() OVER(PARTITION BY finding_id ORDER BY updated DESC,id DESC) AS position
             FROM review_events WHERE dataset=? AND case_id=?'''+condition+''') WHERE position=1 ORDER BY finding_id'''
            return [dict(r) for r in conn.execute(query,params)]

    def save_scenario(self,dataset,case_id,payload):
        payload={**payload,'recorded_at':datetime.now(TZ).isoformat()}
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO scenarios VALUES (?,?,?)",(dataset,case_id,json.dumps(payload,ensure_ascii=False)))
            self._append_audit(conn,datetime.now(TZ).isoformat(),'principal_scenario',json.dumps({"dataset":dataset,"case_id":case_id,**payload},ensure_ascii=False))

    def scenario(self,dataset,case_id):
        with self.connect() as conn:
            row=conn.execute("SELECT payload FROM scenarios WHERE dataset=? AND case_id=?",(dataset,case_id)).fetchone()
            return json.loads(row[0]) if row else None

    def recent_audit(self,limit=100):
        with self.connect() as conn:
            conn.row_factory=sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?",(limit,))]
