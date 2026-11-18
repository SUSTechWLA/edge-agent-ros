"""Read-only ROS observer. Publish incident transitions into the existing module journal."""
import json
import math
import os
import re
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import time
import sys
import fcntl

import rclpy
from rclpy.node import Node
from rclpy.clock import Clock, ClockType
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image, Imu, LaserScan, PointCloud2
from std_msgs.msg import String

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"agent"))
from module_journal import ModuleJournal
import incidents
from quality import match, measure_cloud, measure_image, measure_imu, measure_scan

VERSION="1.1.0"
SOURCES={"rgb":(Image,measure_image),"depth":(Image,lambda m:measure_image(m,depth=True)),
         "scan":(LaserScan,measure_scan),"points":(PointCloud2,measure_cloud),"imu":(Imu,measure_imu),
         "match":(String,match),"lidar_match":(String,match)}
INFO={"ok","few_returns","few_points"}
GUIDANCE={"black_image":"核对遮挡、镜头和照明。","overexposed":"核对光照与曝光设置。",
          "weak_texture":"检查视野纹理和前端内点统计。","depth_holes":"检查深度有效范围、遮挡和反光。",
          "few_returns":"近场回波少。开阔地可有该观测，结合期望场景或定位前端确认。",
          "few_points":"点云有效点数量少，结合视场和前端统计确认。",
          "low_match_inliers":"前端实际报告匹配支持不足，核对特征、运动与传感器同步。",
          "stream_timeout":"检查驱动、时钟、ROS 域、话题名及 QoS。",
          "late_stamp":"核对传感器与节点的 ROS 时间基准及传输积压。",
          "future_stamp":"核对设备与 ROS 时钟。","repeated_stamp":"检查驱动是否重复发布旧帧。",
          "frame_mismatch":"检查驱动 frame_id 和本插件的配置。"}
LABEL={"rgb":"彩色图像","depth":"深度图像","scan":"二维激光","points":"三维点云","imu":"惯性测量",
       "match":"视觉匹配统计","lidar_match":"激光匹配统计"}


def positive_setting(name,default):
    value=float(os.getenv(name,str(default)))
    if not math.isfinite(value) or not .1<=value<=3600:
        raise ValueError("invalid duration: "+name)
    return value


class Monitor(Node):
    def __init__(self):
        super().__init__("edge_sensor_health")
        self.lock=threading.RLock()
        path=Path(os.getenv("EDGE_HEALTH_JOURNAL","/journal/module-events.sqlite"))
        path.parent.mkdir(parents=True,exist_ok=True)
        self.writer=path.with_name(path.name+".lock").open("a")
        fcntl.flock(self.writer,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.journal=ModuleJournal(path)
        self.timeout=positive_setting("EDGE_HEALTH_TIMEOUT_S",3.)
        self.max_age=positive_setting("EDGE_HEALTH_MAX_AGE_S",1.5)
        self.budget=positive_setting("EDGE_HEALTH_RECHECK_S",6.)
        self.states={}
        self.previous_clock=None
        self.boot_ros_ns=self.get_clock().now().nanoseconds
        robot=os.getenv("EDGE_HEALTH_ROBOT_ID") or self.journal.source_id
        if len(robot)>128 or not robot.isprintable():
            raise ValueError("invalid EDGE_HEALTH_ROBOT_ID")
        now=time.monotonic()
        for key,(kind,measure) in SOURCES.items():
            topic=os.getenv("EDGE_HEALTH_"+key.upper()+"_TOPIC","")
            if not topic:
                continue
            frame=os.getenv("EDGE_HEALTH_"+key.upper()+"_FRAME","")
            ident=dict(robot=robot,domain=self.context.get_domain_id(),sim_time=bool(self.get_parameter("use_sim_time").value),
                       input=key,topic=self.resolve_topic_name(topic),expected_frame=frame,journal=self.journal.source_id)
            self.states[key]={"topic":topic,"expected_frame":frame,"identity":ident,
                "state":"waiting","code":"waiting","consecutive_good":0,"latest":{},"last_rx":now,
                "last_stamp":0,"since":None,"event_id":None}
            self.create_subscription(kind,topic,lambda m,k=key,f=measure:self.observe(k,m,f),qos_profile_sensor_data)
        if not self.states:
            raise ValueError("configure at least one EDGE_HEALTH_*_TOPIC")
        with self.journal.db:
            self.boot_event=self.journal.emit({"module":"sensor.health","kind":"sensor.health.started",
                "code":"sensor.health.started","trace_id":"sensor.health.lifecycle","level":"info",
                "detail":"传感器巡检启动","data":{"version":VERSION,"inputs":list(self.states)}})
            incidents.restore(self.journal,self.states,now,self.boot_event)
        self.steady=Clock(clock_type=ClockType.STEADY_TIME)
        self.create_timer(.5,self.tick,clock=self.steady)

    def record(self,key,phase,code,now):
        row=self.states[key]
        row.update(state=phase,code=code)
        detail=GUIDANCE.get(code,"检查传感器最近测量、坐标系与时序。")
        if phase=="recovered":
            detail="已连续检测到三份独立的新鲜正常测量。"
        with self.journal.db:
            row["event_id"]=self.journal.emit({"module":"sensor.health","layer":"perception","kind":"sensor."+phase,
                "code":"sensor."+code,"level":"warn" if phase in ("rechecking","needs_attention") else "info",
                "trace_id":"sensor.health."+key,"parent_event_id":row["event_id"],"detail":detail,
                "data":{"input":key,"topic":row["topic"],"phase":phase,"observation":row["latest"],
                        "elapsed_s":round(now-row["since"],3) if row["since"] is not None else 0,
                        "consecutive_good":row["consecutive_good"],"sample_timeout_s":self.timeout,
                        "max_age_s":self.max_age,"recheck_budget_s":self.budget,"identity":row["identity"]}})
            if phase in ("rechecking","needs_attention"):
                incidents.remember(self.journal.db,key,row)
            else:
                incidents.forget(self.journal.db,key)

    def assess(self,key,code,now):
        r=self.states[key]
        if code not in INFO:
            r["consecutive_good"]=0
            if r["since"] is None:
                r["since"]=now
                self.record(key,"rechecking",code,now)
            elif now-r["since"]>=self.budget and r["state"]!="needs_attention":
                self.record(key,"needs_attention",code,now)
            return
        if r["since"] is not None:
            r["consecutive_good"]+=1
            if r["consecutive_good"]>=3:
                self.record(key,"recovered",code,now)
                r["since"]=None
        elif r["state"]=="waiting" or r["code"]!=code:
            self.record(key,"observed",code,now)

    def observe(self,key,msg,measure):
        now=time.monotonic()
        with self.lock:
            row=self.states[key]
            # Bound the paid analysis rate per input on edge processors.
            if now-row.get("last_analysis",-1e12)<.09:
                return
            row["last_rx"]=row["last_analysis"]=now
            frame=""
            stamp=None
            values={}
            age=None
            ros_ns=self.get_clock().now().nanoseconds
            if self.previous_clock is not None and ros_ns<self.previous_clock:
                self.boot_ros_ns=ros_ns
                for name,stream in self.states.items():
                    stream["last_stamp"]=0
                    self.assess(name,"clock_reset",now)
            self.previous_clock=ros_ns
            try:
                if key.endswith("match"):
                    stamp,frame,code,values=measure(msg)
                else:
                    h=msg.header
                    frame=h.frame_id
                    stamp=h.stamp.sec*10**9+h.stamp.nanosec
                    code,values=measure(msg)
                if not 0 < stamp < 2**63:
                    raise ValueError("invalid_stamp")
                age=(ros_ns-stamp)*1e-9
                if not frame or len(frame)>200 or row["expected_frame"] and frame!=row["expected_frame"]:
                    code="frame_mismatch"
                elif ros_ns==0:
                    code="clock_unavailable"
                elif age < -.1:
                    code="future_stamp"
                elif age>self.max_age:
                    code="late_stamp"
                elif row.get("prior_frame") and not row["expected_frame"] and frame!=row["prior_frame"]:
                    with self.journal.db:
                        incidents.boundary(self.journal,key,row["event_id"],"source_detached","重启后测量的坐标系已改变。",
                            {"prior_frame":row["prior_frame"],"current_frame":frame,"identity":row["identity"]})
                        incidents.forget(self.journal.db,key)
                    row.update(state="waiting",code="waiting",since=None,event_id=None,consecutive_good=0,last_stamp=stamp)
                    row.pop("prior_frame")
                elif "prior_frame" in row and ros_ns < row["last_stamp"]-100_000_000:
                    # A reset simulation clock cannot establish continuity across two ROS epochs.
                    with self.journal.db:
                        row["event_id"]=incidents.boundary(self.journal,key,row["event_id"],"clock_epoch_changed",
                            "重启前后的 ROS 时钟不连续，重新建立样本时间基线。",
                            {"prior_stamp_ns":row["last_stamp"],"current_ros_ns":ros_ns,"identity":row["identity"]})
                        row["last_stamp"]=0
                        incidents.remember(self.journal.db,key,row)
                    self.boot_ros_ns=ros_ns
                    code="clock_reset"
                elif stamp<=row["last_stamp"] or row["since"] is not None and stamp<self.boot_ros_ns:
                    code="repeated_stamp"
                else:
                    row["last_stamp"]=stamp
            except (ValueError,TypeError,OverflowError,IndexError) as exc:
                allowed={"unsupported_encoding","invalid_image_layout","invalid_scan_layout","invalid_cloud_layout",
                         "invalid_cloud_fields","invalid_match_report","invalid_stamp"}
                code=str(exc) if str(exc) in allowed else "invalid_measurement"
                if not isinstance(stamp,int) or not 0<stamp<2**63:
                    stamp=None
            row["latest"]=dict(stamp_ns=stamp,frame_id=frame[:200],ros_now_ns=ros_ns,age_s=round(age,4) if age is not None else None,
                               measurement_code=code,metrics=values)
            self.assess(key,code,now)
            if row["since"] is None:
                row.pop("prior_frame",None)

    def tick(self):
        now=time.monotonic()
        with self.lock:
            for key,row in self.states.items():
                if now-row["last_rx"]>=self.timeout:
                    self.assess(key,"stream_timeout",now)
            with self.journal.db:
                self.journal.heartbeat(6)

    def snapshot(self):
        with self.lock:
            now=time.monotonic()
            return {"module":"sensor.health","version":VERSION,"inputs":{
                k:{**{f:r[f] for f in ("topic","state","code","consecutive_good","latest","event_id")},
                   "silence_s":round(now-r["last_rx"],3),
                   "action":GUIDANCE.get(r["code"],"查看最近测量和事件证据。")} for k,r in self.states.items()},
                "unconfigured":sorted(SOURCES.keys()-self.states.keys())}

    def diagnose(self):
        snapshot=self.snapshot()
        observations=[]
        attention=[]
        for key,row in snapshot["inputs"].items():
            sample=row["latest"]
            observations.append(dict(input=key,topic=row["topic"],state=row["state"],code=row["code"],
                latest_code=sample.get("measurement_code"),stamp_ns=sample.get("stamp_ns"),frame=sample.get("frame_id"),
                age_at_receipt_s=sample.get("age_s"),silence_s=row["silence_s"],metrics=sample.get("metrics",{}),
                interpretation=GUIDANCE.get(row["code"],"查看样本及事件记录。"),event_id=row["event_id"]))
            if row["state"] in ("needs_attention","rechecking","waiting") or row["code"]!="ok":
                attention.append(LABEL[key]+" "+row["code"]+"（"+GUIDANCE.get(row["code"],"查看最新样本")+"）")
        summary=f"已配置 {len(observations)} 个传感器或前端输入。"
        summary+=("请核查 "+"、".join(attention)+"。") if attention else "当前输入均已取得正常观测。"
        if snapshot["unconfigured"]:
            summary+="未配置 "+"、".join(LABEL[key] for key in snapshot["unconfigured"])+"。"
        return dict(summary=summary,scope="current_observation",observations=observations,unconfigured=snapshot["unconfigured"])


class Status(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass
    def do_POST(self):
        if self.path!="/diagnose":
            return self.send_error(404)
        try:
            length=int(self.headers.get("Content-Length","0"))
            if not 0<length<=8192:
                return self.send_error(413)
            req=json.loads(self.rfile.read(length))
            if not isinstance(req,dict) or req.get("version")!="1" or not re.fullmatch(
                    r"[\w./:@-]{1,240}",str(req.get("operation_id",""))) or not isinstance(req.get("operation_id"),str):
                return self.send_error(400)
            args=req.get("arguments")
            if not isinstance(args,dict) or set(args)!={"scope"} or args["scope"] not in ("导航","本机","当前"):
                return self.send_error(400)
            result={"operation_id":req["operation_id"],"state":"SUCCEEDED","evidence":self.server.node.diagnose()}
        except (OSError,ValueError,TypeError,RecursionError):
            return self.send_error(400)
        self.respond(json.dumps(result,ensure_ascii=False,allow_nan=False).encode(),"application/json; charset=utf-8")

    def do_GET(self):
        if self.path not in ("/","/status","/health"):
            self.send_error(404)
            return
        if self.path=="/":
            raw=Path(__file__).with_name("index.html").read_bytes()
            mime="text/html; charset=utf-8"
        else:
            payload=self.server.node.snapshot() if self.path=="/status" else {
                "id":"sensor.health","version":VERSION,"started_event":self.server.node.boot_event}
            raw=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
            mime="application/json; charset=utf-8"
        self.respond(raw,mime)

    def respond(self,raw,mime):
        self.send_response(200)
        self.send_header("Content-Type",mime)
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Server(HTTPServer):
    def get_request(self):
        sock,addr=super().get_request()
        sock.settimeout(2)
        return sock,addr


if __name__=="__main__":
    rclpy.init()
    node=Monitor()
    server=Server((os.getenv("EDGE_HEALTH_BIND","127.0.0.1"),int(os.getenv("EDGE_HEALTH_PORT","8136"))),Status)
    server.node=node
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt,rclpy.executors.ExternalShutdownException):
        pass
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        node.journal.close()
        node.writer.close()
        node.destroy_node()
        rclpy.try_shutdown()
