"""Measure the isolated Gazebo rover's own laser at the registered campus test stations."""
from collections import deque
import json
import math
import os
from pathlib import Path
import re
import threading
import time

from gz.msgs10.boolean_pb2 import Boolean
from gz.msgs10.laserscan_pb2 import LaserScan
from gz.msgs10.pose_pb2 import Pose
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.transport13 import Node


def stamp(message):
    t = message.header.stamp
    return t.sec * 10**9 + t.nsec


def measure():
    if not re.fullmatch(r"campus_audit_[a-f0-9]+",os.environ.get("GZ_PARTITION","")):
        raise RuntimeError("station probes require an isolated campus audit partition")
    stations = json.loads(Path("/campus/overview/manifest.json").read_bytes())["sensor_stations"]
    if not {"open","cluttered","street"} <= {s["environment"] for s in stations}:
        raise RuntimeError("missing campus test environments")
    node, changed = Node(), threading.Condition()
    poses, scans = deque(maxlen=12), deque(maxlen=32)
    def on_pose(message):
        for pose in message.pose:
            if pose.name == "edge_rover":
                with changed:
                    poses.append((stamp(message),pose))
                    changed.notify_all()
                break
    def on_scan(message):
        with changed:
            scans.append(message)
            changed.notify_all()
    def until(check, name, timeout=18):
        with changed:
            deadline = time.monotonic()+timeout
            while not (result := check()):
                remaining = deadline - time.monotonic()
                if remaining<=0:
                    raise RuntimeError("no fresh station evidence: "+name)
                changed.wait(remaining)
            return result
    def place(p):
        success, result = node.request("/world/edge_lab/set_pose/blocking",p,Pose,Boolean,5000)
        if not success or not result.data:
            raise RuntimeError("Gazebo rejected test rover placement")
    if not node.subscribe(Pose_V,"/world/edge_lab/dynamic_pose/info",on_pose) or not node.subscribe(LaserScan,"/scan",on_scan):
        raise RuntimeError("could not subscribe to Gazebo ground truth and laser")
    original = until(lambda: poses[-1][1] if poses else None,"initial world pose")
    output = []
    try:
        for s in stations:
            x,y = s["xy"]
            pose = Pose(name="edge_rover")
            pose.position.x,pose.position.y,pose.position.z = x,y,.103
            pose.orientation.z,pose.orientation.w = math.sin(s["yaw"]/2),math.cos(s["yaw"]/2)
            with changed:
                after = poses[-1][0]
            place(pose)
            def arrived():
                t,p = poses[-1]
                return t if t>after and math.hypot(p.position.x-x,p.position.y-y)<.12 else None
            t = until(arrived,s["id"]+" position")
            def frames():
                fresh = [m for m in scans if stamp(m)>t+250_000_000 and m.count==len(m.ranges)==360
                         and math.hypot(m.world_pose.position.x-x,m.world_pose.position.y-y)<.3]
                return fresh[-3:] if len({stamp(m) for m in fresh})>=3 else None
            samples = until(frames,s["id"]+" laser")
            counts,nearest = [],[]
            for m in samples:
                hits = [v for v in m.ranges if math.isfinite(v) and m.range_min<=v<m.range_max-.02]
                counts.append(len(hits))
                nearest.append(round(min(hits),3) if hits else None)
            if s["environment"]=="open" and max(counts)>5:
                raise AssertionError(f"open field contains unexpected lidar obstacles: {counts}")
            if s["environment"]=="cluttered" and min(counts)<3:
                raise AssertionError(f"woodland lacks repeatable near-field returns: {counts}")
            output.append({"id":s["id"],"environment":s["environment"],"xy_m":[x,y],
                           "rays_per_scan":samples[-1].count,"range_max_m":samples[-1].range_max,
                           "stamps_ns":[stamp(m) for m in samples],"returns":counts,"nearest_m":nearest})
    finally:
        place(original)
        until(lambda: poses and math.hypot(poses[-1][1].position.x-original.position.x,
                     poses[-1][1].position.y-original.position.y)<.12,"original position restored")
        node.unsubscribe("/scan")
        node.unsubscribe("/world/edge_lab/dynamic_pose/info")
    return {"method":"independent station placement, original rover lidar, three post-placement scans",
            "original_xy_m":[original.position.x,original.position.y],"restored":True,"stations":output}


if __name__=="__main__":
    print(json.dumps(measure(),allow_nan=False))
