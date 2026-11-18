"""Read-only ROS PointCloud2 map and viewer. All task and motion ownership stays upstream."""
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from rclpy.time import Time
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import PointCloud2
from std_msgs.msg import String
from tf2_ros import Buffer, TransformException, TransformListener

from geometry import decode, IDENTITY, rotation, Surface, VOXEL_M
from inputs import discover, EXTRA_VOXELS
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
from module_journal import ModuleJournal

STATIC = Path(__file__).parent / "web"
TTL = 3.0
VERSION = "1.1.0"
CLOUD_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)


class MapNode(Node):
    def __init__(self):
        super().__init__("edge_map3d")
        self.frame = os.getenv("EDGE_MAP3D_FRAME", "odom")
        self.topic = os.getenv("EDGE_MAP3D_TOPIC", "/edge/map3d/points")
        self.simulated = os.getenv("EDGE_MAP3D_ENV") == "simulation"
        self.lock = threading.RLock()
        self.journal = ModuleJournal(os.getenv("EDGE_MAP3D_JOURNAL", "/journal/module-events.sqlite"))
        self.generation = 1
        self.clock_ns = None
        self.surface = Surface()
        self.layers = self.empty_layers()
        self.input_dir = Path(os.getenv("EDGE_MAP3D_INPUTS_DIR", "/etc/edge/map3d-inputs"))
        self.inputs = {}
        self.catalog_error = None
        self.states = {}
        self.last_event = {}
        self.buffer = Buffer(node=self)
        self.tf = TransformListener(self.buffer, self)
        self.create_subscription(PointCloud2, self.topic, self.cloud, CLOUD_QOS)
        self.create_subscription(Odometry, os.getenv("EDGE_MAP3D_POSE", "/edge/perception/pose"), self.pose, qos_profile_sensor_data)
        self.create_subscription(String, "/edge/semantic_map", self.scene, qos_profile_sensor_data)
        if self.simulated:
            self.create_subscription(Clock, "/clock", self.clock, qos_profile_sensor_data)
        # Use steady time even when simulation time is paused.
        from rclpy.clock import Clock as NodeClock, ClockType
        self.create_timer(1, self.health, clock=NodeClock(clock_type=ClockType.STEADY_TIME))
        self.fact("plugin", "started", version=VERSION, frame=self.frame)

    @staticmethod
    def empty_layers():
        return {"cloud": {"source_frame": None, "stamp_s": None, "sample_points": 0},
                "pose": {"xyz": None, "stamp_s": None}, "scene": {"features": [], "zones": []}}

    def fact(self, layer, state, **fields):
        with self.journal.db:
            self.last_event[layer] = self.journal.emit({
                "module":"map3d", "kind":"map3d."+state, "code":"map3d."+state,
                "trace_id":"map3d."+layer, "parent_event_id":self.last_event.get(layer),
                "level":"warn" if state in ("error","stale","clock_reset") else "info",
                "data":{"layer":layer, "generation":self.generation, **fields}})

    def clock(self, msg):
        now = msg.clock.sec*10**9 + msg.clock.nanosec
        with self.lock:
            if self.clock_ns is not None and now < self.clock_ns:
                self.surface = Surface()
                self.layers = self.empty_layers()
                for key, item in self.inputs.items():
                    item["surface"] = Surface(EXTRA_VOXELS)
                    self.layers["lidar."+key] = self.empty_cloud(item["config"])
                self.states.clear()
                self.generation += 1
                self.fact("plugin", "clock_reset", previous_ns=self.clock_ns, clock_ns=now)
            self.clock_ns = now

    def age(self, layer, now):
        if "received" not in layer:
            return None
        age = now - layer["received"]
        if layer.get("stamp_s") is not None:
            clock_ns = self.clock_ns if self.simulated else self.get_clock().now().nanoseconds
            if clock_ns is not None:
                age = max(age, clock_ns / 1e9 - layer["stamp_s"])
        return age

    def status(self, layer, now):
        if layer.get("error") == "no_valid_returns" and now - layer["packet_received"] > layer.get("ttl_s", TTL):
            return "stale"
        if layer.get("error"):
            return "error"
        if "received" not in layer:
            return "waiting"
        return "online" if self.age(layer, now) <= layer.get("ttl_s", TTL) else "stale"

    def health(self):
        with self.lock, self.journal.db:
            self.refresh_inputs()
            self.journal.heartbeat(6)
            for name in self.layers:
                self.transition(name)

    def transition(self, name):
        layer = self.layers[name]
        key = (self.status(layer, time.monotonic()), layer.get("error"))
        if self.states.get(name) != key:
            self.states[name] = key
            self.fact(name, key[0], reason=key[1])

    def error(self, name, reason):
        with self.lock:
            self.layers[name]["error"] = reason
            self.transition(name)

    def valid_stamp(self, ns, ttl=TTL):
        now = self.clock_ns if self.simulated else self.get_clock().now().nanoseconds
        if now is None or ns <= 0 or not -min(1.0, ttl) <= (now - ns)/1e9 <= ttl:
            raise ValueError("stamp_outside_window")

    @staticmethod
    def empty_cloud(config):
        data = {"source_frame":None, "stamp_s":None, "sample_points":0, "ttl_s":config.ttl_s}
        if config.error:
            data["error"] = config.error
        return data

    def refresh_inputs(self):
        try:
            configured = discover(self.input_dir, self.topic)
        except (ValueError, OSError) as exc:
            reason = "input_catalog_limit" if isinstance(exc,ValueError) else "input_catalog_unavailable"
            if self.catalog_error != reason:
                self.fact("inputs","error",reason=reason)
            self.catalog_error = reason
            # Fail closed until the entire operator catalog is bounded and readable.
            configured = {}
        else:
            if self.catalog_error:
                self.fact("inputs","restored")
            self.catalog_error = None
        for key, item in list(self.inputs.items()):
            if configured.get(key) == item["config"]:
                continue
            if item["subscription"] is not None:
                self.destroy_subscription(item["subscription"])
            del self.inputs[key], self.layers["lidar."+key]
            self.states.pop("lidar."+key,None)
            self.fact("lidar."+key, "removed", topic=item["config"].topic)
        for key, config in configured.items():
            if key in self.inputs:
                continue
            name = "lidar."+key
            item = {"config":config, "surface":Surface(EXTRA_VOXELS), "subscription":None}
            self.inputs[key] = item
            self.layers[name] = self.empty_cloud(config)
            if not config.error:
                try:
                    item["subscription"] = self.create_subscription(PointCloud2,config.topic,
                              self.input_callback(key,item),CLOUD_QOS)
                except (ValueError, RuntimeError):
                    self.layers[name]["error"] = "subscription_failed"
            self.fact(name, "installed", topic=config.topic, expected_frame=config.expected_frame,
                      name=config.name, ttl_s=config.ttl_s)
            self.transition(name)

    def input_callback(self,key,item):
        def receive(msg):
            if self.inputs.get(key) is item:
                self.cloud(msg,key)
        return receive

    def cloud(self, msg, key=None):
        name="cloud" if key is None else "lidar."+key
        item=self.inputs.get(key)
        config=item["config"] if item else None
        surface=item["surface"] if item else self.surface
        source = msg.header.frame_id
        stamp = Time.from_msg(msg.header.stamp)
        try:
            self.valid_stamp(stamp.nanoseconds, config.ttl_s if config else TTL)
            if not source:
                raise ValueError("source_frame_missing")
            if config and config.expected_frame and source != config.expected_frame:
                raise ValueError("sensor_frame_mismatch")
            matrix = IDENTITY
            if source != self.frame:
                tf = self.buffer.lookup_transform(self.frame, source, stamp).transform
                t, q = tf.translation, tf.rotation
                matrix = rotation((t.x,t.y,t.z), (q.x,q.y,q.z,q.w))
            points = decode(msg, matrix)
        except TransformException:
            self.error(name, "transform_unavailable")
            return
        except ValueError as error:
            self.error(name, str(error))
            return
        with self.lock:
            if not surface.update(stamp.nanoseconds, points):
                return
            now = time.monotonic()
            layer = self.layers[name]
            layer.update(packet_received=now, packet_stamp_s=stamp.nanoseconds/1e9,
                         source_frame=source, ttl_s=config.ttl_s if config else TTL, sample_points=len(points))
            if points:
                layer.update(received=now, stamp_s=layer["packet_stamp_s"])
                layer.pop("error", None)
            else:
                layer["error"] = "no_valid_returns"
            self.transition(name)

    def pose(self, msg):
        ns = Time.from_msg(msg.header.stamp).nanoseconds
        p = msg.pose.pose.position
        try:
            self.valid_stamp(ns)
            if msg.header.frame_id != self.frame:
                raise ValueError("pose_frame_mismatch")
            if not all(math.isfinite(v) and abs(v) <= 1e6 for v in (p.x,p.y,p.z)):
                raise ValueError("pose_invalid")
        except ValueError as exc:
            self.error("pose", str(exc))
            return
        with self.lock:
            prior = self.layers["pose"]["stamp_s"]
            if prior is not None and ns/1e9 <= prior:
                return
            self.layers["pose"] = {"received":time.monotonic(), "xyz":[p.x,p.y,p.z],"stamp_s":ns/1e9}
            self.transition("pose")

    def scene(self, msg):
        try:
            if len(msg.data) > 100000:
                raise ValueError()
            data = json.loads(msg.data, parse_constant=lambda s: float(s))
            if data["frame"] != self.frame:
                self.error("scene","scene_frame_mismatch")
                return
            features = []
            for f in data["features"][:256]:
                xyz = [*f["xy"], f.get("base_z",0)]
                if len(xyz)!=3 or len(f["size"])!=3 or not all(math.isfinite(v) and abs(v)<=1e6 for v in xyz) or not all(
                        math.isfinite(v) and 0<v<=1e6 for v in f["size"]):
                    raise ValueError()
                features.append({**{k:str(f.get(k,""))[:96] for k in ("id","kind","label")}, "xyz":xyz, "size":f["size"]})
            zones = []
            for zone in data["zones"][:64]:
                bounds = zone["bounds"]
                if len(bounds)!=4 or not all(math.isfinite(v) and abs(v)<=1e6 for v in bounds):
                    raise ValueError()
                zones.append({"kind":str(zone.get("kind",""))[:96], "bounds":bounds})
            layer = {"received":time.monotonic(), "features":features, "zones":zones,
                     "source":str(data.get("source",""))[:128], "revision":str(data.get("revision",""))[:96]}
        except (AttributeError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            self.error("scene","scene_invalid")
            return
        with self.lock:
            self.layers["scene"] = layer
            self.transition("scene")

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            data = {"version":VERSION, "frame":self.frame, "generation":self.generation,
                    "environment":"simulation" if self.simulated else "hardware",
                    "cloud_sources":{}, "input_catalog_error":self.catalog_error}
            for name, layer in self.layers.items():
                view = {k:v for k,v in layer.items() if k not in ("received", "packet_received")}
                if "packet_received" in layer:
                    view["packet_age_s"] = round(now - layer["packet_received"], 2)
                view["status"] = self.status(layer, now)
                view["age_s"] = round(self.age(layer, now), 2) if "received" in layer else None
                if name.startswith("lidar."):
                    key=name.removeprefix("lidar.")
                    item=self.inputs[key]
                    cfg=item["config"]
                    view.update(name=cfg.name or key, topic=cfg.topic, expected_frame=cfg.expected_frame,
                                voxel_m=VOXEL_M, evictions=item["surface"].evictions, points=list(item["surface"].cells.values()))
                    data["cloud_sources"][key]=view
                else:
                    data[name] = view
            data["cloud"].update(topic=self.topic, voxel_m=VOXEL_M, evictions=self.surface.evictions,
                                 points=list(self.surface.cells.values()))
            return data


class ViewerServer(ThreadingHTTPServer):
    """Bound concurrent snapshot copies and clients holding connections open."""
    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(4)
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        static = {"/":("index.html","text/html; charset=utf-8"), "/map.js":("map.js","text/javascript"),
                  "/map.css":("map.css","text/css")}
        if path in ("/health", "/api/map"):
            obj = self.server.node.snapshot() if path=="/api/map" else self.server.node.identity
            raw, mime = json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",",":")).encode(), "application/json"
        elif path in static:
            file, mime = static[path]
            raw = (STATIC/file).read_bytes()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type",mime)
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        if path == "/health":
            self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError):
            pass


def main():
    rclpy.init()
    node = MapNode()
    node.identity = {"id":"map3d", "version":VERSION, "started_event":node.last_event["plugin"]}
    server = ViewerServer((os.getenv("EDGE_MAP3D_BIND","0.0.0.0"),int(os.getenv("EDGE_MAP3D_PORT","8770"))),Handler)
    server.node = node
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        node.fact("plugin","stopped")
        node.journal.close()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
