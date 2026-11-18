"""Subscribe to author declines and deliver private plugin design briefs."""
import argparse
from contextlib import closing
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import URLError

from event_sink import Sink
import plugin_design as design


FILTER = {"source":"journal:plugin.author", "module":"plugin.author", "kind":"plugin.author.declined",
          "code":"author.proposal_rejected"}
MODULE = "plugin.design_watch"
SOURCE_MODULE = "plugin.design"
HEX = re.compile("[0-9a-f]{64}")
RETRYABLE = {"receipt_missing","model_unavailable","model_reply_invalid","attempt_interrupted"}
STEPS = {
    "receipt_missing":"检查作者 decisions 卷挂载及收据是否已落盘，按 receipt_id 取回原文件。",
    "model_unavailable":"检查授权模型服务的地址、进程与可用模型，再用原收据复核。",
    "model_reply_invalid":"检查模型及结构化输出支持，离线复核请求和响应摘要。",
    "attempt_interrupted":"查原尝试和私有草案卷，核对进程退出和模型日志。",
    "configuration_changed":"恢复原作者与模型配置，按原设计请求复核并交接。",
    "source_conflict":"同一源事件出现了不同事实，比较作者事件源和原收据。",
}
SOURCE_FIELDS = ("source","module","kind","code","event_id","parent_event_id","trace_id","task_id","operation_id","data")


class EventWorkWatch:
    """One durable consumer per private directory. Subclasses provide the input and delivery contracts."""
    selector = FILTER
    namespace = MODULE
    source_module = SOURCE_MODULE
    code_prefix = "design_watch"
    retryable = RETRYABLE
    steps = STEPS
    def __init__(self, url, decisions, state, journal, model_url=None, model=None, *,
                 attempts=3, retry_delay=2, contracts_dir=None):
        self.state, self.decisions = Path(state), Path(decisions)
        self.model_url, self.model, self.contracts = model_url, model, contracts_dir
        self.budget, self.delay = attempts, retry_delay
        self.config = design.sha(design.json_bytes([design.sha(Path(design.__file__).read_bytes()),
                                    model_url, model, str(contracts_dir) if contracts_dir else None]))
        with design.writer_lock(self.state):
            self.connect(url,journal)

    def connect(self,url,journal):
        self.sink = Sink(self.state/"intake.sqlite", url, self.selector)
        self.db = self.sink.db
        try:
            self.journal = design.ModuleJournal(journal)
            self.sink.bind_journal(self.journal,self.source_module)
            legacy = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='design_jobs'").fetchone()
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS design_jobs(
                  op TEXT PRIMARY KEY, archive TEXT NOT NULL, seq INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                  state TEXT NOT NULL, attempts INTEGER NOT NULL, due REAL NOT NULL, head TEXT NOT NULL,
                  config TEXT, budget INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS design_pending ON design_jobs(state,due);
                CREATE TABLE IF NOT EXISTS design_intake(last_row INTEGER NOT NULL);
                INSERT INTO design_intake SELECT 0 WHERE NOT EXISTS(SELECT 1 FROM design_intake);
                CREATE TABLE IF NOT EXISTS design_outbox(event_id TEXT PRIMARY KEY,event TEXT NOT NULL);
            """)
            with self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS worker_owner(singleton INTEGER PRIMARY KEY,name TEXT NOT NULL)")
                self.db.execute("INSERT OR IGNORE INTO worker_owner VALUES(1,?)",(MODULE if legacy else self.namespace,))
                if self.db.execute("SELECT name FROM worker_owner WHERE singleton=1").fetchone()[0] != self.namespace:
                    raise ValueError("the state directory belongs to another consumer")
        except BaseException:
            self.sink.close()
            if hasattr(self,"journal"):
                self.journal.close()
            raise

    def close(self):
        self.sink.close()
        self.journal.close()

    def emit(self, stage, op, row, parent, identity, **data):
        """Stage the public fact within the private work transition's transaction."""
        eid = design.sha(design.json_bytes([self.namespace,op,stage,identity]))
        event = {"module":self.source_module,"kind":self.namespace+"."+stage,"code":self.code_prefix+"."+stage,"event_id":eid,
                 "operation_id":op,"trace_id":row.get("trace_id") if design.public_reference(row.get("trace_id")) else op,
                 "task_id":row.get("task_id") if design.public_reference(row.get("task_id")) else None,
                 "parent_event_id":parent if design.public_reference(parent) else None,
                 "level":"warn" if stage in ("retry","intervention_required","source_conflict") else "info",
                 "data":data}
        self.db.execute("INSERT OR IGNORE INTO design_outbox VALUES(?,?)",(eid,json.dumps(event)))
        return eid

    def flush(self):
        for eid, raw in self.db.execute("SELECT * FROM design_outbox ORDER BY rowid LIMIT 128").fetchall():
            with self.journal.db:
                self.journal.emit(json.loads(raw),key=eid)
            with self.db:
                self.db.execute("DELETE FROM design_outbox WHERE event_id=?",(eid,))

    def intake(self):
        rows = self.db.execute("SELECT rowid,archive_id,seq,event FROM events WHERE rowid>"
                               "(SELECT last_row FROM design_intake) ORDER BY rowid LIMIT 32").fetchall()
        with self.db:
            for rid, archive, seq, raw in rows:
                row = json.loads(raw)
                source_id = row.get("event_id")
                identity = source_id if design.public_reference(source_id) else [archive,seq]
                op = design.sha(design.json_bytes([self.namespace,identity]))
                fingerprint = design.sha(json.dumps([row.get(k) for k in SOURCE_FIELDS],sort_keys=True,
                                                      ensure_ascii=True,separators=(",",":")).encode())
                previous = self.db.execute("SELECT fingerprint,head,state,archive,seq FROM design_jobs WHERE op=?",(op,)).fetchone()
                if previous is None:
                    head = self.emit("queued",op,row,source_id,0,archive_id=archive,seq=seq)
                    self.db.execute("INSERT INTO design_jobs VALUES(?,?,?,?,'ready',0,0,?,NULL,?)",
                                    (op,archive,seq,fingerprint,head,self.budget))
                elif previous[0]!=fingerprint:
                    original = self.db.execute("SELECT event FROM events WHERE archive_id=? AND seq=?",previous[3:]).fetchone()
                    head = self.emit("source_conflict",op,json.loads(original[0]),previous[1],[archive,seq],reason="source_conflict",
                                     steps=[STEPS["source_conflict"]],archive_id=archive,seq=seq)
                    if previous[2] in ("ready","inflight"):
                        self.db.execute("UPDATE design_jobs SET state='intervention',head=? WHERE op=?",(head,op))
                self.db.execute("UPDATE design_intake SET last_row=?",(rid,))
        return len(rows)

    def work(self):
        cursor = self.db.execute("""SELECT w.op,w.head,w.state,w.attempts,w.budget,w.config,e.event
             FROM design_jobs w JOIN events e ON e.archive_id=w.archive AND e.seq=w.seq
             WHERE w.state IN ('ready','inflight') AND w.due<=? ORDER BY w.due,w.rowid LIMIT 1""",(time.time(),))
        job = cursor.fetchone()
        if not job:
            return False
        op, head, state, attempt, budget, config, raw = job
        row = json.loads(raw)
        resume = state=="inflight"
        if not resume:
            attempt += 1
            with self.db:
                head = self.emit("attempt",op,row,head,attempt,attempt=attempt,budget=budget)
                self.db.execute("UPDATE design_jobs SET state='inflight',attempts=?,head=?,config=coalesce(config,?) "
                                "WHERE op=?",(attempt,head,self.config,op))
            self.flush()
        request_id = design.sha(design.json_bytes([op,attempt]))
        try:
            if config and self.config!=config:
                raise design.DesignError("configuration_changed")
            result = self.execute(row,request_id,resume)
        except design.DesignError as exc:
            reason = str(exc)
            retry = reason in self.retryable and attempt<budget
            stage = "retry" if retry else "intervention_required"
            due = time.time()+min(60,self.delay*(2**(attempt-1))) if retry else 0
            with self.db:
                head = self.emit(stage,op,row,head,attempt,reason=reason,attempt=attempt,budget=budget,
                                 design_event_id=exc.event_id,request_id=request_id,
                                 steps=[] if retry else [self.steps.get(reason,"复核原事件、私有输入与已持久化工件，再交接研发审查。")])
                self.db.execute("UPDATE design_jobs SET state=?,due=?,head=? WHERE op=?",
                                ("ready" if retry else "intervention",due,head,op))
        else:
            with self.db:
                stage, evidence, parent = result
                head = self.emit(stage,op,row,parent,0,attempt=attempt,attempt_event_id=head,**evidence)
                self.db.execute("UPDATE design_jobs SET state=?,due=0,head=? WHERE op=?",(stage,head,op))
        return True

    def poll(self):
        with design.writer_lock(self.state):
            with self.journal.db:
                self.journal.heartbeat(240)
            self.flush()
            unavailable = False
            try:
                more = self.sink.copy()
            except (URLError,TimeoutError,ConnectionError,HTTPException):
                unavailable, more = True, False
            incoming = self.intake()
            worked = self.work()
            self.flush()
            return bool(more or incoming==32 or worked), unavailable


class DesignWatch(EventWorkWatch):
    def execute(self,row,request_id,recover_only):
        if any(row.get(k)!=v for k,v in FILTER.items()) or not isinstance(row.get("data"),dict):
            raise design.DesignError("source_ineligible")
        receipt = row["data"].get("receipt_id")
        if not isinstance(receipt,str) or not HEX.fullmatch(receipt):
            raise design.DesignError("receipt_id_invalid")
        path = self.decisions/(receipt+".json")
        if path.is_symlink() or path.exists() and not path.is_file():
            raise design.DesignError("receipt_invalid")
        if not path.is_file():
            raise design.DesignError("receipt_missing")
        result = design.run(row,path,self.state/"drafts",self.journal,self.model_url,self.model,
                          request_id=request_id,recover_only=recover_only,contracts_dir=self.contracts)
        return "completed", {k:result[k] for k in ("design_id","draft_sha256")}, result["event_id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",required=True)
    for name in ("decisions","state","journal"):
        parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--ollama-url")
    parser.add_argument("--model")
    parser.add_argument("--contracts-dir",type=Path,default=os.getenv("EDGE_DESIGN_CONTRACTS") or None)
    parser.add_argument("--attempts",type=int,default=3)
    parser.add_argument("--interval",type=float,default=1)
    parser.add_argument("--retry-delay",type=float,default=2)
    parser.add_argument("--once",action="store_true")
    args = parser.parse_args()
    if (bool(args.ollama_url)!=bool(args.model) or not 1<=args.attempts<=8 or not .1<=args.interval<=60
            or not .1<=args.retry_delay<=60):
        parser.error("provide a model and URL together, attempts 1–8, interval/retry-delay 0.1–60 seconds")
    return serve(lambda:DesignWatch(args.url,args.decisions,args.state,args.journal,args.ollama_url,args.model,
                 attempts=args.attempts,retry_delay=args.retry_delay,contracts_dir=args.contracts_dir),args,"design_watch")


def serve(create,args,component):
    try:
        with closing(create()) as watch:
            while True:
                more, unavailable = watch.poll()
                if not more:
                    if args.once:
                        return int(unavailable)
                    time.sleep(args.interval)
    except (OSError,sqlite3.Error,ValueError) as exc:
        print(json.dumps({"code":component+".unavailable","error_type":type(exc).__name__}),file=sys.stderr)
        return 1


if __name__=="__main__":
    sys.exit(main())
