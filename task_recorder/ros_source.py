"""Read-only ROS 2 source for an explicitly started task recording."""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener
import uuid


def post_json(http, endpoint, body=None):
    raw = None if body is None else json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
    request = Request(endpoint, data=raw, headers={"Content-Type": "application/json"})
    with http.open(request, timeout=3) as stream:
        return json.load(stream)


class Sink:
    """Bound network backpressure and retry the same event ID after a lost response."""
    def __init__(self, endpoint, task, clock_domain=None):
        self.endpoint = endpoint
        self.task = task
        self.clock_domain = clock_domain
        self.queue = Queue(maxsize=96)
        self.end = threading.Event()
        self.problem = None
        self.worker = threading.Thread(target=self.upload, daemon=True)
        self.worker.start()

    def emit(self, kind, stamp, **values):
        if stamp <= 0 or self.end.is_set():
            return
        if self.clock_domain:
            values.setdefault("clock_domain",self.clock_domain)
        row = dict(values, kind=kind, stamp_ns=stamp, task_id=self.task, event_id=uuid.uuid4().hex)
        try:
            self.queue.put_nowait(row)
        except Full:
            self.problem = "recording queue exceeded 96 events; acquisition interrupted"
            self.end.set()

    def upload(self):
        http = build_opener(ProxyHandler({}))
        while not self.end.is_set():
            try:
                row = self.queue.get(timeout=.2)
            except Empty:
                continue
            try:
                for attempt in range(5):
                    try:
                        post_json(http, self.endpoint+"/events", row)
                        break
                    except HTTPError as error:
                        if error.code == 409 and post_json(http,self.endpoint)["status"] != "ACTIVE":
                            self.end.set()
                            return
                        if error.code < 500 or attempt == 4:
                            raise
                    except (OSError, URLError) as error:
                        if attempt == 4:
                            raise
                    self.end.wait(.12*2**attempt)
            except Exception as error:
                self.problem = "recording upload interrupted: "+str(error)
                self.end.set()
            finally:
                self.queue.task_done()


def attachment(name, data):
    return {"name": name, "base64": base64.b64encode(data).decode("ascii")}


def sensor_rate(value):
    hz = float(value)
    if not 0.1 <= hz <= 10:
        raise argparse.ArgumentTypeError("sensor rate must be 0.1 through 10 Hz")
    return hz


def check_signal_scope(args):
    deployment = os.getenv("EDGE_RECORDING_DEPLOYMENT")
    if args.sim and deployment == "hardware":
        raise ValueError("hardware recorder rejects the simulation switch")
    if (args.laser_scan or args.imu) and (not args.sim or deployment != "gazebo"):
        raise ValueError("direct raw sensor summaries require the isolated Gazebo collector deployment")


def scan_metrics(scan):
    counts = {"beam_count":len(scan.ranges), "return_count":0, "no_return_count":0, "invalid_count":0}
    if len(scan.ranges) > 65536:
        return counts, "scan_exceeds_beam_limit"
    if not all(map(math.isfinite, (scan.range_min, scan.range_max))) or not 0 <= scan.range_min < scan.range_max:
        return counts, "invalid_range_limits"
    nearest = math.inf
    for ray in scan.ranges:
        if ray == math.inf:
            counts["no_return_count"] += 1
        elif not math.isfinite(ray) or not scan.range_min <= ray <= scan.range_max:
            counts["invalid_count"] += 1
        else:
            counts["return_count"] += 1
            nearest = min(nearest, ray)
    if counts["return_count"]:
        counts["nearest_m"] = nearest
        return counts, ""
    return counts, "no_valid_ranges"


def imu_metrics(imu):
    if imu.angular_velocity_covariance[0] == -1:
        return {}, "gyro_unavailable"
    gyro = imu.angular_velocity
    if not all(map(math.isfinite, (gyro.x, gyro.y, gyro.z))):
        return {}, "nonfinite_gyro"
    return {"angular_x_rad_s":gyro.x, "angular_y_rad_s":gyro.y, "angular_z_rad_s":gyro.z}, ""


def task_event(event, task):
    if not isinstance(event, dict):
        return False
    # Shared ROS domains carry events for multiple tasks. Accept exact roots or scoped child IDs.
    return any(isinstance(v,str) and (v == task or v.startswith(task+"/"))
               for v in (event.get(k) for k in ("task_id","trace_id","mission_id","operation_id")))


class Sources:
    def __init__(self, node, args, sink, kinds):
        check_signal_scope(args)
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import OccupancyGrid, Odometry
        from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
        from rosgraph_msgs.msg import Clock
        from sensor_msgs.msg import Image
        from std_msgs.msg import String

        self.node, self.args, self.sink = node, args, sink
        self.clock = 0
        self.last = {}
        self.signal_next = {}
        self.mark_offset = 0
        self.gz = None

        def sub(kind, msg, topic, callback, qos=qos_profile_sensor_data):
            if kind in kinds and topic:
                node.create_subscription(msg, topic, callback, qos)

        if args.sim:
            node.create_subscription(Clock,"/clock",self.on_clock,qos_profile_sensor_data)
        self.send_clock = "clock" in kinds
        if not args.sim and self.send_clock:
            node.create_timer(.5,lambda:sink.emit("clock",self.now()))
        sub("map",OccupancyGrid,args.map,self.grid,QoSProfile(depth=16 if args.all_map_revisions else 1,
                                                               durability=DurabilityPolicy.TRANSIENT_LOCAL))
        sub("fused_pose",Odometry,args.pose,lambda m:self.pose("fused_pose",m))
        sub("world_pose",Odometry,args.world_pose,lambda m:self.pose("world_pose",m))
        sub("twist",Twist,args.twist,self.twist)
        sub("bus",String,args.bus,self.bus)
        sub("rgb",Image,args.camera,self.rgb)
        if args.laser_scan and "laser_scan" in kinds:
            from sensor_msgs.msg import LaserScan
            sub("laser_scan",LaserScan,args.laser_scan,lambda m:self.sensor("laser_scan",m,args.laser_scan,scan_metrics))
        if args.imu and "imu" in kinds:
            from sensor_msgs.msg import Imu
            sub("imu",Imu,args.imu,lambda m:self.sensor("imu",m,args.imu,imu_metrics))
        if "rgb" in kinds and args.gz_camera:
            from gz.transport13 import Node
            from gz.msgs10.image_pb2 import Image as GzImage
            self.gz=Node()
            if not self.gz.subscribe(GzImage,args.gz_camera,self.gz_rgb):
                raise RuntimeError("Gazebo RGB subscription failed")
        if "mission" in kinds and args.markers:
            node.create_timer(.12,self.markers)

    def now(self):
        return self.clock if self.args.sim else self.node.get_clock().now().nanoseconds

    @staticmethod
    def stamp(message):
        t=message.header.stamp
        return t.sec*10**9+t.nanosec

    def due(self, kind, stamp, interval):
        if stamp <= 0 or stamp-self.last.get(kind, -interval)<interval:
            return False
        self.last[kind]=stamp
        return True

    def on_clock(self, msg):
        stamp=msg.clock.sec*10**9+msg.clock.nanosec
        if self.clock and stamp<self.clock:
            self.sink.problem="simulation clock went backwards; start a new recording epoch"
            self.sink.end.set()
            return
        self.clock=stamp
        if self.send_clock and self.due("clock",stamp,500_000_000):
            self.sink.emit("clock",stamp)

    def pose(self, kind, msg):
        ns=self.stamp(msg)
        if self.due(kind,ns,120_000_000):
            p,q=msg.pose.pose.position,msg.pose.pose.orientation
            yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
            if all(map(math.isfinite,[p.x,p.y,yaw])):
                self.sink.emit(kind,ns,xy=[p.x,p.y],yaw=yaw,frame=msg.header.frame_id)

    def twist(self,msg):
        ns=self.now()
        if self.due("twist",ns,180_000_000) and all(map(math.isfinite,[msg.linear.x,msg.angular.z])):
            self.sink.emit("twist",ns,linear=msg.linear.x,angular=msg.angular.z)

    def sensor(self, kind, msg, topic, summarize):
        # Rate-limit by local elapsed time even when a device clock stalls or jumps.
        now = time.monotonic()
        if now < self.signal_next.get(kind, 0):
            return
        self.signal_next[kind] = now + 1/self.args.sensor_hz
        stamp = self.stamp(msg)
        frame = msg.header.frame_id
        valid_stamp = 0 < stamp < 2**63 and 0 <= msg.header.stamp.nanosec < 10**9
        if not valid_stamp:
            metrics, reason = {}, "invalid_header_stamp"
        elif not frame or len(frame) > 256:
            metrics, reason = {}, "invalid_frame_id"
        else:
            metrics, reason = summarize(msg)
        values = dict(topic=topic, frame_id=frame[:256], source="sensor_msgs/msg/"+type(msg).__name__,
                      header_stamp_ns=stamp, stamp_origin="header" if valid_stamp else "receipt",
                      valid=not reason, metrics=metrics)
        if reason:
            values["reason"] = reason
        if not valid_stamp:
            values["clock_domain"] = "observer_unix_ns"
        self.sink.emit(kind, stamp if valid_stamp else time.time_ns(), **values)

    def bus(self,msg):
        try:
            event=json.loads(msg.data)
            if task_event(event,self.args.task_id):
                self.sink.emit("bus",self.now(),event=event)
        except ValueError:
            pass

    def grid(self,msg):
        ns=self.stamp(msg)
        if (not self.due("map",ns,1 if self.args.all_map_revisions else 400_000_000) or not 0<len(msg.data)<=2000000
                or msg.info.width*msg.info.height!=len(msg.data)):
            return
        cells=list(msg.data)
        o=msg.info.origin
        record=dict(frame=msg.header.frame_id,width=msg.info.width,height=msg.info.height,resolution=msg.info.resolution,
                    origin=[o.position.x,o.position.y,2*math.atan2(o.orientation.z,o.orientation.w)],cells=cells)
        data=json.dumps(record,allow_nan=False,separators=(",",":")).encode()
        self.sink.emit("map",ns,frame=msg.header.frame_id,known=sum(c>=0 for c in cells),
                       media=attachment(f"grid/{ns:018d}.json",data))

    def png(self,ns,data,width,height,topic,source):
        self.sink.emit("rgb",ns,topic=topic,source=source,width=width,height=height,sha256=hashlib.sha256(data).hexdigest(),
                       media=attachment(f"rgb/{ns:018d}.png",data))

    def rgb(self,msg):
        from perception.camera.frames import raw_frame
        ns=self.stamp(msg)
        if self.due("rgb",ns,110_000_000):
            png,_,_,_,width,height,_=raw_frame(msg)
            self.png(ns,png,width,height,self.args.camera,"sensor_msgs/msg/Image")

    def gz_rgb(self,msg):
        from scene.campus_large.capture import encode
        stamp=msg.header.stamp
        ns=stamp.sec*10**9+stamp.nsec
        if self.due("rgb",ns,110_000_000):
            try:
                png,meta=encode(msg)
                self.png(ns,png,meta["width"],meta["height"],self.args.gz_camera,"gz.msgs.Image")
            except ValueError as error:
                self.sink.problem=str(error)
                self.sink.end.set()

    def markers(self):
        ns=self.now()
        if ns<=0 or not self.args.markers.is_file():
            return
        with self.args.markers.open("rb") as stream:
            stream.seek(self.mark_offset)
            for _ in range(100):
                line=stream.readline(32769)
                if len(line)>32768:
                    raise ValueError("marker exceeds 32 KiB")
                if not line.endswith(b"\n"):
                    break
                row=json.loads(line)
                self.mark_offset=stream.tell()
                if row.pop("task_id",None)==self.args.task_id:
                    value=row.get("source_stamp_ns",row.get("stamp_ns"))
                    stamp=value if type(value) is int and 0<value<=ns+500_000_000 else ns
                    values={k:v for k,v in row.items() if k not in ("kind","event_id","stamp_ns","media","file","seq","received_ns")}
                    if row.get("file"):
                        values["artifact"]=row["file"]
                    self.sink.emit("mission",stamp,**values)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url",required=True)
    ap.add_argument("--recording-id",required=True)
    ap.add_argument("--task-id",required=True)
    ap.add_argument("--sim",action="store_true")
    cameras=ap.add_mutually_exclusive_group()
    cameras.add_argument("--camera",help="ROS sensor_msgs/Image topic")
    cameras.add_argument("--gz-camera",help="Gazebo native RGB8 topic in a simulation image")
    ap.add_argument("--laser-scan", help="opt in to summaries of a ROS sensor_msgs/LaserScan topic")
    ap.add_argument("--imu", help="opt in to measured gyro from a ROS sensor_msgs/Imu topic")
    ap.add_argument("--sensor-hz", type=sensor_rate, default=4, help="maximum samples per second per raw sensor")
    ap.add_argument("--all-map-revisions", action="store_true", help="preserve each stamped map revision for decision audits")
    ap.add_argument("--clock-domain", help="explicit shared time basis guaranteed by this recording deployment")
    ap.add_argument("--map",default="/edge/perception/map")
    ap.add_argument("--pose",default="/edge/perception/pose")
    ap.add_argument("--twist",default="/cmd_vel")
    ap.add_argument("--bus",default="/edge/events")
    ap.add_argument("--world-pose",help="optional independent simulator witness topic")
    ap.add_argument("--markers",type=Path,help="optional append-only task marker JSONL")
    args=ap.parse_args()
    try:
        check_signal_scope(args)
    except ValueError as error:
        ap.error(str(error))
    import rclpy
    endpoint=args.url.rstrip("/")+"/recordings/"+quote(args.recording_id,safe="")
    http=build_opener(ProxyHandler({}))
    delay_until=time.monotonic()+120
    while True:
        try:
            header=post_json(http,endpoint)
            break
        except HTTPError as error:
            if error.code!=404: raise
        except URLError:
            pass
        if time.monotonic()>delay_until:
            raise TimeoutError("no start for selected recording")
        time.sleep(.3)
    if header.get("task_id")!=args.task_id or header.get("status")!="ACTIVE":
        raise ValueError("recording must be active and belong to the requested task")
    rclpy.init(args=[])
    node=rclpy.create_node("edge_recording_source_"+uuid.uuid4().hex[:8])
    sink=Sink(endpoint,args.task_id,args.clock_domain)
    source=None
    try:
        source=Sources(node,args,sink,set(header["sources"]))
        poll_at=time.monotonic()+2
        while rclpy.ok() and not sink.end.is_set():
            rclpy.spin_once(node,timeout_sec=.1)
            if time.monotonic()>=poll_at:
                poll_at=time.monotonic()+2
                if post_json(http,endpoint)["status"]!="ACTIVE": break
        if sink.problem: raise RuntimeError(sink.problem)
    finally:
        sink.end.set()
        if source and source.gz: source.gz.unsubscribe(args.gz_camera)
        sink.worker.join(timeout=4)
        node.destroy_node()
        rclpy.try_shutdown()


if __name__=="__main__":
    main()
