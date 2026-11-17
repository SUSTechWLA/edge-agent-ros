"""Named-place capability with a durable binding to the original waypoint plan."""
import json
import math
import os
import sqlite3
import threading
import time
import uuid
import re
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import rclpy
from edge_interfaces.srv import GetMission, SubmitMission
from geometry_msgs.msg import Point
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rosgraph_msgs.msg import Clock
from std_msgs.msg import String
from layout import Layout, RouteError


class Pending(Exception):
    pass


def core_code(detail, fallback):
    match = re.search(r"\[code:([\w.]+)\]\s*$", detail)
    return match.group(1) if match else fallback


def unknown(detail, code, plan):
    return {"state":"RECOVERY_REQUIRED","detail":detail,
            "error":{"code":code,"message":detail,"details":{"route":plan}}}


def trace_context(ctx):
    if ctx is None:
        return {"trace_id":"standalone-"+uuid.uuid4().hex}
    if not isinstance(ctx,dict):
        raise ValueError("invalid trace")
    result={}
    for key in ("task_id","trace_id","parent_event_id"):
        value=ctx.get(key)
        if value is None and key=="parent_event_id":
            continue
        if not isinstance(value,str) or not re.fullmatch(r"[\w./:@-]{1,240}",value):
            raise ValueError("invalid trace identity")
        result[key]=value
    trigger=ctx.get("trigger",{})
    if trigger:
        if not isinstance(trigger,dict) or not all(isinstance(trigger.get(k),str) and
              re.fullmatch(r"[\w./:@-]{1,240}",trigger[k]) for k in ("id","kind","source")):
            raise ValueError("invalid trigger")
        result["trigger"]={k:trigger[k] for k in ("id","kind","source")}
    return result


class Scene(Node):
    def __init__(self, layout, directory):
        super().__init__("edge_semantic_scene")
        self.layout = layout
        self.position = None
        self.sim_time_ns = None
        self.create_subscription(Clock,"/clock",self.observe_clock,qos_profile_sensor_data)
        self.create_subscription(Odometry,"/edge/perception/pose",self.observe,qos_profile_sensor_data)
        self.publisher = self.create_publisher(String,"/edge/semantic_map",1)
        self.create_timer(0.8,self.publish)
        self.get_mission = self.create_client(GetMission,"/edge/get_mission")
        self.submit_mission = self.create_client(SubmitMission,"/edge/submit_mission")
        directory.mkdir(parents=True,exist_ok=True)
        self.db_path=directory/"routes.db"
        self.db = sqlite3.connect(self.db_path,check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS routes
            (op TEXT PRIMARY KEY, place TEXT NOT NULL, plan TEXT NOT NULL)""")
        if "binding" not in {r[1] for r in self.db.execute("PRAGMA table_info(routes)")}:
            self.db.execute("ALTER TABLE routes ADD COLUMN binding TEXT NOT NULL DEFAULT 'legacy'")
        self.lock = threading.Lock()
        self.db.executescript("""CREATE TABLE IF NOT EXISTS decisions(
            op TEXT PRIMARY KEY, place TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
            refused INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, last_attempt REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS decisions_pending ON decisions(delivered,last_attempt);""")
        self.events=self.create_publisher(String,"/edge/events",100)
        self.archive=os.getenv("EDGE_EVENT_ARCHIVE_URL","http://console:8080").rstrip("/")
        self.stop_delivery=threading.Event()
        threading.Thread(target=self.deliver,daemon=True).start()

    def record(self,op,place,ctx,position,plan=None,error=None):
        refused=error is not None
        data={"place":place,"start_xy":position,"scene":self.layout.data["id"],
              "revision":self.layout.data["revision"],"digest":self.layout.digest}
        if plan:
            data.update(path=plan["path"],waypoints=plan["waypoints"],frame=plan["frame"])
        event={"schema_version":1,"event_id":"scene-"+uuid.uuid4().hex,"utc":datetime.now(timezone.utc).isoformat(),
               **ctx,"mission_id":op,"operation_id":op,"module":"scene.navigation","layer":"navigation",
               "kind":"scene.route_refused" if refused else "scene.route_planned",
               "code":error.code if refused else "scene.route_planned","level":"warn" if refused else "info",
               "detail":str(error) if refused else "已按场景通行图确定路线","data":data}
        self.db.execute("INSERT INTO decisions(op,place,event_id,body,refused) VALUES(?,?,?,?,?)",
                        (op,place,event["event_id"],json.dumps(event,ensure_ascii=False),int(refused)))

    def deliver(self):
        # A successful ROS publish alone cannot acknowledge a durable archive.
        # Keep the exact payload until the console's read API returns the source ID.
        with closing(sqlite3.connect(self.db_path)) as db:
            while not self.stop_delivery.wait(.35) and rclpy.ok():
                try:
                    row=db.execute("""SELECT event_id,body FROM decisions WHERE delivered=0 AND last_attempt<?
                                      ORDER BY last_attempt,rowid LIMIT 1""",(time.time()-2,)).fetchone()
                    if not row:
                        continue
                    eid,body=row
                    with db:
                        db.execute("UPDATE decisions SET last_attempt=? WHERE event_id=?",(time.time(),eid))
                    self.events.publish(String(data=body))
                    query=urlencode({"event_id":eid,"limit":1})
                    with urlopen(self.archive+"/api/events?"+query,timeout=1.5) as response:
                        events=json.load(response)["events"]
                    if any(e.get("event_id")==eid and e.get("source")=="ros:/edge/events" for e in events):
                        with db:
                            db.execute("UPDATE decisions SET delivered=1 WHERE event_id=?",(eid,))
                except (OSError,ValueError,KeyError,sqlite3.Error):
                    # Durable row remains pending; execution and ROS callbacks stay independent.
                    continue

    def observe_clock(self,msg):
        now_ns = msg.clock.sec * 1_000_000_000 + msg.clock.nanosec
        if self.sim_time_ns is not None and now_ns < self.sim_time_ns:
            self.position = None
        self.sim_time_ns = now_ns

    def observe(self,msg):
        if msg.header.frame_id == self.layout.data["frame"]:
            p=msg.pose.pose.position
            stamp=msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
            if not all(map(math.isfinite, (p.x, p.y))) or not self.current_stamp(stamp):
                return
            if not self.position or stamp>self.position[2]:
                self.position=((p.x,p.y),time.monotonic(),stamp)

    def current_stamp(self, stamp):
        return self.sim_time_ns is None or abs(self.sim_time_ns - stamp) <= 1_500_000_000

    def sample(self):
        p = self.position
        return p[0] if p and time.monotonic()-p[1]<1.5 and self.current_stamp(p[2]) else None

    def describe(self):
        pos=self.sample()
        return {**self.layout.data,"digest":self.layout.digest,
                "current_zone":self.layout.zone(pos) if pos else None}

    def preview(self, place):
        position = self.sample()
        if position is None:
            raise Pending("等待地图坐标系中的新鲜定位")
        return self.layout.plan(position, place)

    def publish(self):
        self.publisher.publish(String(data=json.dumps(self.describe(),ensure_ascii=False)))

    def call(self,client,request):
        if not client.wait_for_service(timeout_sec=.4):
            raise Pending("ROS 任务内核暂不可用")
        future=client.call_async(request)
        done=threading.Event()
        future.add_done_callback(lambda _:done.set())
        if not done.wait(2):
            client.remove_pending_request(future)
            raise Pending("ROS 回执超时，将按原操作 ID 对账")
        return future.result()

    def bind(self, op, plan):
        if not self.submit_mission.wait_for_service(timeout_sec=.4):
            raise Pending("等待 ROS 提交服务")
        req = SubmitMission.Request(mission_id=op, waypoints=[
            Point(x=float(x), y=float(y)) for x,y in plan["waypoints"]])
        # The kernel compares every waypoint even when this ID already exists.
        # Persist send intent before the RPC so a lost receipt can be reconciled.
        with self.db:
            self.db.execute("UPDATE routes SET binding='sent' WHERE op=?", (op,))
        result = self.call(self.submit_mission, req)
        if not result.accepted:
            code = core_code(result.detail, "scene.dispatch_rejected")
            with self.db:
                self.db.execute("UPDATE routes SET binding='prepared' WHERE op=?", (op,))
            if code == "core.base_occupied" or "base occupied" in result.detail:
                raise Pending(result.detail)
            raise RouteError(code, result.detail)
        with self.db:
            self.db.execute("UPDATE routes SET binding='bound' WHERE op=?", (op,))

    def navigate(self,op,place,ctx):
        with self.lock:
            refusal=self.db.execute("SELECT place,body FROM decisions WHERE op=? AND refused=1",(op,)).fetchone()
            if refusal:
                if refusal[0]!=place:
                    raise RouteError("scene.operation_conflict","操作 ID 已绑定其他目的地")
                event=json.loads(refusal[1])
                raise RouteError(event["code"],event["detail"])
            row=self.db.execute("SELECT place,plan,binding FROM routes WHERE op=?",(op,)).fetchone()
            if row:
                if row[0]!=place:
                    raise RouteError("scene.operation_conflict","操作 ID 已绑定其他目的地")
                plan=json.loads(row[1])
                binding=row[2]
            else:
                pos=self.sample()
                if pos is None:
                    raise Pending("等待地图坐标系中的新鲜定位")
                try:
                    plan=self.layout.plan(pos,place)
                except RouteError as error:
                    with self.db:
                        self.record(op,place,ctx,pos,error=error)
                    raise
                with self.db:
                    self.db.execute("INSERT INTO routes(op,place,plan,binding) VALUES (?,?,?,'prepared')",
                                    (op,place,json.dumps(plan)))
                    self.record(op,place,ctx,pos,plan=plan)
                binding="prepared"
            status=self.call(self.get_mission,GetMission.Request(mission_id=op))
            if not status.found and binding != "prepared":
                return unknown("原委托可能已执行，但内核查不到记录，请先核实底盘状态",
                               "scene.mission_unaccounted", plan)
            if binding != "bound":
                self.bind(op, plan)
            if not status.found:
                return {"state":"RUNNING","route":plan}
            if status.state=="SUCCEEDED":
                points = plan["waypoints"]
                if (status.verified_count != len(points) or len(status.evidence) != len(points)
                    or not all(math.dist((pt.x, pt.y), target) <= .12
                               for pt, target in zip(status.evidence, points))):
                    return unknown("运动回执与原路线的逐点验收不一致", "scene.evidence_mismatch", plan)
                pt=status.evidence[-1]
                return {"state":"SUCCEEDED","evidence":{**plan,"place":place,"observed_xy":[pt.x,pt.y],
                        "verified_count":status.verified_count,"mission_id":op}}
            if status.state in ("RUNNING","CANCELING"):
                return {"state":"RUNNING","route":plan}
            return {"state":status.state,"detail":status.detail,
                    "error":{"code":core_code(status.detail,"scene.motion_incomplete"),"message":status.detail,"details":{"route":plan}}}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        op=""
        status=200
        try:
            size=int(self.headers.get("Content-Length",0))
            if not 0<size<=16384:
                raise ValueError("invalid length")
            body=json.loads(self.rfile.read(size))
            op=body["operation_id"]
            if not isinstance(op,str) or not re.fullmatch(r"[\w./:@-]{1,240}",op) or body.get("version")!="1":
                raise ValueError("invalid operation or version")
            if self.server.scene is None and self.path in ("/describe","/navigate","/preview"):
                result={"state":"BLOCKED","error":{"code":"scene.disabled","message":"当前仿真未启用语义场景"}}
            elif self.path=="/describe":
                result={"state":"SUCCEEDED","evidence":self.server.scene.describe()}
            elif self.path in ("/navigate","/preview"):
                place=body["arguments"]["place"]
                if not isinstance(place,str) or not 0<len(place)<=120:
                    raise ValueError("invalid place")
                if self.path=="/preview":
                    result={"state":"SUCCEEDED","evidence":self.server.scene.preview(place)}
                else:
                    result=self.server.scene.navigate(op,place,trace_context(body.get("trace")))
            else:
                status,result=404,{"detail":"unknown capability"}
        except RouteError as exc:
            result={"state":"BLOCKED","detail":str(exc),"error":{"code":exc.code,"message":str(exc)}}
        except Pending as exc:
            status,result=503,{"detail":str(exc)}
        except (ValueError,TypeError,KeyError) as exc:
            status,result=400,{"detail":str(exc)}
        data=json.dumps({"operation_id":op,**result},ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__=="__main__":
    scene_file=os.environ.get("EDGE_SCENE_FILE","/opt/edge/scene/campus.json")
    scene=None
    if scene_file:
        rclpy.init()
        scene=Scene(Layout(scene_file),Path("/var/lib/edge-scene"))
        threading.Thread(target=rclpy.spin,args=(scene,),daemon=True).start()
    server=ThreadingHTTPServer(("0.0.0.0",8092),Handler)
    server.scene=scene
    server.serve_forever()
