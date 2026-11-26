"""Place only an isolated test rover and align its original lidar with measured world poses."""
from collections import deque
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import weakref
import xml.etree.ElementTree as ET

from gz.msgs10.boolean_pb2 import Boolean
from gz.msgs10.laserscan_pb2 import LaserScan
from gz.msgs10.pose_pb2 import Pose
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.transport13 import Node


def stamp(message):
    t = message.header.stamp
    return t.sec * 10**9 + t.nsec


def xyz(pose):
    return [getattr(pose.position, k) for k in "xyz"]


def matches(seen, target):
    dot = sum(getattr(seen.orientation, k) * getattr(target.orientation, k) for k in "xyzw")
    return math.dist(xyz(seen), xyz(target)) < .12 and abs(dot) > math.cos(.06)


def mount_in_world(base, local):
    q = base.orientation
    x, y, z = local
    # Rotate the mount offset by the base quaternion, then translate.
    tx, ty, tz = 2 * (q.y * z - q.z * y), 2 * (q.z * x - q.x * z), 2 * (q.x * y - q.y * x)
    return [base.position.x + x + q.w * tx + q.y * tz - q.z * ty,
            base.position.y + y + q.w * ty + q.z * tx - q.x * tz,
            base.position.z + z + q.w * tz + q.x * ty - q.y * tx]


@contextmanager
def subscription(node, message_type, topic, receive):
    released = threading.Event()

    # gz-transport can hold its dispatch snapshot on a native reception thread
    # after unsubscribe returns. Let it release the Python callable before exit.
    def deliver(message):
        receive(message)

    weakref.finalize(deliver, released.set).atexit = False
    if not node.subscribe(message_type, topic, deliver):
        raise RuntimeError("could not subscribe to " + topic)
    del deliver  # The transport now owns the only remaining strong reference.
    try:
        yield
    finally:
        removed = node.unsubscribe(topic)
        if not released.wait(5):
            raise RuntimeError("Gazebo still retains subscription callback: " + topic)
        if not removed:
            raise RuntimeError("could not unsubscribe from " + topic)


def measure():
    partition = os.environ.get("GZ_PARTITION", "")
    if not re.fullmatch(r"campus_audit_[a-f0-9]+", partition):
        raise RuntimeError("station probes require an isolated campus audit partition")
    manifest = json.loads(Path("/campus/overview/manifest.json").read_bytes())
    revision = json.loads(Path("/campus/layout.json").read_bytes())["revision"]
    stations = manifest["sensor_stations"]
    if not {"open", "cluttered", "street"} <= {s["environment"] for s in stations}:
        raise RuntimeError("missing campus test environments")

    sdf = Path("/campus/world.sdf").read_bytes()
    sensor = ET.fromstring(sdf).find(".//model[@name='edge_rover']/link[@name='base_link']/sensor[@name='horizontal_lidar']")
    topic, frame = sensor.findtext("topic"), sensor.findtext("gz_frame_id")
    mount_xyz = list(map(float, sensor.findtext("pose").split()))[:3]
    expected_rays = int(sensor.findtext("lidar/scan/horizontal/samples"))
    node, changed = Node(), threading.Condition()
    poses, scans = deque(maxlen=240), deque(maxlen=48)

    def on_pose(message):
        for p in message.pose:
            if p.name == "edge_rover":
                with changed:
                    poses.append((stamp(message), p))
                    changed.notify_all()
                break

    def on_scan(message):
        with changed:
            scans.append(message)
            changed.notify_all()

    def until(check, name, timeout=18):
        with changed:
            deadline = time.monotonic() + timeout
            while not (value := check()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("no fresh station evidence: " + name)
                changed.wait(remaining)
            return value

    def place(target):
        with changed:
            previous = poses[-1][0]
        # Harmonic may execute the queued command but time out waiting for its
        # reply. The independently reported pose is the physical completion check.
        responded, result = node.request("/world/edge_lab/set_pose", target, Pose, Boolean, 2000)
        if responded and not result.data:
            raise RuntimeError("Gazebo rejected placement at " + str(xyz(target)))

        def at_target():
            t, seen = poses[-1]
            if t > previous and matches(seen, target):
                return {"stamp_ns": t, "world_xyz_m": xyz(seen), "rpc_acknowledged": responded}
        return until(at_target, "world pose for " + str(xyz(target)))

    def sample_frames(placement, target):
        # Both bracketing ground truth samples must put this robot at this site.
        # The LaserScan.world_pose field on Harmonic GPU lidar carries the mount
        # transform, so it cannot establish the location of the robot on its own.
        samples = {}
        history = list(poses)
        for m in scans:
            ts = stamp(m)
            if ts <= placement["stamp_ns"] + 250_000_000 or m.count != len(m.ranges) or m.count != expected_rays or m.frame != frame:
                continue
            for (ta, a), (tb, b) in zip(history, history[1:]):
                if ta <= ts <= tb and tb - ta <= 100_000_000 and matches(a, target) and matches(b, target):
                    raw = xyz(m.world_pose)
                    world_sensor = mount_in_world(a, mount_xyz)
                    if math.dist(raw, mount_xyz) < .025:
                        convention = "sensor_mount_local"
                    elif math.dist(raw, world_sensor) < .05:
                        convention = "world"
                    else:
                        raise AssertionError(f"unrecognised laser pose {raw} for {frame} at {xyz(a)}")
                    hits = [v for v in m.ranges if math.isfinite(v) and m.range_min <= v < m.range_max - .02]
                    samples[ts] = {"stamp_ns": ts, "pose_bracket_ns": [ta, tb], "rover_world_xyz_m": xyz(a),
                                   "reported_sensor_xyz_m": raw, "reported_pose_frame": convention,
                                   "sensor_world_xyz_m": world_sensor,
                                   "returns": len(hits), "nearest_m": round(min(hits), 3) if hits else None,
                                   "rays": m.count, "range_max_m": m.range_max}
                    break
        return list(samples.values())[-3:] if len(samples) >= 3 else None

    world_topic = "/world/edge_lab/dynamic_pose/info"
    with subscription(node, Pose_V, world_topic, on_pose), subscription(node, LaserScan, topic, on_scan):
        original = until(lambda: poses[-1][1] if poses else None, "initial world pose")
        output = []
        try:
            for s in stations:
                x, y = s["xy"]
                target = Pose(name="edge_rover")
                target.position.x, target.position.y, target.position.z = x, y, .103
                target.orientation.z, target.orientation.w = math.sin(s["yaw"] / 2), math.cos(s["yaw"] / 2)
                placed = place(target)
                samples = until(lambda: sample_frames(placed, target), s["id"] + " laser")
                counts = [f["returns"] for f in samples]
                if s["environment"] == "open" and max(counts) > 5:
                    raise AssertionError(f"open field contains unexpected lidar obstacles: {counts}")
                if s["environment"] in {"cluttered", "street"} and min(counts) < 3:
                    raise AssertionError(f"{s['environment']} lacks repeatable near-field returns: {counts}")
                output.append({"id": s["id"], "environment": s["environment"], "xy_m": [x, y], "placement": placed,
                               "rays_per_scan": expected_rays, "range_max_m": samples[-1]["range_max_m"],
                               "stamps_ns": [f["stamp_ns"] for f in samples], "returns": counts,
                               "nearest_m": [f["nearest_m"] for f in samples], "frames": samples})
        finally:
            restored = place(original)
    return {"method": "original rover laser, three distinct scans bracketed by fresh simulator world poses per station",
            "partition": partition, "world_sha256": hashlib.sha256(sdf).hexdigest(), "revision": revision,
            "sensor_frame": frame, "mount_xyz_m": mount_xyz, "original_xy_m": xyz(original)[:2],
            "restored": True, "restored_stamp_ns": restored["stamp_ns"], "restoration": restored, "stations": output}


if __name__ == "__main__":
    print(json.dumps(measure(), allow_nan=False))
