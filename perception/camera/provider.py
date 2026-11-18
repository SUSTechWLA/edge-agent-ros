"""Read-only ROS 2 camera source plugin. Owns subscriptions and latest-frame previews."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

import rclpy
from rclpy.clock import Clock,ClockType
from rclpy.node import Node
from rclpy.qos import QoSProfile,ReliabilityPolicy
from sensor_msgs.msg import Image,CompressedImage,CameraInfo

from frames import Source, config, NAME
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"agent"))
from module_journal import ModuleJournal

WEB=Path(__file__).with_name("web.html")
SENSOR_QOS=QoSProfile(depth=1,reliability=ReliabilityPolicy.BEST_EFFORT)
VERSION="1.0.1"


class CameraNode(Node):
    def __init__(self):
        super().__init__("edge_camera_preview")
        self.lock=threading.RLock()
        self.sources={}
        self.handles={}
        self.config_sha=None
        self.config_status="waiting"
        self.ros_time=0
        self.run_id=uuid.uuid4().hex
        self.journal=ModuleJournal("/journal/module-events.sqlite")
        self.started_event=self.fact("started",{"version":VERSION},self.run_id)
        self.directory=Path(os.getenv("EDGE_CAMERA_CONFIG_DIR","/config"))
        self.reload()
        self.maintenance=self.create_timer(.25,self.poll,clock=Clock(clock_type=ClockType.STEADY_TIME))
        self.next_read=0

    def fact(self,stage,data,parent):
        with self.journal.db:
            return self.journal.emit({"module":"camera","kind":"camera."+stage,"code":"camera."+stage,
               "trace_id":self.run_id,"parent_event_id":None if stage=="started" else parent,"data":data})

    def clock_ns(self):
        return self.get_clock().now().nanoseconds

    def transition(self,src,previous):
        if previous!=(src.state,src.reason):
            src.event=self.fact("source_state",{"source_id":src.spec["id"],"state":src.state,"reason":src.reason,
                "stamp_ns":src.last_stamp,"config_sha256":self.config_sha},src.event)

    def callback(self,src,meta=False):
        def receive(msg):
            with self.lock:
                if self.sources.get(src.spec["id"]) is not src:
                    return
                if meta:
                    src.calibration(msg,time.monotonic())
                else:
                    old=src.state,src.reason
                    now=self.checked_clock()
                    src.observe(msg,time.monotonic(),now)
                    self.transition(src,old)
        return receive

    def checked_clock(self):
        now=self.clock_ns()
        if self.ros_time and now<self.ros_time:
            for src in self.sources.values():
                prev=src.state,src.reason
                src.reset_clock()
                self.transition(src,prev)
        self.ros_time=now
        return now

    def reload(self):
        added={}
        try:
            with (self.directory/"sources.json").open("rb") as f:
                specs,version=config(f.read(16385))
            if version==self.config_sha:
                if self.config_status!="ok":
                    self.fact("config_restored",{"config_sha256":version},self.started_event)
                self.config_status="ok"
                return
            for name,spec in specs.items():
                if name in self.sources and self.sources[name].spec==spec:
                    continue
                src=Source(spec)
                handles=[]
                added[name]=(src,handles)
                cls=Image if spec["type"]=="Image" else CompressedImage
                handles.append(self.create_subscription(cls,spec["topic"],self.callback(src),SENSOR_QOS))
                if spec.get("info_topic"):
                    handles.append(self.create_subscription(CameraInfo,spec["info_topic"],self.callback(src,True),SENSOR_QOS))
        except (ValueError,TypeError,OSError,RuntimeError,RecursionError):
            for _,handles in added.values():
                for sub in handles:
                    self.destroy_subscription(sub)
            if self.config_status!="invalid":
                self.fact("config_invalid",{"last_good":self.config_sha},self.started_event)
            self.config_status="invalid"
            return
        previous=self.config_sha
        for name in list(self.sources):
            if name not in specs or name in added:
                src=self.sources.pop(name)
                for sub in self.handles.pop(name):
                    self.destroy_subscription(sub)
                self.fact("source_removed",{"source_id":name,"config_sha256":previous},src.event)
        self.config_sha,self.config_status=version,"ok"
        head=self.fact("configured",{"source_count":len(specs),"config_sha256":version,"previous":previous},self.started_event)
        for name,(src,handles) in added.items():
            self.sources[name],self.handles[name]=src,handles
            src.event=self.fact("source_added",{"source_id":name,"topic":src.spec["topic"],"type":src.spec["type"]},head)

    def refresh_sources(self,now):
        ros=self.checked_clock()
        for src in self.sources.values():
            old=src.state,src.reason
            src.refresh(now,ros)
            self.transition(src,old)

    def poll(self):
        with self.lock:
            now=time.monotonic()
            if now>=self.next_read:
                self.reload()
                self.next_read=now+1
            self.refresh_sources(now)

    def response(self,path):
        if path=="/health":
            with self.lock:
                return json_reply(200,{"id":"camera","version":VERSION,"started_event":self.started_event,"config_status":self.config_status})
        if path=="/":
            return 200,"text/html; charset=utf-8",WEB.read_bytes(),{}
        with self.lock:
            now=time.monotonic()
            if path=="/api/cameras":
                self.refresh_sources(now)
                return json_reply(200,{"config_status":self.config_status,"config_sha256":self.config_sha,
                               "sources":[src.status(now) for src in self.sources.values()]})
            parts=path.split("/")
            if len(parts)==5 and parts[:3]==["","api","cameras"] and parts[4]=="image" and NAME.fullmatch(parts[3]):
                src=self.sources.get(parts[3])
                if src is None:
                    return json_reply(404,{"error":"camera.unknown_source"})
                self.refresh_sources(now)
                if src.state!="online" or src.data is None:
                    return json_reply(503,{"error":"camera.image_unavailable","state":src.state,"reason":src.reason})
                frame=src.meta["frame_id"].encode("ascii","backslashreplace").decode()
                return 200,src.meta["content_type"],src.data,{"X-Frame-Stamp-Ns":str(src.last_stamp),"X-Frame-Id":frame}
        return json_reply(404,{"error":"camera.not_found"})


def json_reply(status,obj):
    return status,"application/json",json.dumps(obj,allow_nan=False).encode(),{}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def do_GET(self):
        path=self.path.partition("?")[0]
        code,mime,data,headers=self.server.node.response(path)
        self.send_response(code)
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Content-Type",mime)
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        if path=="/health":
            self.send_header("Access-Control-Allow-Origin","*")
        for k,v in headers.items():
            self.send_header(k,v)
        self.end_headers()
        self.wfile.write(data)


class CameraHTTP(ThreadingHTTPServer):
    def __init__(self,address,node):
        self.node=node
        self.slots=threading.BoundedSemaphore(8)
        super().__init__(address,Handler)

    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        request.settimeout(3)
        try:
            super().process_request(request,address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self,request,address):
        def expire():
            try:
                request.shutdown(2)
            except OSError:
                pass
        limit=threading.Timer(5,expire)
        try:
            limit.start()
            super().process_request_thread(request,address)
        finally:
            limit.cancel()
            self.slots.release()

    def handle_error(self,request,address):
        if not isinstance(sys.exc_info()[1],OSError):
            super().handle_error(request,address)


def main():
    rclpy.init()
    node=CameraNode()
    http=CameraHTTP((os.getenv("EDGE_CAMERA_BIND","0.0.0.0"),int(os.getenv("EDGE_CAMERA_PORT","8771"))),node)
    thread=threading.Thread(target=http.serve_forever,daemon=True)
    thread.start()
    try:
        rclpy.spin(node)
    finally:
        http.shutdown()
        thread.join()
        http.server_close()
        node.destroy_node()
        node.journal.close()
        if rclpy.ok():
            rclpy.shutdown()


if __name__=="__main__":
    main()
