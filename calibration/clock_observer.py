"""Read-only acquisition of independently timestamped board-plane time series."""
from collections import deque
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Condition, Lock, Thread
import time
import uuid

import numpy as np

from calibration.board_provider import Handler


def validate_environment(profile):
    if profile["environment"] == "simulation":
        return
    if profile["environment"] != "hardware" or os.getenv("EDGE_CALIBRATION_READONLY_HARDWARE") != "1":
        raise ValueError("hardware recording requires explicit read-only commissioning")
    from calibration.hardware_profile import validate_hardware_profile
    validate_hardware_profile(profile)


def point_window(header_ns, xyz, timestamps, config):
    """Fit one short scan slice to bound within-scan motion of the target."""
    raw = np.asarray(timestamps).reshape(-1)
    if not raw.size or len(xyz) != len(raw) or not np.isfinite(raw).all() or np.any(raw < 0):
        raise ValueError("invalid per-point acquisition times")
    if config["unit"] not in ("s","ns") or config["mode"] not in ("absolute","relative"):
        raise ValueError("invalid per-point time convention")
    if np.issubdtype(raw.dtype, np.floating):
        precision_ns = float(np.spacing(np.max(raw))) * (1e9 if config["unit"] == "s" else 1)
        if precision_ns > 4_000_000:
            raise ValueError("per-point time precision insufficient for scan slice")
    scaled = raw.astype(np.longdouble) * (1_000_000_000 if config["unit"] == "s" else 1)
    if np.any(scaled > np.iinfo(np.int64).max - 1_000_000_000):
        raise ValueError("point acquisition times out of range")
    stamps = np.rint(scaled).astype(np.int64)
    if config["mode"] == "relative":
        if stamps.max() > 500_000_000:
            raise ValueError("relative point time exceeds scan duration")
        stamps = stamps + header_ns
    if np.max(np.abs(stamps - header_ns)) > 500_000_000 or stamps.max() == stamps.min():
        raise ValueError("point time not resolved in LiDAR clock")
    midpoint = int(stamps.min()) + (int(stamps.max()) - int(stamps.min())) // 2
    selected = np.abs(stamps - midpoint) <= 4_000_000
    return midpoint, xyz[selected]


class Epoch:
    def __init__(self, roles):
        self.roles = set(roles)
        self.restart()

    def restart(self, reason="observer_started"):
        self.id = uuid.uuid4().hex
        self.reason = reason
        self.stamps = {}
        self.seen = {}
        self.publishers = {}

    def update(self, key, stamp, publisher, now):
        if key in self.stamps:
            delta=stamp-self.stamps[key]
            if delta<0 or delta>2_000_000_000:
                self.restart(f"{key}: acquisition_stamp_discontinuity ({delta} ns)")
            elif self.publishers[key] != publisher:
                self.restart(f"{key}: publisher_replaced")
            elif now-self.seen[key]>3:
                self.restart(f"{key}: reception_interruption ({now-self.seen[key]:.3f} s)")
        if self.stamps.get(key) == stamp:
            return False
        self.stamps[key], self.seen[key], self.publishers[key] = stamp, now, publisher
        return True

    def status(self):
        fresh = set(self.seen) == self.roles and all(time.monotonic()-t <= 3 for t in self.seen.values())
        return {"clock_epoch": self.id, "streams_fresh": fresh, "epoch_reason":self.reason}


class ClockObserver:
    def __init__(self, profile):
        import cv2
        import rclpy
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import CameraInfo, Image, PointCloud2

        validate_environment(profile)
        cv2.setNumThreads(1)
        self.profile = profile
        self.lidars = {s["id"]: s for s in profile["lidars"]}
        cam = profile["camera"]
        self.hardware = profile["environment"] == "hardware"
        channels = {"camera":(Image,cam["topic"],cam["frame"]), "info":(CameraInfo,cam["info_topic"],cam["frame"])}
        channels.update({k:(PointCloud2,v["topic"],v["frame"]) for k,v in self.lidars.items()})
        topics = json.loads(os.getenv("EDGE_CLOCK_TOPICS_JSON","{}"))
        if self.hardware and topics:
            raise ValueError("hardware clock topics must come from the commissioned sensor profile")
        if set(topics)-set(channels) or len({topics.get(k,v[1]) for k,v in channels.items()})!=len(channels):
            raise ValueError("invalid clock stream binding")
        self.topics = {k:topics.get(k,v[1]) for k,v in channels.items()}
        self.epoch = Epoch(channels)
        self.cv = Condition()
        self.run_lock = Lock()
        self.window = None
        self.info = None
        self.directory = Path(os.environ.get("EDGE_TARGET_DETECTORS", Path(__file__).parent/"station/recognizers"))
        self.node = rclpy.create_node("edge_clock_observer")
        for key,(kind,_,frame) in channels.items():
            self.node.create_subscription(kind,self.topics[key],self.callback(key,frame),qos_profile_sensor_data)
        Thread(target=rclpy.spin,args=(self.node,),daemon=True).start()

    def callback(self, key, frame):
        def receive(msg):
            with self.cv:
                # Jazzy MessageInfo doesn't expose the writer GID. Resolve the
                # single DDS publisher for each bound topic from the graph.
                publishers = self.node.get_publishers_info_by_topic(self.topics[key])
                if msg.header.frame_id != frame or len(publishers)!=1:
                    self.epoch.restart(f"{key}: invalid frame or publisher count ({len(publishers)})")
                    self.info = None
                    self.cv.notify_all()
                    return
                ns = msg.header.stamp.sec*10**9 + msg.header.stamp.nanosec
                prev = self.epoch.id
                if not self.epoch.update(key,ns,bytes(publishers[0].endpoint_gid),time.monotonic()):
                    return
                if self.epoch.id != prev:
                    self.info = None
                w = self.window
                if key == "info":
                    self.info = (ns,msg)
                elif w and w["epoch"]==self.epoch.id and key in w["pending"]:
                    w["pending"][key].append((ns,msg,self.info if key=="camera" else None,time.monotonic_ns()))
                self.cv.notify_all()
        return receive

    def measure(self, key, stamp, msg, info):
        from calibration.board_solver import observe_lidar
        from calibration.board_ros import decode_cloud
        from perception.target_detection import recognize
        profile = self.profile["solver"]
        if key=="camera":
            if not info or abs(stamp-info[0])>300_000_000:
                return stamp,None,None
            from calibration.camera_geometry import decode_rgb
            rgb,k=decode_rgb(msg,info[1],self.profile["camera"])
            result = recognize("checkerboard",rgb,k,{"solver":profile},self.directory)
            matches = result["detections"]
            plane = matches[0]["features"]["checkerboard_plane"] if len(matches)==1 else None
            return stamp,plane,result["detector"]
        cfg = self.lidars[key].get("clock_point_time")
        xyz, times = decode_cloud(msg, cfg["field"] if cfg else None)
        if cfg:
            stamp,xyz = point_window(stamp,xyz,times,cfg)
        return stamp,observe_lidar(xyz,profile),None

    def worker(self, w, key):
        try:
            while True:
                with self.cv:
                    self.cv.wait_for(lambda: w["stop"] or w["pending"][key])
                    if w["stop"]:
                        return
                    entry = w["pending"][key].popleft()
                stamp,plane,detector = self.measure(key,*entry[:3])
                with self.cv:
                    if w["stop"]:
                        return
                    if detector:
                        if w.setdefault("detector",detector)!=detector:
                            raise ValueError("checkerboard recognizer changed during acquisition")
                    if plane:
                        plane = {k:plane[k] for k in ("normal","center_m","offset_m")}
                        w["traces"][key].append({"stamp_ns":stamp,"received_monotonic_ns":entry[3],"plane":plane})
                    self.cv.notify_all()
        except Exception as e:
            with self.cv:
                w["error"] = f"{key}: {e}"
                self.cv.notify_all()

    def capture(self):
        if not self.run_lock.acquire(blocking=False):
            raise ValueError("clock acquisition busy")
        keys = ["camera",*self.lidars]
        threads = []
        try:
            with self.cv:
                if not self.cv.wait_for(lambda:self.epoch.status()["streams_fresh"],timeout=8):
                    raise ValueError("missing fresh independent timestamp streams")
                w = dict(epoch=self.epoch.id,stop=False,pending={k:deque(maxlen=3) for k in keys},
                         traces={k:[] for k in keys})
                self.window = w
                initial = dict(self.epoch.stamps)
            for key in keys:
                thread = Thread(target=self.worker,args=(w,key),daemon=True)
                thread.start()
                threads.append(thread)
            with self.cv:
                def finished():
                    return w.get("error") or self.epoch.id!=w["epoch"] or all(
                        self.epoch.stamps[k]-initial[k]>=12_000_000_000 for k in keys)
                if not self.cv.wait_for(finished,timeout=110):
                    raise ValueError("clock trace acquisition timed out")
                if w.get("error") or self.epoch.id!=w["epoch"]:
                    raise ValueError(w.get("error","clock source restarted during acquisition"))
                if any(len(rows)<3 for rows in w["traces"].values()):
                    raise ValueError("insufficient board planes in moving target window")
                return dict(clock_epoch=w["epoch"],topics=self.topics,traces=w["traces"],detector=w["detector"],
                            exposure_bound_ns=self.profile["camera"].get("clock_exposure_bound_ns",0),
                            lidar_slice_bound_ns={k:(4_000_000 if v.get("clock_point_time") else 0)
                                                  for k,v in self.lidars.items()})
        finally:
            with self.cv:
                if self.window:
                    self.window["stop"]=True
                self.window=None
                self.cv.notify_all()
            for thread in threads:
                thread.join(3)
            self.run_lock.release()

    def dispatch(self,path,request):
        if request:
            raise ValueError("clock acquisition uses only installed bindings")
        if path=="/epoch":
            with self.cv:
                return self.epoch.status()
        if path=="/capture":
            return self.capture()
        raise ValueError("unknown clock observer capability")


def main():
    import rclpy
    profile = json.loads(Path(os.environ["EDGE_CALIBRATION_PROFILE"]).read_bytes())
    rclpy.init()
    source = ClockObserver(profile)
    try:
        with ThreadingHTTPServer((os.getenv("EDGE_CALIBRATION_HTTP_BIND","0.0.0.0"),8143),Handler) as server:
            server.dispatch=source.dispatch
            server.serve_forever()
    finally:
        rclpy.shutdown()


if __name__=="__main__":
    main()
