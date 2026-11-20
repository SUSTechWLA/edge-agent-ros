"""Task ledger, owner lock and additive migrations for the Agent kernel."""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid

from agent.tracing import artifact
from edge_contracts.errors import Refused
from edge_contracts.soft_transport import SoftError
from edge_contracts.trace_content import clean_text, sanitized
from .authorization import check_id
from . import host
from .task_facts import read_task, detail_events


def utc():
    return datetime.now(timezone.utc).isoformat()


def trigger_input(data):
    if not isinstance(data,dict) or set(data)-{"kind","id","source","parent_task_id","parent_event_id","trace_id","read_only_tool"}:
        raise ValueError("trigger 格式无效")
    for field in ("kind","source","id",*set(data).intersection(("parent_task_id","parent_event_id","trace_id"))):
        value=data.get(field)
        if not isinstance(value,str) or not re.fullmatch(r"[\w.:-]{1,128}",value):
            raise ValueError("trigger."+field+" 格式无效")
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,31}",data["kind"]):
        raise ValueError("trigger.kind 格式无效")
    if data["source"] == "plugin.supervisor" and (data["kind"] != "agent" or
            not {"parent_task_id","parent_event_id","trace_id","read_only_tool"} <= data.keys()):
        raise ValueError("自主续办需要父任务、发布事件、原 trace 和只读工具绑定")
    if "read_only_tool" in data:
        pin = data["read_only_tool"]
        if (not isinstance(pin,dict) or set(pin)!={"id","version"} or not isinstance(pin["version"],str)
                or not re.fullmatch(r"[0-9a-f]{64}",pin["version"])):
            raise ValueError("trigger.read_only_tool 格式无效")
        check_id(pin["id"])
    return data


class Journal:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        with ExitStack() as on_failure:
            self.owner = on_failure.enter_context((directory / "agent.lock").open("a+b"))
            if os.name == "nt":
                import msvcrt
                self.owner.seek(0)
                msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock = threading.RLock()
            self.db = sqlite3.connect(directory / "tasks.db", check_same_thread=False)
            on_failure.callback(self.db.close)
            self._initialize_schema()
            on_failure.pop_all()

    def _initialize_schema(self):
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS audit_identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1),id TEXT NOT NULL);
            INSERT OR IGNORE INTO audit_identity VALUES(1,lower(hex(randomblob(16))));
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, text TEXT, state TEXT, detail TEXT,
                provider TEXT, steps TEXT, cursor INTEGER, retries INTEGER, due REAL, created TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, utc TEXT, task_id TEXT,
                step INTEGER, kind TEXT, detail TEXT, operation_id TEXT, payload TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS events_by_task ON events(task_id,seq);
            CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(state,due,created);
            CREATE TABLE IF NOT EXISTS soft_settlements(operation TEXT PRIMARY KEY, pin TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS soft_plan_intents(task TEXT PRIMARY KEY, plan TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_triggers(
              task_id TEXT PRIMARY KEY REFERENCES tasks(id), kind TEXT NOT NULL, source TEXT NOT NULL,
              source_key TEXT NOT NULL, context TEXT NOT NULL, trace_id TEXT NOT NULL,
              UNIQUE(kind,source,source_key));
        """)
        if "payload" not in {r[1] for r in self.db.execute("PRAGMA table_info(events)")}:
            self.db.execute("ALTER TABLE events ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")
            self.db.commit()
        if "event_id" not in {r[1] for r in self.db.execute("PRAGMA table_info(events)")}:
            self.db.execute("ALTER TABLE events ADD COLUMN event_id TEXT")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_by_id ON events(event_id) WHERE event_id IS NOT NULL")
        settlement_fields = {r[1] for r in self.db.execute("PRAGMA table_info(soft_settlements)")}
        for name, declaration in (("next_attempt","REAL NOT NULL DEFAULT 0"),
                                  ("attempts","INTEGER NOT NULL DEFAULT 0"), ("last_code","TEXT"),
                                  ("unissued","INTEGER NOT NULL DEFAULT 0")):
            if name not in settlement_fields:
                self.db.execute(f"ALTER TABLE soft_settlements ADD COLUMN {name} {declaration}")
        self.db.execute("CREATE INDEX IF NOT EXISTS soft_settlements_due ON soft_settlements(next_attempt)")
        self.db.commit()
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 1:
            self.repair_nonfinite_steps()
        self.quarantine_unreadable_active_steps()

    def quarantine_unreadable_active_steps(self):
        # SQLite JSON predicates in queue selection must never let one damaged legacy task
        # stop another. Preserve the exact original record for offline diagnosis.
        rows = self.db.execute("""SELECT id,steps FROM tasks WHERE state IN ('PLANNING','RUNNING','WAITING')
            AND CASE WHEN json_valid(steps) THEN json_type(steps) IS NOT 'array' ELSE 1 END""").fetchall()
        for row in rows:
            with self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS quarantined_steps(task_id TEXT PRIMARY KEY, original_steps BLOB)")
                self.db.execute("INSERT INTO quarantined_steps VALUES(?,?)", (row["id"], row["steps"]))
                self.db.execute("UPDATE tasks SET steps='[]' WHERE id=?", (row["id"],))
                self.save(self.get(row["id"]), "RECOVERY_REQUIRED", "活动任务的步骤记录无法读取，原文已隔离",
                          kind="steps_quarantined",diagnostic={"code":"journal.invalid_steps","origin":"journal"})

    def repair_nonfinite_steps(self):
        # Older providers could produce NaN/Infinity, making a confirmed task unreadable.
        # Preserve historical events and tag each invalid measurement in its original place.
        with self.db:
            for row in self.db.execute("SELECT id,steps FROM tasks WHERE json_valid(steps)=0").fetchall():
                try:
                    steps=json.loads(row["steps"],parse_constant=lambda token: {"invalid_number":token})
                except (TypeError, ValueError):
                    # Unrelated database corruption requires recovery from the original backup.
                    continue
                task=self.get(row["id"])
                task["steps"]=steps
                original_hash=hashlib.sha256(row["steps"].encode()).hexdigest()
                self.save(task,"RECOVERY_REQUIRED","历史步骤含非有限数值，需对照原始执行证据复核",
                          kind="evidence_corrected",diagnostic={"code":"tool.legacy_invalid_evidence",
                          "origin":"journal","original_steps_sha256":original_hash})
            self.db.execute("PRAGMA user_version=1")

    def get(self, key):
        with self.lock:
            return read_task(self.db, key)

    def create(self,key,text,trigger=None):
        with self.lock, self.db:
            if trigger is not None:
                trigger=trigger_input(trigger)
            identity=trigger or {"kind":"user","source":"user","id":key}
            if identity["id"] is not None:
                origin=self.db.execute("""SELECT task_id FROM task_triggers WHERE kind=? AND source=? AND source_key=?""",
                    (identity["kind"],identity["source"],identity["id"])).fetchone()
                if origin:
                    if key is not None and key!=origin[0]:
                        raise Refused("同一触发已绑定其他任务 ID")
                    key=origin[0]
            key=key or uuid.uuid4().hex
            existing = self.get(key)
            if existing:
                if existing["text"]!=text or (trigger is not None and existing["trigger"]!=trigger):
                    raise Refused("同一任务 ID 已用于不同请求或触发来源")
                return existing
            trigger=trigger or {"kind":"user","source":"user","id":key}
            parent=self.get(trigger["parent_task_id"]) if trigger.get("parent_task_id") else None
            if trigger["source"] == "plugin.supervisor" and (not parent or parent["state"] != "BLOCKED"
                    or parent["steps"] or parent["text"] != text or key != trigger["id"]):
                raise Refused("自主续办必须匹配尚无步骤的原始阻塞任务、目标和触发 ID")
            parent_event=trigger.get("parent_event_id")
            if parent_event:
                row=self.db.execute("SELECT task_id FROM events WHERE event_id=?",(parent_event,)).fetchone()
                if row:
                    if trigger.get("parent_task_id",row[0])!=row[0]:
                        raise Refused("父事件与父任务不一致")
                    parent=self.get(row[0])
            if parent and trigger.get("trace_id",parent["trace_id"])!=parent["trace_id"]:
                raise Refused("trace_id 与本地父任务不一致")
            trace_id=parent["trace_id"] if parent else trigger.get("trace_id") or key
            now = utc()
            self.db.execute("INSERT INTO tasks VALUES(?,?,'PLANNING','等待生成计划',NULL,'[]',0,0,0,?,?)",
                            (key,text,now,now))
            self.db.execute("INSERT INTO task_triggers VALUES(?,?,?,?,?,?)",
                (key,trigger["kind"],trigger["source"],trigger["id"],json.dumps(trigger),trace_id))
            task=self.get(key)
            self.append(task,"accepted","已受理任务",{"request":artifact(text)},step=0)
            return self.get(key)

    def append(self,task,kind,detail,payload,*,step,op=None,trace=None):
        """Caller owns the transaction, so state and causal event commit together."""
        prior=self.db.execute("SELECT event_id FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 1",
                              (task["id"],)).fetchone()
        event_id=uuid.uuid4().hex
        trigger=task["trigger"]
        parent_id=prior[0] if prior else (trigger or {}).get("parent_event_id")
        envelope={"schema_version":1,"module":"agent","layer":"task","code":"agent."+kind,
                  **(trace or {}),"event_id":event_id,"trace_id":task["trace_id"],
                  "parent_event_id":parent_id,"trigger":trigger}
        self.db.execute("""INSERT INTO events(utc,task_id,step,kind,detail,operation_id,payload,event_id)
                           VALUES(?,?,?,?,?,?,?,?)""",
            (utc(),task["id"],step,kind,clean_text(detail)[:1200],op,
             json.dumps({**payload,"trace":envelope},ensure_ascii=False),event_id))

    def exchange_event(self,task,kind,trace,operation_id):
        with self.lock,self.db:
            if trace.get("module")=="edge_core.submit_mission" and kind in ("ros.request","ros.response"):
                self.db.execute("UPDATE tasks SET steps=? WHERE id=?",
                                (json.dumps(task["steps"]),task["id"]))
            if "diagnosis" in trace:
                trace={**trace,"code":trace["diagnosis"]["code"]}
            self.append(task,kind,trace.get("diagnosis",{}).get("message",kind),{},
                        step=task["cursor"],op=operation_id,trace=trace)

    def provider_context(self,task,op):
        with self.lock:
            row=self.db.execute("SELECT event_id FROM events WHERE task_id=? AND operation_id=? AND kind='dispatch' ORDER BY seq LIMIT 1",
                                (task["id"],op)).fetchone()
        return {"trace_id":task["trace_id"],"task_id":task["id"],"parent_event_id":row[0] if row else None,
                "trigger":task.get("trigger") or {}}

    def due(self, planning, verifying=False):
        states = ("PLANNING","PLANNING") if planning else ("RUNNING","WAITING")
        # One executor and one verifier consume disjoint durable step states. Slow evidence
        # storage cannot occupy the thread responsible for dispatch or ROS polling.
        queue = ""
        if not planning:
            condition = "IS" if verifying else "IS NOT"
            queue = f" AND json_extract(steps,'$[' || cursor || '].status') {condition} 'VERIFYING'"
        with self.lock:
            row=self.db.execute("SELECT id FROM tasks WHERE state IN (?,?) AND due<=?" + queue +
                                " ORDER BY due,created LIMIT 1",
                                (*states,time.time())).fetchone()
            return self.get(row[0]) if row else None

    def retry(self,key):
        with self.lock:
            task = self.get(key)
            if task is None:
                raise KeyError(key)
            if not task["can_retry"]:
                raise Refused("仅可续办因暂时故障耗尽重试的任务")
            task["retries"] = 0
            op = None
            if task["steps"]:
                step = task["steps"][task["cursor"]]
                op = step.get("operation_id")
                if op:
                    step["resumed_at"] = time.time()
            self.save(task,"RUNNING" if task["steps"] else "PLANNING","操作员已请求用原步骤继续核对",
                      kind="retry_requested",op=op)
            return self.get(key)

    def save(self,t, state, detail, delay=0, kind=None, op=None, diagnostic=None, tool_response=None, unissued_pins=()):
        now=utc()
        with self.lock,self.db:
            prior=self.db.execute("SELECT state FROM tasks WHERE id=?",(t["id"],)).fetchone()[0]
            if unissued_pins:
                saved = self.db.execute("SELECT plan FROM soft_plan_intents WHERE task=?", (t["id"],)).fetchone()
                dispatched = self.db.execute("SELECT 1 FROM events WHERE task_id=? AND kind='dispatch' LIMIT 1",
                                             (t["id"],)).fetchone()
                proposal = json.loads(saved[0])[1] if saved else []
                expected = [dict(step["soft_pin"],reservation_id=f"{t['id']}/step/{index}")
                            for index,step in enumerate(proposal,1) if "soft_pin" in step]
                if (prior == "PLANNING" and state == "BLOCKED" and kind == "blocked" and not t["steps"]
                        and not dispatched and expected == unissued_pins):
                    self.db.executemany("""INSERT OR IGNORE INTO soft_settlements(operation,pin,next_attempt,unissued)
                        VALUES(?,?,?,1)""", ((pin["reservation_id"],json.dumps(pin),time.time()) for pin in unissued_pins))
                else:
                    self.append(t,"admission_cleanup_withheld","预约与本机原计划不一致，保留原绑定待核对",{},step=None)
            if tool_response is not None:
                self.append(t, "tool.response", "tool.response", {}, step=t["cursor"], op=op, trace=tool_response)
            if kind is None and prior!=state:
                kind="transition"
            self.db.execute("""UPDATE tasks SET state=?,detail=?,provider=?,steps=?,cursor=?,retries=?,due=?,updated=? WHERE id=?""",
                (state,clean_text(detail)[:1200],t["provider"],json.dumps(t["steps"]),t["cursor"],t["retries"],time.time()+delay,now,t["id"]))
            if kind == "planned":
                self.db.execute("DELETE FROM soft_plan_intents WHERE task=?", (t["id"],))
            if kind in ("verified","blocked","canceled"):
                # The journal owner releases a proven terminal step, and any
                # never-dispatched steps abandoned behind it. Unknowns keep their pin.
                closed = (t["steps"][t["cursor"]-1:t["cursor"]] if kind == "verified"
                          else t["steps"][t["cursor"]:])
                for step in closed:
                    pin = step.get("soft_pin") or step.get("physical_pin")
                    if pin:
                        self.db.execute("INSERT OR IGNORE INTO soft_settlements(operation,pin,next_attempt) VALUES(?,?,?)",
                                        (pin["reservation_id"],json.dumps(pin),time.time()))
            if kind:
                index = t["cursor"]-1 if kind=="verified" else t["cursor"]
                payload = {"provider":t["provider"],"prior_state":prior,"state":state,"retries":t["retries"]}
                if diagnostic:
                    payload["diagnostic"]=sanitized({k:v for k,v in diagnostic.items() if k!="proposal"})
                    if "proposal" in diagnostic:
                        payload["diagnostic"]["proposal_artifact"]=artifact(diagnostic["proposal"])
                if kind=="planned":
                    payload["steps"]=[project_step(s) for s in t["steps"]]
                elif op and 0 <= index < len(t["steps"]):
                    payload["step"]=project_step(t["steps"][index])
                trace={}
                if diagnostic and diagnostic.get("code"):
                    if kind in ("evidence_corrected","steps_quarantined"):
                        layer = "journal"
                    elif diagnostic["code"].startswith(("verification.","plugin.")):
                        layer = "verification"
                    else:
                        layer = "planner" if op is None else "tool"
                    trace={"layer":layer,"code":diagnostic["code"],
                           "diagnosis":payload["diagnostic"]}
                self.append(t,kind,detail,payload,step=index,op=op,trace=trace)

    def soft_intent(self, task, candidate=None):
        with self.lock,self.db:
            if candidate is not None:
                self.db.execute("INSERT OR IGNORE INTO soft_plan_intents VALUES(?,?)",
                                (task,json.dumps(candidate,allow_nan=False)))
            row = self.db.execute("SELECT plan FROM soft_plan_intents WHERE task=?",(task,)).fetchone()
            return json.loads(row[0]) if row else None

    def settle_soft_batch(self, stop):
        with self.lock:
            rows = self.db.execute("""SELECT operation,pin,attempts,last_code,unissued FROM soft_settlements
                WHERE next_attempt <= ? ORDER BY next_attempt,rowid LIMIT 64""",(time.time(),)).fetchall()
        for op, raw, attempts, previous_code, unissued in rows:
            if stop.is_set():
                break
            code = None
            try:
                pin = json.loads(raw)
                if not isinstance(pin,dict) or pin.get("reservation_id") != op:
                    raise ValueError("stored settlement pin mismatch")
                if host.call_for(pin,"settle_unissued" if unissued else "settle") is not True:
                    code = "plugin.settlement_unconfirmed"
            except SoftError as exc:
                code = exc.code if isinstance(exc.code,str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}",exc.code) else "plugin.transport"
            except (TypeError, ValueError, RecursionError):
                code = "plugin.settlement_pin_invalid"
            with self.lock,self.db:
                if code:
                    due = time.time() + min(60, 2 ** min(attempts,6))
                    self.db.execute("""UPDATE soft_settlements SET next_attempt=?,attempts=attempts+1,
                        last_code=? WHERE operation=?""",(due,code,op))
                else:
                    self.db.execute("DELETE FROM soft_settlements WHERE operation=?",(op,))
                if code != previous_code:
                    task_id, _, number = op.rpartition("/step/")
                    task = self.get(task_id) or {"id":task_id or "soft.settlements","trace_id":task_id or op,"trigger":None}
                    kind = "settlement_deferred" if code else "settlement_recovered"
                    self.append(task,kind,"插件结算待重试" if code else "原操作已确认结算",
                                {"attempt":attempts+1,"code":code or "plugin.settlement_recovered","previous_code":previous_code},
                                step=int(number)-1 if number.isdecimal() and len(number)<10 else None,op=op)

    def settle_soft(self, stop):
        while not stop.wait(.3):
            self.settle_soft_batch(stop)

    def event_page(self, key, after=None, limit=128):
        with self.lock:
            return detail_events(self.db, key, after, limit)


def project_step(step):
    result=dict(step)
    for key in ("args","evidence"):
        if key in result:
            captured=artifact(result[key])
            result[key]=captured.get("content",captured)
    if "verification" in result:
        captured = artifact(result["verification"])
        result["verification"] = captured.get("content", captured)
    return result
