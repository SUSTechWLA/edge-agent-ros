"""Persistent natural-language task supervisor. The ROS executive owns physical actions."""
import json
import math
import os
import re
import threading
import time
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
import soft_client
from planning import (OutcomeUnknown, Refused, Unavailable, binding, catalog, is_series,
                      failure, http_json, plan, plan_read_only, registry, remote_failure, verification_plan)
from tracing import artifact, clean_text, diagnosis, exchange, sanitized
from kernel import host, verification
from kernel.authorization import motion_grant
from kernel.catalog import plugin_view
from kernel.ledger import Journal, project_step, trigger_input, utc
from kernel.verification import finish_step
from kernel.worker import event_diagnosis, task_plan as run_task_plan, worker as run_worker
from edge_contracts.errors import OperationCanceled


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
        if "derived" in cap and not cap.get("endpoint") and "soft_pin" not in step:
            if is_series(cap):
                try:
                    evidence = evaluate(cap["derived"], step["args"])
                except ValueError as exc:
                    raise failure(Refused, str(exc), "tool.series_invalid", "arguments.series") from exc
                return {**evidence, "definition_version": cap["version"]}
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
        return host.invoke(step, op, cap, trace, context, http_json)


def task_plan(journal, task, trace):
    return run_task_plan(journal, task, trace, registry=registry, planner=plan,
                         read_only_planner=plan_read_only, freeze=host.freeze)


def worker(journal, robot, planning, verifying=False):
    return run_worker(journal, robot, planning, verifying, keep_running=rclpy.ok,
                      plan_task=task_plan, registry=registry, services=runtime_plugins)


def prepare_verification(step, cap):
    return verification.prepare_verification(step, cap, runtime_plugins)


def verify_candidate(journal, task, step, op, trace):
    return verification.verify_candidate(journal, task, step, op, trace, runtime_plugins)


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
            try:
                capabilities = catalog(registry())
            except Unavailable as exc:
                return self.send(503, {"code":exc.code,"origin":exc.origin,"detail":str(exc)})
            return self.send(200,{"capabilities":capabilities})
        if path.path=="/agent/plugins":
            try:
                directory = self.server.registry_observer.observe()
            except runtime_plugins.PluginUnavailable as exc:
                return self.send(503, {"code":exc.code,"detail":str(exc)})
            if parse_qs(path.query).get("schema") == ["2"]:
                try:
                    return self.send(200, plugin_view(directory))
                except soft_client.SoftError as exc:
                    return self.send(503, {"code":exc.code, "detail":str(exc)})
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
                resolved = host.resolve_service(name, expected)
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
                prior=self.server.journal.get(key)
                if prior and prior.get("motion_resource"):
                    motion_grant(prior["motion_resource"], self.headers.get("Authorization"))
                return self.send(202,self.server.journal.retry(key))
            if not isinstance(text,str) or not 1<=len(text.strip())<=4096:
                raise ValueError("任务文本长度应为 1–4096 字符")
            grant=motion_grant(request.get("motion_resource"), self.headers.get("Authorization"))
            if grant and request.get("trigger") is not None:
                raise PermissionError("运动授权仅适用于操作员直接提交的任务")
            self.send(202,self.server.journal.create(key,text.strip(),request.get("trigger"),motion_resource=grant))
        except PermissionError as exc:
            self.send(403,{"detail":str(exc)})
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
    if host.enabled():
        threading.Thread(target=journal.settle_soft,args=(stop,),daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        monitor.join(timeout=2)
