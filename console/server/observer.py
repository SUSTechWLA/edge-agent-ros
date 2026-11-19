"""Single ROS executor owns sensor ingestion. HTTP threads only observe snapshots."""
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

from geometry_msgs.msg import Twist
from nav_msgs.msg import OccupancyGrid, Odometry
from rcl_interfaces.msg import Log
from rosgraph_msgs.msg import Clock
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Imu, LaserScan
from std_msgs.msg import String

from history import EventConflict, History
from legacy_ledger import LegacyLedgerReader
from task_data import AgentFacts, FactsUnavailable
from journal_ingress import JournalIngress
from module_events import parse_event
from observations import Observations


TOPICS = {
    "scan": ("/scan", LaserScan, "sensor_msgs/LaserScan", 1.5),
    "imu": ("/imu", Imu, "sensor_msgs/Imu", 1.0),
    "odom": ("/odom", Odometry, "nav_msgs/Odometry", 1.0),
    "pose": ("/edge/perception/pose", Odometry, "nav_msgs/Odometry", 1.5),
    "map": ("/edge/perception/map", OccupancyGrid, "nav_msgs/OccupancyGrid", 5.0),
    "perception": ("/edge/perception/status", String, "std_msgs/String", 5.0),
    "semantics": ("/edge/semantic_map", String, "std_msgs/String", 3.0),
    "truth": ("/ground_truth", Odometry, "nav_msgs/Odometry", 1.5),
    "cmd": ("/cmd_vel", Twist, "geometry_msgs/Twist", 1.0),
    "operation": ("/edge/active_operation", String, "std_msgs/String", 1.5),
}


def num(v):
    return v if math.isfinite(v) else None


def vector(v):
    return [num(v.x), num(v.y), num(v.z)]


def quat(q):
    return vector(q) + [num(q.w)]


def yaw(q):
    if not all(math.isfinite(n) for n in (q.x,q.y,q.z,q.w)):
        return None
    return math.atan2(2*(q.w*q.z+q.x*q.y), 1-2*(q.y*q.y+q.z*q.z))


def stamp_s(stamp):
    return stamp.sec + stamp.nanosec * 1e-9


def legacy_payload(raw):
    # Some old providers persisted JSON extensions (NaN) or exponents outside
    # float64. Keep the original numeric lexeme so the source row is auditable.
    converted = set()
    def number(literal):
        value = float(literal)
        if math.isfinite(value):
            return value
        converted.add(literal)
        return literal
    payload = json.loads(raw, parse_float=number, parse_constant=number)
    return payload, sorted(converted)


class Observer(Node):
    def __init__(self):
        super().__init__("edge_console")
        self.lock = threading.RLock()
        self.session = uuid.uuid4().hex
        self.environment = os.environ.get("EDGE_ENVIRONMENT") or os.environ.get("EDGE_MODE","simulation")
        self.robot_id = os.environ.get("EDGE_ROBOT_ID","edge-rover")
        default_mount = '{"x":0.08,"y":0,"z":0.2,"yaw":0}' if self.environment == "simulation" else "null"
        self.mount = json.loads(os.environ.get("EDGE_SCAN_MOUNT", default_mount))
        self.scan_frame = os.environ.get("EDGE_SCAN_FRAME", "laser_link")
        self.base_frame = os.environ.get("EDGE_BASE_FRAME", "base_link")
        self.history = History(Path(os.environ.get("EDGE_CONSOLE_DIR","/var/lib/edge-console")) / "history.db")
        self.ledger = LegacyLedgerReader()
        self.facts = AgentFacts()
        self.journals = JournalIngress.from_deployment(self.facts)
        self.sources = {}
        self.health = {}
        self.health_event = {}
        self.trail = deque(maxlen=600)
        self.historical = deque(maxlen=24)
        self.mission_data = {}
        self.source_id = None
        self.ledger_error = None
        self.task_error = None
        self.task_source_id = None
        self.last_baseline = 0
        self.moving = None
        self.mode = None
        self.sim_clock = None
        self.extra = Observations(self, os.environ.get("EDGE_OBSERVATIONS_DIR","/etc/edge/observations"))
        previous = self.history.replay(self.history.cursor) if self.history.cursor else None
        if previous:
            self.source_id = previous.get("ledger_id")
            self.mission_data = {m["mission_id"]:m for m in previous["missions"]}
        for key, (topic, cls, _, _) in TOPICS.items():
            if key == "truth" and self.environment != "simulation":
                continue
            self.create_subscription(cls, topic, lambda msg, k=key: self.consume(k, msg), qos_profile_sensor_data)
        self.create_subscription(Log, "/rosout", self.ros_log, qos_profile_sensor_data)
        self.create_subscription(String,"/edge/events",self.module_event,100)
        self.last_wire_warning = {}
        if self.environment=="simulation":
            self.create_subscription(Clock, "/clock",self.clock_update,
                                     qos_profile_sensor_data)
        self.create_timer(0.25, self.tick)
        self.event("console.started", "edge_console", "开始采集 ROS 状态",
                   data={"robot_id":self.robot_id, "environment":self.environment,
                         "source_revision":os.environ.get("EDGE_REVISION","unknown"),
                         "ros_distro":os.environ.get("ROS_DISTRO"),"rmw":os.environ.get("RMW_IMPLEMENTATION"),
                         "ros_domain_id":os.environ.get("ROS_DOMAIN_ID"),"schema_version":1})

    def clock_update(self, msg):
        now = stamp_s(msg.clock)
        with self.lock:
            previous = self.sim_clock
            self.sim_clock = now
            if previous is None or now >= previous:
                return
            self.sources.clear()
            self.health.clear()
            self.health_event.clear()
            self.trail.clear()
            self.historical.clear()
            self.moving = self.mode = None
            self.extra.reset_samples()
            self.event("sensor.clock_reset", "/clock", "仿真时钟回退，等待新时间段的观测", "warn",
                       data={"previous_stamp_s":previous, "stamp_s":now})

    def future_sample(self, stamp):
        return self.sim_clock is not None and stamp is not None and stamp > self.sim_clock + 1.0

    def data_for(self, key, msg):
        if key in ("pose","odom","truth"):
            p = msg.pose.pose
            t = msg.twist.twist
            return {"x":num(p.position.x),"y":num(p.position.y),"z":num(p.position.z),"yaw":yaw(p.orientation),
                    "v":num(t.linear.x),"w":num(t.angular.z),"frame":msg.header.frame_id,
                    "child_frame":msg.child_frame_id, "quaternion":quat(p.orientation),
                    "linear_velocity":vector(t.linear), "angular_velocity":vector(t.angular),
                    "pose_covariance":[num(v) for v in msg.pose.covariance]}
        if key == "imu":
            return {"frame":msg.header.frame_id,"yaw":yaw(msg.orientation) if msg.orientation_covariance[0]!=-1 else None,
                    "quaternion":quat(msg.orientation),"angular_velocity":vector(msg.angular_velocity),
                    "linear_acceleration":vector(msg.linear_acceleration),"angular_z":num(msg.angular_velocity.z),
                    "linear_x":num(msg.linear_acceleration.x),"linear_y":num(msg.linear_acceleration.y),
                    "orientation_covariance":[num(v) for v in msg.orientation_covariance],
                    "angular_velocity_covariance":[num(v) for v in msg.angular_velocity_covariance],
                    "linear_acceleration_covariance":[num(v) for v in msg.linear_acceleration_covariance]}
        if key == "scan":
            ranges = [num(x) for x in msg.ranges]
            invalid_indices = {str(i):("NaN" if math.isnan(v) else "-Infinity")
                               for i,v in enumerate(msg.ranges) if math.isnan(v) or v==float("-inf")}
            return {"frame":msg.header.frame_id, "mounted_frame":self.base_frame,
                    "mount":self.mount if msg.header.frame_id==self.scan_frame else None,
                    "range_min":num(msg.range_min), "range_max":num(msg.range_max),
                    "angle_min":num(msg.angle_min), "angle_max":num(msg.angle_max),
                    "angle_increment":num(msg.angle_increment),
                    "time_increment":num(msg.time_increment),"scan_time":num(msg.scan_time),
                    "ranges":ranges, "intensities":[num(v) for v in msg.intensities],
                    # JSON null with no exception denotes +Infinity (no return).
                    "invalid_indices":invalid_indices,
                    "no_return":sum(math.isinf(x) and x>0 for x in msg.ranges),
                    "invalid":len(invalid_indices)}
        if key == "map":
            info = msg.info
            return {"frame":msg.header.frame_id,"width":info.width,"height":info.height,
                    "resolution":num(info.resolution),"origin_x":num(info.origin.position.x),
                    "origin_y":num(info.origin.position.y),"origin_yaw":yaw(info.origin.orientation),
                    "cells":list(msg.data)}
        if key == "cmd":
            return {"v":num(msg.linear.x),"w":num(msg.angular.z)}
        if key == "operation":
            return {"id":msg.data}
        if key=="semantics" and len(msg.data)>100000:
            raise ValueError("semantic map exceeds preview limit")
        result = json.loads(msg.data)
        # Require numbers to be representable in snapshots, including 1e999.
        json.dumps(result, allow_nan=False)
        if not isinstance(result,dict):
            raise ValueError("expected a status object")
        if key=="semantics" and (result.get("schema_version")!=1 or not isinstance(result.get("frame"),str)
                or not all(isinstance(result.get(k),list) for k in ("zones","features","nodes","edges"))):
            raise ValueError("invalid semantic map")
        return result

    def consume(self, key, msg):
        try:
            data = self.data_for(key,msg)
        except (ValueError, TypeError, RecursionError, OverflowError):
            now = time.monotonic()
            if now-self.last_wire_warning.get(key,float("-inf"))>=10:
                self.last_wire_warning[key]=now
                self.event("source.invalid",self.resolve_topic_name(TOPICS[key][0]),
                           "已丢弃无效的观测消息","warn",data={"key":key})
            return
        stamp = stamp_s(msg.header.stamp) if hasattr(msg,"header") else None
        now = time.monotonic()
        with self.lock:
            if self.future_sample(stamp):
                return
            prev = self.sources.get(key)
            rate = prev["rate"] if prev else None
            if prev and now > prev["recv"]:
                interval = now-prev["recv"]
                estimated = (1/rate)*0.9 + interval*0.1 if rate and interval < 3 else interval
                rate = 1/max(estimated,0.001)
            if prev and stamp is not None and prev["stamp"] is not None and stamp < prev["stamp"]:
                self.event("sensor.clock_regressed", TOPICS[key][0],"采样时钟回退","warn",
                           data={"previous_stamp_s":prev["stamp"], "stamp_s":stamp})
                if self.sim_clock is not None:
                    return
                if key == "pose":
                    self.trail.clear()
            sample_recv = prev["sample_recv"] if prev and stamp is not None and prev["stamp"]==stamp else now
            self.sources[key] = {"data":data,"recv":now,"sample_recv":sample_recv,"wall":time.time(),"stamp":stamp,"rate":rate}
            if key == "pose" and all(data[k] is not None for k in ("x","y")):
                last = self.trail[-1] if self.trail else None
                if prev and data["frame"] != prev["data"]["frame"]:
                    self.trail.clear()
                    last = None
                if last is None or math.hypot(data["x"]-last["x"], data["y"]-last["y"])>=0.02:
                    self.trail.append({"x":data["x"],"y":data["y"],"stamp_s":stamp})
            if key == "operation" and (not prev or data != prev["data"]):
                op = data["id"] or None
                self.event("operation.changed",TOPICS[key][0],"运动许可更新" if op else "运动许可撤回",
                           operation_id=op,mission_id=self.mission_for(op),data=data)

    def mission_for(self, op):
        if not op:
            return None
        for m in self.mission_data.values():
            if any(s.get("operation_id")==op for s in m["steps"]):
                return m["mission_id"]
        return op.rsplit("/waypoint/",1)[0] if "/waypoint/" in op else None

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            src = {}
            for key,(topic,_,name,ttl) in TOPICS.items():
                item = self.sources.get(key)
                receive_age = now-item["recv"] if item is not None else None
                age = now-item["sample_recv"] if item is not None else None
                source_age = None
                if item and item["stamp"] is not None and self.sim_clock is not None:
                    source_age = max(0,self.sim_clock-item["stamp"])
                    age=max(age,source_age)
                src[key] = {"topic":self.resolve_topic_name(topic),"type":name,"available":item is not None,
                            "age_s":round(age,4) if age is not None else None,
                            "receive_age_s":round(receive_age,4) if receive_age is not None else None,
                            "source_age_s":round(source_age,4) if source_age is not None else None,
                            "fresh":age is not None and age <= ttl, "ttl_s":ttl,
                            "received_at":item["wall"] if item else None,
                            "stamp_s":item["stamp"] if item else None,
                            "rate_hz":round(item["rate"],2) if item and item["rate"] is not None else None,
                            "data":item["data"] if item else None}
            op = src["operation"]
            return {"mode":"live","now":time.time(),"session_id":self.session,"cursor":self.history.cursor,
                    "earliest_cursor":self.history.earliest,"environment":self.environment,
                    "robot_id":self.robot_id,"sim_world_aligned":self.environment=="simulation",
                    "ledger_id":self.source_id,"sources":src,"extra_sources":self.extra.snapshot(now),
                    "missions":list(self.mission_data.values())[:64],
                    "trail":list(self.trail),"trail_frame":(src["pose"]["data"] or {}).get("frame"),
                    "active_operation":(op["data"]["id"] or None) if op["fresh"] else None}

    def event(self,kind,source,detail,level="info",**kwargs):
        return self.history.record(self.snapshot(),kind=kind,source=source,detail=detail,level=level,**kwargs)

    def ros_log(self, msg):
        if msg.level < 30 and msg.name not in ("edge_core","edge_gateway","edge_perception"):
            return
        if msg.name == "edge_console":
            return
        level = "error" if msg.level >= 40 else "warn" if msg.level >= 30 else "info"
        # Only attach an operation when its identifier is in this exact ROS record.
        with self.lock:
            op = next((s["operation_id"] for m in self.mission_data.values() for s in m["steps"]
                       if s.get("operation_id") and s["operation_id"] in msg.msg),None)
            self.event("ros.log",msg.name,msg.msg,level,mission_id=self.mission_for(op),operation_id=op,
                       data={"file":msg.file,"line":msg.line,"function":msg.function,
                             "stamp_s":stamp_s(msg.stamp),"severity":int(msg.level)})

    def module_event(self, msg):
        try:
            event,digest = parse_event(msg.data)
            with self.lock:
                frame = self.observed_frame(event["utc"]) if event.get("utc") else self.snapshot()
                frame["module_event"] = event
                # source records the actual transport, module is the producer's claim.
                self.history.record(frame,kind=event["kind"],source="ros:/edge/events",
                                    level=event.get("level","info"),detail=event.get("detail",""),
                                    mission_id=event.get("mission_id"),operation_id=event.get("operation_id"),
                                    data=event.get("data",{}),event_utc=event.get("utc"),
                                    trace=event,ingress_hash=digest)
        except (ValueError,OverflowError,RecursionError) as exc:
            code = "module.event_conflict" if isinstance(exc,EventConflict) else "module.event_invalid"
            now = time.monotonic()
            if now-self.last_wire_warning.get(code,float("-inf"))>=10:
                self.last_wire_warning[code]=now
                # Never copy arbitrary malformed wire text (possibly secrets) to the archive.
                self.event(code,"edge_console","模块事件 ID 冲突" if isinstance(exc,EventConflict) else "模块事件格式无效",
                           "warn",data={"reason":str(exc)[:180],"topic":"/edge/events"})

    def ingest_event(self, row, targets):
        mid,op,kind = row["mission_id"],row["operation_id"],row["kind"]
        old = self.mission_data.get(mid)
        if not old or kind=="mission.created":
            steps = [{**s,"operation_id":None,"action_result":None,"evidence_x":None,"evidence_y":None,
                      "odom_stamp_ns":None} for s in targets]
            current = {"mission_id":mid,"state":row["prior_state"],"detail":"","steps":steps}
        else:
            current = {**old,"steps":[s.copy() for s in old["steps"]]}
        data = {"source_seq":row["seq"],"source_cause_seq":row["cause_seq"],
                "prior_state":row["prior_state"],"state":row["state"]}
        if kind.startswith("mission."):
            current.update(state=row["state"],detail=row["detail"])
        elif kind.startswith("step."):
            if kind in ("step.dispatch","step.verified"):
                data.update(json.loads(row["detail"]))
            for s in current["steps"]:
                if s["step"]==data.get("step") or (op and s["operation_id"]==op):
                    if kind=="step.dispatch":
                        s["operation_id"]=op
                    elif kind=="step.result":
                        s["action_result"]=row["detail"]
                    elif kind=="step.verified":
                        s.update(evidence_x=data["observed_x"],evidence_y=data["observed_y"],
                                 odom_stamp_ns=data["odom_stamp_ns"])
                    break
        self.mission_data = {mid:current, **{k:v for k,v in self.mission_data.items() if k!=mid}}
        while len(self.mission_data)>64:
            self.mission_data.pop(next(reversed(self.mission_data)))
        snapshot = self.at_source_time(row["utc"])
        level = "error" if row["state"] in ("FAILED","RECOVERY_REQUIRED") else "info"
        parent = self.history.dispatch_origin(self.task_source_id,mid)
        if parent and kind=="mission.created":
            data["parent_task_id"]=parent["mission_id"]
            data["agent_dispatch_seq"]=parent["seq"]
        self.history.record(snapshot,kind=kind,source="edge_core/ledger",detail=row["detail"],level=level,
                            mission_id=mid,operation_id=op,data=data,event_utc=row["utc"],
                            source_id=self.source_id,source_seq=row["seq"],source_cause=row["cause_seq"],
                            cause=parent["seq"] if parent and kind=="mission.created" else None,
                            trace={"module":"edge.core","layer":"core","code":row.get("code") or kind,
                                   "trace_id":parent["trace_id"] if parent else mid,
                                   "task_id":parent["mission_id"] if parent else None,
                                   "trigger":{k:parent["trigger_"+k] for k in ("kind","id","source")} if parent else {}})

    def at_source_time(self, event_utc):
        when = datetime.fromisoformat(event_utc.replace("Z","+00:00")).timestamp()
        prior = next((s for s in reversed(self.historical) if s["now"]<=when), None)
        snapshot = self.snapshot()
        snapshot["event_at"] = when
        snapshot["now"] = when
        if prior and when-prior["now"] <= 2:
            snapshot.update(sources=prior["sources"], extra_sources=prior.get("extra_sources",{}),
                            trail=prior["trail"], trail_frame=prior["trail_frame"],
                            active_operation=prior["active_operation"],telemetry_at=prior["now"])
        else:
            for s in snapshot["sources"].values():
                s.update(data=None, fresh=False, available=False, age_s=None, received_at=None,stamp_s=None,
                         rate_hz=None, source_age_s=None, receive_age_s=None)
            snapshot.update(extra_sources={},trail=[],trail_frame=None,active_operation=None,telemetry_at=None)
        return snapshot

    def poll_tasks(self):
        try:
            page = self.facts.event_batch("task-events", self.history)
        except FactsUnavailable as exc:
            if self.task_error != str(exc):
                self.task_error=str(exc)
                self.event("agent.journal_unavailable","mission_agent/tasks",str(exc),"warn")
            return
        if self.task_error:
            self.task_error=None
            self.event("agent.journal_available","mission_agent/tasks","任务日志已连接")
        source_id = page["source_id"]
        self.task_source_id=source_id
        for row in page["events"]:
            kind=row["kind"]
            data={"source_seq":row["seq"],"task_id":row["task_id"],"journal_step":row["step"],
                  "delegated_operation_id":row["operation_id"]}
            if row.get("payload"):
                try:
                    data["payload"], converted = legacy_payload(row["payload"])
                    if converted:
                        data["legacy_nonfinite_literals"] = converted
                except (TypeError,ValueError):
                    data["payload"]=row["payload"]
            level="error" if kind in ("error","retry_exhausted","timeout") or kind.endswith(".error") else (
                "warn" if kind in ("retry","blocked") else "info")
            frame=self.observed_frame(row["utc"])
            frame["agent_event"]=data
            trace=(data["payload"].get("trace") or {}) if isinstance(data.get("payload"),dict) else {}
            self.history.record(frame,kind="agent."+kind,source="mission_agent/tasks",level=level,
                                mission_id=row["task_id"],operation_id=row["operation_id"],detail=row["detail"],
                                data=data,event_utc=row["utc"],source_id=source_id,source_seq=row["seq"],
                                trace={**trace,"task_id":row["task_id"]},
                                cause=self.history.previous_task_event(source_id,row["task_id"]))

    def observed_frame(self, event_utc):
        with self.lock:
            frame=self.at_source_time(event_utc)
            prior=next((s for s in reversed(self.historical) if s["now"]<=frame["event_at"]), None)
            frame["missions"]=prior["missions"] if prior and frame["telemetry_at"] is not None else []
            return frame

    def poll_facts(self):
        # Import task dispatch first, so core events can link to the original operation.
        self.poll_tasks()
        with self.lock:
            self.poll_ledger()

    def watch_journals(self, stop):
        # Source locks, backlog import and snapshot compression must not occupy the ROS executor.
        unavailable = False
        while not stop.is_set():
            try:
                self.poll_facts()
                self.journals.poll(self.history, lambda at: self.observed_frame(at) if at else self.snapshot())
                if unavailable:
                    self.event("module.ingress_restored","edge_console","持久模块事件归档恢复")
                    unavailable = False
            except (OSError, sqlite3.Error) as error:
                if not unavailable:
                    # If archive media itself is unavailable, use an independent diagnostic channel.
                    self.get_logger().error("module journal archive unavailable: " + type(error).__name__)
                    unavailable = True
            stop.wait(0.5)

    def poll_ledger(self):
        try:
            identity, missions, events, steps = self.ledger.batch(self.history)
        except (OSError,sqlite3.Error) as ex:
            if self.ledger_error != str(ex):
                self.ledger_error=str(ex)
                self.event("ledger.unavailable","edge_core/ledger",str(ex),"warn")
            return
        if self.source_id != identity:
            self.source_id = identity
            self.mission_data = {}
        if self.ledger_error:
            self.ledger_error=None
            self.event("ledger.available","edge_core/ledger","只读任务账本恢复")
        for row in events:
            self.ingest_event(row,steps.get(row["mission_id"],[]))
        if len(events)<256:
            legacy = [m for m in missions if m["mission_id"] not in self.mission_data]
            self.mission_data = {m["mission_id"]:m for m in missions}
            for m in legacy:
                self.event("mission.baseline","edge_core/ledger","首次读取任务快照，之前的状态变化无采集记录",
                           mission_id=m["mission_id"],data={"state":m["state"]})

    def tick(self):
        with self.lock:
            now = time.monotonic()
            self.extra.tick(now)
            frame = self.snapshot()
            self.historical.append(frame)
            for key,s in frame["sources"].items():
                if not s["available"]:
                    continue
                if self.health.get(key)!=s["fresh"]:
                    previous = self.health.get(key)
                    self.health[key]=s["fresh"]
                    detail = "采样恢复" if previous is False else "收到有效更新"
                    self.health_event[key] = self.event(
                        "source.online" if s["fresh"] else "source.stale",s["topic"],
                        detail if s["fresh"] else "超过采样新鲜度阈值",
                        "info" if s["fresh"] or key=="operation" else "warn",cause=self.health_event.get(key),
                        data={field:s[field] for field in ("age_s","receive_age_s","source_age_s","ttl_s","stamp_s")})
            pose = frame["sources"]["odom"]
            if pose["fresh"]:
                d=pose["data"]
                if d["v"] is not None and d["w"] is not None:
                    moving = abs(d["v"])>0.025 or abs(d["w"])>0.05
                    if self.moving!=moving:
                        self.moving=moving
                        op=frame["active_operation"]
                        self.event("motion.changed",pose["topic"],"里程计检测到运动" if moving else "里程计低于运动阈值",
                                   mission_id=self.mission_for(op),operation_id=op,data={"moving":moving,"v":d["v"],"w":d["w"]})
            perf=frame["sources"]["perception"]
            mode=(perf["data"] or {}).get("mode") if perf["fresh"] else None
            if mode!=self.mode:
                self.mode=mode
                detail = (perf["data"] or {}).get("reason") or mode if perf["fresh"] else "感知状态采样超时"
                self.event("perception.mode",perf["topic"],str(detail),
                           "info" if mode=="wheel_imu" else "warn",data={"mode":mode})
            interval = 3 if self.moving or frame["active_operation"] else 60
            if now - self.last_baseline >= interval:
                self.last_baseline=now
                self.event("telemetry.sample","edge_console","定时保存观测快照")
