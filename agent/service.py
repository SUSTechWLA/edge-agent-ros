"""Persistent natural-language task supervisor. The ROS executive owns physical actions."""
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from nav_msgs.msg import Odometry
from geometry_msgs.msg import Point
from edge_interfaces.srv import GetMission, SubmitMission
from derived_tools import check_id, evaluate
import runtime_plugins
from planning import (OutcomeUnknown, Refused, Unavailable, binding, catalog,
                      failure, http_json, plan, plan_read_only, registry, remote_failure, verification_plan)
from tracing import artifact, clean_text, diagnosis, exchange, sanitized


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
        self.owner = open(directory/"agent.lock","a")
        fcntl.flock(self.owner,fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory/"tasks.db",check_same_thread=False)
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
            row = self.db.execute("""SELECT tasks.*,tr.context,tr.trace_id,
                       (SELECT kind FROM events WHERE task_id=tasks.id ORDER BY seq DESC LIMIT 1) AS latest_event
                       FROM tasks LEFT JOIN task_triggers tr ON tr.task_id=tasks.id WHERE tasks.id=?""",(key,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["steps"] = json.loads(item["steps"])
        context=item.pop("context")
        item["trigger"]=json.loads(context) if context is not None else None
        item["trace_id"]=item["trace_id"] or key
        item["can_retry"] = item.pop("latest_event")=="retry_exhausted" and item["state"] in ("BLOCKED","RECOVERY_REQUIRED")
        return item

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

    def save(self,t, state, detail, delay=0, kind=None, op=None, diagnostic=None, tool_response=None):
        now=utc()
        with self.lock,self.db:
            prior=self.db.execute("SELECT state FROM tasks WHERE id=?",(t["id"],)).fetchone()[0]
            if tool_response is not None:
                self.append(t, "tool.response", "tool.response", {}, step=t["cursor"], op=op, trace=tool_response)
            if kind is None and prior!=state:
                kind="transition"
            self.db.execute("""UPDATE tasks SET state=?,detail=?,provider=?,steps=?,cursor=?,retries=?,due=?,updated=? WHERE id=?""",
                (state,clean_text(detail)[:1200],t["provider"],json.dumps(t["steps"]),t["cursor"],t["retries"],time.time()+delay,now,t["id"]))
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

    def event_page(self,key,after=None,limit=128):
        if not 1<=limit<=500 or (after is not None and after<0):
            raise ValueError("事件分页参数无效")
        rows=[]
        size=0
        with self.lock:
            reverse=after is None
            order="DESC" if reverse else "ASC"
            stream=self.db.execute("SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq "+order+" LIMIT ?",
                                   (key,after or 0,limit+1))
            for row in stream:
                size+=len(row["payload"].encode())
                if len(rows)==limit or (rows and size>350000):
                    return (rows[::-1] if reverse else rows),True
                rows.append(dict(row))
        return (rows[::-1] if reverse else rows),False


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


class Robot(Node):
    def __init__(self):
        super().__init__("edge_language_agent")
        self.submit = self.create_client(SubmitMission,"/edge/submit_mission")
        self.query = self.create_client(GetMission,"/edge/get_mission")
        self.sample = None
        self.create_subscription(Odometry,"/odom",self.receive_odom,qos_profile_sensor_data)

    def receive_odom(self,msg):
        stamp=msg.header.stamp
        position=msg.pose.pose.position
        q=msg.pose.pose.orientation
        if not all(math.isfinite(v) for v in (position.x,position.y,q.w,q.x,q.y,q.z,msg.twist.twist.linear.x)):
            self.sample=None
            return
        if self.sample and stamp==self.sample[1].header.stamp:
            return
        self.sample=time.monotonic(),msg

    def call(self,client,req):
        if not client.wait_for_service(timeout_sec=1):
            raise Unavailable(f"{client.srv_name} 尚未就绪")
        future=client.call_async(req)
        done=threading.Event()
        future.add_done_callback(lambda _:done.set())
        if not done.wait(4):
            client.remove_pending_request(future)
            raise Unavailable(f"{client.srv_name} 回执超时")
        return future.result()

    def odometry(self):
        sample=self.sample
        if not sample or time.monotonic()-sample[0]>2:
            raise failure(Unavailable,"等待新鲜里程计","tool.odom_stale","ros.odometry")
        msg=sample[1]
        p=msg.pose.pose.position
        q=msg.pose.pose.orientation
        return {"source":self.resolve_topic_name("/odom"),"frame":msg.header.frame_id,
                "stamp_ns":msg.header.stamp.sec*1000000000+msg.header.stamp.nanosec,
                "stamp":{"sec":msg.header.stamp.sec,"nanosec":msg.header.stamp.nanosec},"x":p.x,"y":p.y,
                "yaw":math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z)),"speed":msg.twist.twist.linear.x}

    def run(self,step,op,cap,trace=None,context=None):
        if step["tool"] == "status" and not cap.get("endpoint"):
            return self.odometry()
        if "derived" in cap and not cap.get("endpoint"):
            try:
                evidence = evaluate(cap["derived"], self.odometry())
            except ValueError as exc:
                raise failure(Unavailable, str(exc), "tool.derived_sample_invalid", "ros.odometry") from exc
            return {**evidence, "definition_version": cap["version"]}
        if step["tool"]=="inspect" and not cap.get("endpoint"):
            def current():
                s=self.call(self.query,GetMission.Request(mission_id=op))
                points=[[p.x,p.y] for p in s.evidence]
                if any(not math.isfinite(v) for xy in points for v in xy):
                    raise failure(OutcomeUnknown,"巡检回执含非有限坐标","tool.receipt_invalid","edge_core")
                return {"found":s.found,"state":s.state,"detail":s.detail,"verified_count":s.verified_count,
                        "points":points}
            status=exchange(trace,"ros","edge_core.get_mission",{"mission_id":op},current)
            binding_state=step.get("ros_binding")
            if not status["found"] and binding_state!="prepared":
                raise failure(OutcomeUnknown,"原操作可能已执行，ROS 内核查不到记录",
                              "navigation.mission_unaccounted","edge_core")
            if binding_state!="bound":
                # GetMission identifies a row but does not expose its requested waypoints.
                # SubmitMission is the core's atomic check for an exact operation/payload pair.
                pts=[Point(x=float(x),y=float(y)) for x,y in step["args"]["points"]]
                def submit():
                    s=self.call(self.submit,SubmitMission.Request(mission_id=op,waypoints=pts))
                    step["ros_binding"]="bound" if s.accepted else "prepared"
                    return {"accepted":s.accepted,"state":s.state,"detail":s.detail}
                step["ros_binding"]="sent"
                response=exchange(trace,"ros","edge_core.submit_mission",{"mission_id":op,"waypoints":step["args"]["points"]},submit)
                if not response["accepted"]:
                    if "occupied" in response["detail"]:
                        raise failure(Unavailable,response["detail"],"core.base_occupied","edge_core")
                    raise failure(Refused,response["detail"],"navigation.goal_rejected","edge_core")
                if not status["found"] or response["state"]!=status["state"]:
                    return None
            state=status["state"]
            if state in ("RUNNING","CANCELING"):
                return None
            if state=="RECOVERY_REQUIRED":
                raise failure(OutcomeUnknown,status["detail"],"navigation.outcome_unknown","edge_core")
            if state=="CANCELED":
                raise failure(OperationCanceled,status["detail"],"navigation.canceled","edge_core")
            if state!="SUCCEEDED":
                raise failure(Refused,state+": "+status["detail"],"navigation.failed","edge_core")
            count=len(step["args"]["points"])
            if (status["verified_count"]!=count or len(status["points"])!=count or
                    any(math.dist(point,target)>.12 for point,target in zip(status["points"],step["args"]["points"]))):
                raise failure(OutcomeUnknown,"巡检终态与有序点位的逐点验收不符","tool.receipt_invalid","edge_core")
            return {"mission_id":op,"verified_count":status["verified_count"],"points":status["points"]}
        try:
            body={"operation_id":op,"arguments":step["args"],"version":step["version"]}
            if context is not None:
                body["trace"]=context
            result=exchange(trace,"adapter",step["tool"],body,lambda:http_json(cap["endpoint"],body))
        except Refused as exc:
            if cap["effect"]!="read":
                raise failure(OutcomeUnknown,str(exc),getattr(exc,"code","tool.receipt_invalid"),
                              getattr(exc,"origin","provider"),getattr(exc,"details",None)) from exc
            raise
        if not isinstance(result,dict) or result.get("operation_id")!=op:
            raise failure(OutcomeUnknown,"插件回执缺少原始 operation_id","tool.receipt_invalid","provider")
        if result.get("state")=="RUNNING":
            return None
        if result.get("state")=="CANCELED":
            raise OperationCanceled(str(result.get("detail","操作已取消")))
        if result.get("state") in ("FAILED","BLOCKED"):
            raise remote_failure(Refused,result,"tool.provider_failed")
        if result.get("state")!="SUCCEEDED" or not result.get("evidence"):
            raise remote_failure(OutcomeUnknown,result,"tool.receipt_invalid")
        for outcome, field in cap.get("outcomes",{}).items():
            evidence = result["evidence"]
            if not isinstance(evidence,dict) or field not in evidence or evidence[field] in (None,""):
                raise OutcomeUnknown(f"插件缺少 {outcome} 要求的证据字段 {field}")
        return result["evidence"]


class OperationCanceled(Exception):
    """The executor confirmed that its operation was canceled."""


def prepare_verification(step, cap):
    if "verification" not in step:
        check = verification_plan(cap, runtime_plugins.load_policy())
        if check["required"] and (step["status"] != "PENDING" or step.get("operation_id")):
            raise failure(OutcomeUnknown, "旧操作已派发且没有验收绑定，请按原操作 ID 核查",
                          "verification.legacy_unbound", "registry")
        step["verification"] = check
    check = step["verification"]
    if check["required"] and not check["pins"]:
        directory = runtime_plugins.snapshot()
        check["pins"] = [directory.pin(name) for name in check["required"]]


def verify_candidate(journal, task, step, op, trace):
    check = step["verification"]
    # Completed decisions were committed separately; resume at the first outstanding service.
    for pin in check["pins"][len(check["decisions"]):]:
        runtime_plugins.snapshot().pin(pin["service"], expected_binding=pin["binding"])
        request = {"operation_id":op, "task_id":task["id"], "trace_id":task["trace_id"],
                   "tool":step["tool"], "arguments":step["args"], "outcomes":check["outcomes"],
                   "evidence":check["candidate"], "started_at":step["started_at"],
                   **{k:pin[k] for k in ("service","plugin_id","plugin_version")}}
        decision = runtime_plugins.verify(pin, request, trace)
        if decision["verdict"] == "fail":
            check["decisions"].append(decision)
            raise failure(Refused, "独立验收发现反证：" + pin["service"],
                          "verification.rejected", pin["plugin_id"], decision["evidence"])
        check["decisions"].append(decision)
        task["retries"] = 0
        journal.save(task, "RUNNING", "已记录独立验收：" + pin["service"], kind="verification_passed", op=op)
    return check["candidate"]


def finish_step(journal, task, step, op, result):
    step.update(status="SUCCEEDED", evidence=result)
    step["verification"].pop("candidate", None)
    task["cursor"] += 1
    task["retries"] = 0
    last = task["cursor"] == len(task["steps"])
    journal.save(task, "SUCCEEDED" if last else "RUNNING", "已取得步骤证据", kind="verified", op=op)


def worker(journal,robot,planning,verifying=False):
    while rclpy.ok():
        t=journal.due(planning, verifying)
        if not t:
            time.sleep(0.2)
            continue
        op=f"{t['id']}/step/{t['cursor']+1}"
        def trace(kind, fields):
            journal.exchange_event(t,kind,fields,None if planning else op)
        try:
            if planning:
                pin = (t.get("trigger") or {}).get("read_only_tool")
                if pin:
                    t["provider"],t["steps"]=plan_read_only(t["text"],registry(),pin)
                else:
                    t["provider"],t["steps"]=plan(t["text"],registry(),trace)
                t["retries"]=0
                journal.save(t,"RUNNING","已验证计划",kind="planned")
                continue
            step=t["steps"][t["cursor"]]
            if verifying:
                result = verify_candidate(journal, t, step, op, trace)
                finish_step(journal, t, step, op, result)
                continue
            cap=registry().get(step["tool"])
            if "binding" not in step and step["tool"] not in ("inspect","status"):
                raise failure(OutcomeUnknown,"旧步骤未保存提供者绑定，请按原操作 ID 核对旧提供者的记录",
                              "tool.binding_unverified","registry")
            if (not cap or cap["version"]!=step["version"] or
                    ("binding" in step and binding(cap)!=step["binding"]) or
                    ("binding" not in step and step["tool"] in ("inspect","status") and cap.get("endpoint"))):
                raise failure(Unavailable,"所需能力未挂载，或提供者与已保存的计划不一致","tool.binding_unavailable","registry")
            prepare_verification(step, cap)
            if step["status"]=="PENDING":
                step.update(status="RUNNING",operation_id=op,started_at=time.time())
                if step["tool"]=="inspect" and not cap.get("endpoint"):
                    step["ros_binding"]="prepared"
                journal.save(t,"RUNNING","已持久化步骤意图",kind="dispatch",op=op)
            context=journal.provider_context(t,op) if cap.get("accepts_trace") is True else None
            def save_candidate(candidate, receipt):
                step["verification"]["candidate"] = candidate
                step["status"] = "VERIFYING"
                t["retries"] = 0
                journal.save(t, "RUNNING", "已保存工具候选结果，等待独立验收", kind="verification_pending",
                             op=op, tool_response=receipt)
            result=exchange(trace,"tool",step["tool"],{"operation_id":op,"arguments":step["args"],"version":step["version"]},
                            lambda:robot.run(step,op,cap,trace,context),
                            meta={"provider":"http-adapter" if cap.get("endpoint") else "ros","binding":binding(cap)},
                            commit_candidate=save_candidate if step["verification"]["required"] else None)
            if result is None:
                if time.time()-step.get("resumed_at",step["started_at"])>float(os.getenv("EDGE_STEP_TIMEOUT","1800")):
                    journal.save(t,"RECOVERY_REQUIRED","等待原操作终态超时，请对账",kind="timeout",op=op)
                else:
                    journal.save(t,"WAITING","等待原操作回执",delay=.6)
                continue
            if step["status"] == "VERIFYING":
                continue
            finish_step(journal, t, step, op, result)
        except (Unavailable, runtime_plugins.PluginUnavailable, runtime_plugins.PolicyError) as exc:
            diagnostic=event_diagnosis(exc,planning,"tool.transport_unavailable")
            if planning and diagnostic.get("provider"):
                t["provider"]=diagnostic["provider"]
            t["retries"]+=1
            exhausted=t["retries"]>int(os.getenv("EDGE_RETRY_LIMIT","7"))
            journal.save(t,("BLOCKED" if planning else "RECOVERY_REQUIRED") if exhausted else
                         ("PLANNING" if planning else "WAITING"),str(exc),delay=min(30,2**min(t["retries"]-1,5)),
                         kind="retry_exhausted" if exhausted else "retry",op=None if planning else op,diagnostic=diagnostic)
        except Refused as exc:
            diagnostic=event_diagnosis(exc,planning,"tool.rejected")
            if planning and diagnostic.get("provider"):
                t["provider"]=diagnostic["provider"]
            journal.save(t,"BLOCKED",str(exc),kind="blocked",op=None if planning else op,diagnostic=diagnostic)
        except OutcomeUnknown as exc:
            journal.save(t,"RECOVERY_REQUIRED",str(exc),kind="error",op=op,diagnostic=event_diagnosis(exc,planning,"tool.receipt_invalid"))
        except OperationCanceled as exc:
            journal.save(t,"CANCELED",str(exc),kind="canceled",op=op,diagnostic=event_diagnosis(exc,planning,"tool.canceled"))
        except Exception as exc:
            journal.save(t,"RECOVERY_REQUIRED",f"{type(exc).__name__}: {exc}",kind="error",op=op,
                         diagnostic=event_diagnosis(exc,planning,"agent.unexpected_error"))


def event_diagnosis(exc,planning,fallback):
    extra=getattr(exc,"diagnostic",{})
    if planning:
        fallback={"capability_check":"planner.capability_missing","plan_validation":"planner.invalid_proposal",
                  "planner":"planner.provider_failed"}.get(extra.get("stage"),fallback)
    return {**extra,**diagnosis(exc,fallback,"planner" if planning else "agent")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):
        pass

    def send(self,status,value):
        data=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path=urlsplit(self.path)
        if path.path=="/agent/capabilities":
            return self.send(200,{"capabilities":catalog(registry())})
        if path.path=="/agent/plugins":
            try:
                directory = self.server.registry_observer.observe()
            except runtime_plugins.PluginUnavailable as exc:
                return self.send(503, {"code":exc.code,"detail":str(exc)})
            return self.send(200,directory)
        if path.path=="/agent/services":
            query = parse_qs(path.query, keep_blank_values=True)
            name = query.get("name", [""])[0]
            expected = query.get("binding", [None])[0]
            if (set(query) - {"name","binding"} or any(len(v) != 1 for v in query.values())
                    or not runtime_plugins.NAME.fullmatch(name)
                    or expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected)):
                return self.send(400, {"code":"plugin.lookup_invalid","detail":"expected a service name and optional SHA-256 binding"})
            try:
                resolved = runtime_plugins.snapshot().pin(name, expected_binding=expected)
            except runtime_plugins.PluginUnavailable as exc:
                return self.send(409, {"code":exc.code,"detail":str(exc)})
            return self.send(200, resolved)
        if path.path=="/agent/tasks":
            key=parse_qs(path.query).get("id",[""])[0]
            task=self.server.journal.get(key)
            if task:
                query=parse_qs(path.query)
                try:
                    after=int(query["after"][0]) if "after" in query else None
                    limit=int(query.get("limit",["128"])[0])
                    task["events"],task["events_has_more"]=self.server.journal.event_page(key,after,limit)
                except ValueError:
                    return self.send(400,{"detail":"事件分页参数无效"})
                return self.send(200,task)
        self.send(404,{"detail":"未找到记录"})

    def do_POST(self):
        if self.path not in ("/agent/tasks","/agent/retry"):
            return self.send(404,{})
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=80000:
                raise ValueError("请求长度无效")
            request=json.loads(self.rfile.read(size))
            if not isinstance(request,dict):
                raise ValueError("请求必须是对象")
            key=request.get("id")
            text=request.get("text")
            if (key is not None or self.path=="/agent/retry") and (
                    not isinstance(key,str) or not re.fullmatch(r"[\w.-]{1,96}",key)):
                raise ValueError("ID 格式无效")
            if self.path=="/agent/retry":
                return self.send(202,self.server.journal.retry(key))
            if not isinstance(text,str) or not 1<=len(text.strip())<=4096:
                raise ValueError("任务文本长度应为 1–4096 字符")
            self.send(202,self.server.journal.create(key,text.strip(),request.get("trigger")))
        except (TypeError,ValueError) as exc:
            self.send(400,{"detail":str(exc)})
        except Refused as exc:
            self.send(409,{"detail":str(exc)})
        except KeyError:
            self.send(404,{"detail":"未找到记录"})


if __name__=="__main__":
    task_dir=Path(os.getenv("EDGE_TASK_DIR","/var/lib/edge-tasks"))
    journal=Journal(task_dir)
    rclpy.init()
    robot=Robot()
    threading.Thread(target=rclpy.spin,args=(robot,),daemon=True).start()
    for planning in (True,False):
        threading.Thread(target=worker,args=(journal,robot,planning),daemon=True).start()
    threading.Thread(target=worker,args=(journal,robot,False,True),daemon=True).start()
    server=ThreadingHTTPServer((os.getenv("EDGE_AGENT_BIND","0.0.0.0"),8090),Handler)
    server.journal=journal
    server.registry_observer=runtime_plugins.RegistryObserver(task_dir/"module-events.sqlite")
    stop=threading.Event()
    monitor=threading.Thread(target=server.registry_observer.run,args=(stop,),daemon=True)
    monitor.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        monitor.join(timeout=2)
