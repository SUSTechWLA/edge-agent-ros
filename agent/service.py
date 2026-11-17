"""Persistent natural-language task supervisor. The ROS executive owns physical actions."""
import fcntl
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
from planning import OutcomeUnknown, Refused, Unavailable, binding, catalog, http_json, plan, registry


def utc():
    return datetime.now(timezone.utc).isoformat()


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
        """)
        if "payload" not in {r[1] for r in self.db.execute("PRAGMA table_info(events)")}:
            self.db.execute("ALTER TABLE events ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")
            self.db.commit()

    def get(self, key):
        with self.lock:
            row = self.db.execute("""SELECT tasks.*, (SELECT kind FROM events WHERE task_id=tasks.id
                       ORDER BY seq DESC LIMIT 1) AS latest_event FROM tasks WHERE id=?""",(key,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["steps"] = json.loads(item["steps"])
        item["can_retry"] = item.pop("latest_event")=="retry_exhausted" and item["state"] in ("BLOCKED","RECOVERY_REQUIRED")
        return item

    def create(self,key,text):
        with self.lock, self.db:
            existing = self.get(key)
            if existing:
                if existing["text"] != text:
                    raise Refused("同一任务 ID 已用于不同请求")
                return existing
            now = utc()
            self.db.execute("INSERT INTO tasks VALUES(?,?,'PLANNING','等待生成计划',NULL,'[]',0,0,0,?,?)",
                            (key,text,now,now))
            self.db.execute("INSERT INTO events(utc,task_id,step,kind,detail) VALUES(?,?,0,'accepted',?)",(now,key,text))
            return self.get(key)

    def due(self, planning):
        states = ("PLANNING","PLANNING") if planning else ("RUNNING","WAITING")
        with self.lock:
            row=self.db.execute("SELECT id FROM tasks WHERE state IN (?,?) AND due<=? ORDER BY due,created LIMIT 1",
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

    def save(self,t, state, detail, delay=0, kind=None, op=None, diagnostic=None):
        now=utc()
        with self.lock,self.db:
            self.db.execute("""UPDATE tasks SET state=?,detail=?,provider=?,steps=?,cursor=?,retries=?,due=?,updated=? WHERE id=?""",
                (state,detail,t["provider"],json.dumps(t["steps"]),t["cursor"],t["retries"],time.time()+delay,now,t["id"]))
            if kind:
                index = t["cursor"]-1 if kind=="verified" else t["cursor"]
                payload = {"provider":t["provider"],"state":state,"retries":t["retries"]}
                if diagnostic:
                    payload["diagnostic"]=diagnostic
                if kind=="planned":
                    payload["steps"]=t["steps"]
                elif op and 0 <= index < len(t["steps"]):
                    payload["step"]=t["steps"][index]
                self.db.execute("INSERT INTO events(utc,task_id,step,kind,detail,operation_id,payload) VALUES(?,?,?,?,?,?,?)",
                                (now,t["id"],index,kind,detail,op,json.dumps(payload,ensure_ascii=False)))

    def events(self,key):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE task_id=? ORDER BY seq",(key,))]


class Robot(Node):
    def __init__(self):
        super().__init__("edge_language_agent")
        self.submit = self.create_client(SubmitMission,"/edge/submit_mission")
        self.query = self.create_client(GetMission,"/edge/get_mission")
        self.sample = None
        self.create_subscription(Odometry,"/odom",self.receive_odom,qos_profile_sensor_data)

    def receive_odom(self,msg):
        stamp=msg.header.stamp
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

    def run(self,step,op,cap):
        if step["tool"] == "status" and not cap.get("endpoint"):
            sample=self.sample
            if not sample or time.monotonic()-sample[0]>2:
                raise Unavailable("等待新鲜里程计")
            msg=sample[1]
            p=msg.pose.pose.position
            q=msg.pose.pose.orientation
            return {"source":self.resolve_topic_name("/odom"),"frame":msg.header.frame_id,
                    "stamp_ns":msg.header.stamp.sec*1000000000+msg.header.stamp.nanosec,
                    "stamp":{"sec":msg.header.stamp.sec,"nanosec":msg.header.stamp.nanosec},"x":p.x,"y":p.y,
                    "yaw":math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z)),"speed":msg.twist.twist.linear.x}
        if step["tool"]=="inspect" and not cap.get("endpoint"):
            status=self.call(self.query,GetMission.Request(mission_id=op))
            if not status.found:
                pts=[Point(x=float(x),y=float(y)) for x,y in step["args"]["points"]]
                response=self.call(self.submit,SubmitMission.Request(mission_id=op,waypoints=pts))
                if not response.accepted:
                    if "occupied" in response.detail:
                        raise Unavailable(response.detail)
                    raise Refused(response.detail)
                return None
            if status.state in ("RUNNING","CANCELING"):
                return None
            if status.state=="RECOVERY_REQUIRED":
                raise OutcomeUnknown(status.detail)
            if status.state=="CANCELED":
                raise OperationCanceled(status.detail)
            if status.state!="SUCCEEDED":
                raise Refused(f"{status.state}: {status.detail}")
            return {"mission_id":op,"verified_count":status.verified_count,
                    "points":[[p.x,p.y] for p in status.evidence]}
        try:
            result=http_json(cap["endpoint"],{"operation_id":op,"arguments":step["args"],"version":step["version"]})
        except Refused as exc:
            if cap["effect"]!="read":
                raise OutcomeUnknown(str(exc)) from exc
            raise
        if not isinstance(result,dict) or result.get("operation_id")!=op:
            raise OutcomeUnknown("插件回执缺少原始 operation_id")
        if result.get("state")=="RUNNING":
            return None
        if result.get("state")=="CANCELED":
            raise OperationCanceled(str(result.get("detail","操作已取消")))
        if result.get("state") in ("FAILED","BLOCKED"):
            raise Refused(str(result.get("detail","插件未提供成功证据")))
        if result.get("state")!="SUCCEEDED" or not result.get("evidence"):
            raise OutcomeUnknown(str(result.get("detail","未收到可核对的插件执行证据")))
        for outcome, field in cap.get("outcomes",{}).items():
            evidence = result["evidence"]
            if not isinstance(evidence,dict) or field not in evidence or evidence[field] in (None,""):
                raise OutcomeUnknown(f"插件缺少 {outcome} 要求的证据字段 {field}")
        return result["evidence"]


class OperationCanceled(Exception):
    """The executor confirmed that its operation was canceled."""


def worker(journal,robot,planning):
    while rclpy.ok():
        t=journal.due(planning)
        if not t:
            time.sleep(0.2)
            continue
        op=f"{t['id']}/step/{t['cursor']+1}"
        try:
            caps=registry()
            if planning:
                t["provider"],t["steps"]=plan(t["text"],caps)
                t["retries"]=0
                journal.save(t,"RUNNING","已验证计划",kind="planned")
                continue
            step=t["steps"][t["cursor"]]
            cap=caps.get(step["tool"])
            if (not cap or cap["version"]!=step["version"] or
                    ("binding" in step and binding(cap)!=step["binding"]) or
                    ("binding" not in step and step["tool"] in ("inspect","status") and cap.get("endpoint"))):
                raise Unavailable("所需能力未挂载，或提供者与已保存的计划不一致")
            if step["status"]=="PENDING":
                step.update(status="RUNNING",operation_id=op,started_at=time.time())
                journal.save(t,"RUNNING","已持久化步骤意图",kind="dispatch",op=op)
            result=robot.run(step,op,cap)
            if result is None:
                if time.time()-step.get("resumed_at",step["started_at"])>float(os.getenv("EDGE_STEP_TIMEOUT","1800")):
                    journal.save(t,"RECOVERY_REQUIRED","等待原操作终态超时，请对账",kind="timeout",op=op)
                else:
                    journal.save(t,"WAITING","等待原操作回执",delay=.6)
                continue
            step.update(status="SUCCEEDED",evidence=result)
            t["cursor"]+=1
            t["retries"]=0
            last=t["cursor"]==len(t["steps"])
            journal.save(t,"SUCCEEDED" if last else "RUNNING","已取得步骤证据",kind="verified",op=op)
        except Unavailable as exc:
            diagnostic=getattr(exc,"diagnostic",None)
            if diagnostic:
                t["provider"]=diagnostic.get("provider")
            t["retries"]+=1
            exhausted=t["retries"]>int(os.getenv("EDGE_RETRY_LIMIT","7"))
            journal.save(t,("BLOCKED" if planning else "RECOVERY_REQUIRED") if exhausted else
                         ("PLANNING" if planning else "WAITING"),str(exc),delay=min(30,2**min(t["retries"]-1,5)),
                         kind="retry_exhausted" if exhausted else "retry",op=None if planning else op,diagnostic=diagnostic)
        except Refused as exc:
            diagnostic=getattr(exc,"diagnostic",None)
            if diagnostic:
                t["provider"]=diagnostic.get("provider")
            journal.save(t,"BLOCKED",str(exc),kind="blocked",op=None if planning else op,diagnostic=diagnostic)
        except OutcomeUnknown as exc:
            journal.save(t,"RECOVERY_REQUIRED",str(exc),kind="error",op=op)
        except OperationCanceled as exc:
            journal.save(t,"CANCELED",str(exc),kind="canceled",op=op)
        except Exception as exc:
            journal.save(t,"RECOVERY_REQUIRED",f"{type(exc).__name__}: {exc}",kind="error",op=op)


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
        if path.path=="/agent/tasks":
            key=parse_qs(path.query).get("id",[""])[0]
            task=self.server.journal.get(key)
            if task:
                task["events"]=self.server.journal.events(key)
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
            key=request.get("id") if self.path=="/agent/retry" else request.get("id",uuid.uuid4().hex)
            text=request.get("text")
            if not isinstance(key,str) or not re.fullmatch(r"[\w.-]{1,96}",key):
                raise ValueError("ID 格式无效")
            if self.path=="/agent/retry":
                return self.send(202,self.server.journal.retry(key))
            if not isinstance(text,str) or not 1<=len(text.strip())<=4096:
                raise ValueError("任务文本长度应为 1–4096 字符")
            self.send(202,self.server.journal.create(key,text.strip()))
        except (TypeError,ValueError) as exc:
            self.send(400,{"detail":str(exc)})
        except Refused as exc:
            self.send(409,{"detail":str(exc)})
        except KeyError:
            self.send(404,{"detail":"未找到记录"})


if __name__=="__main__":
    journal=Journal(Path(os.getenv("EDGE_TASK_DIR","/var/lib/edge-tasks")))
    rclpy.init()
    robot=Robot()
    threading.Thread(target=rclpy.spin,args=(robot,),daemon=True).start()
    for planning in (True,False):
        threading.Thread(target=worker,args=(journal,robot,planning),daemon=True).start()
    server=ThreadingHTTPServer((os.getenv("EDGE_AGENT_BIND","0.0.0.0"),8090),Handler)
    server.journal=journal
    server.serve_forever()
