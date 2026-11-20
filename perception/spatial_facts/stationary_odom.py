"""Read-only wheel odometry history and a bounded stationarity witness for sensor time windows."""
import argparse
from collections import deque
from itertools import combinations
import json
import math
import os
from pathlib import Path
import time
import uuid

from edge_contracts.image_frames import TOPIC
from edge_contracts.spatial_facts import identifier, ns, require
from perception.spatial_facts.artifact_ring import replace_bytes


RETAIN = 256
MAX_GAP_NS = 150_000_000
MAX_AGE_NS = 350_000_000
MIN_WINDOW_NS = 450_000_000
MAX_WINDOW_NS = 5_000_000_000
MAX_END_AGE_NS = 2_000_000_000


def _vector(values, size):
    require(isinstance(values, (tuple, list)) and len(values) == size and
            all(type(x) in (int, float) and math.isfinite(x) for x in values), "sensing.invalid_odometry")
    return list(values)


def _unit_quaternion(values):
    quaternion = _vector(values, 4)
    norm = math.hypot(*quaternion)
    require(abs(norm - 1.0) <= 0.01, "sensing.invalid_odometry_quaternion")
    return [n / norm for n in quaternion]


def _stamp(row):
    text = row.get("stamp_ns")
    require(isinstance(text, str) and text.isascii() and text.isdecimal() and 0 < len(text) <= 19
            and text[0] != "0" and ns(int(text)), "sensing.invalid_odometry_time")
    return int(text)


def _row(msg, cfg, now_ns):
    require(msg.header.frame_id == cfg["frame_id"] and msg.child_frame_id == cfg["child_frame_id"],
            "sensing.odometry_frame_mismatch")
    t = msg.header.stamp
    stamp = t.sec * 10**9 + t.nanosec
    require(ns(stamp) and stamp > 0 and 0 <= t.nanosec < 10**9 and 0 <= now_ns-stamp <= MAX_AGE_NS,
            "sensing.odometry_stale")
    pose, twist = msg.pose.pose, msg.twist.twist
    def xyz(v):
        return _vector([v.x, v.y, v.z], 3)
    q = pose.orientation
    return {"stamp_ns": str(stamp), "position_m": xyz(pose.position),
            "orientation_xyzw": _unit_quaternion([q.x, q.y, q.z, q.w]),
            "linear_mps": xyz(twist.linear), "angular_radps": xyz(twist.angular)}


class OdomHistory:
    def __init__(self, cfg):
        require(all(identifier(cfg.get(k)) for k in ("actor_ref", "source_id", "frame_id", "child_frame_id",
                                                     "localization_epoch")) and isinstance(cfg.get("topic"), str)
                and TOPIC.fullmatch(cfg["topic"]) and cfg.get("clock_id") == "unix",
                "sensing.invalid_odometry_binding")
        self.cfg = cfg
        self.samples = deque(maxlen=RETAIN)
        self.epoch = uuid.uuid4().hex

    def reset(self):
        self.samples.clear()
        self.epoch = uuid.uuid4().hex

    def append(self, msg, now_ns):
        try:
            row = _row(msg, self.cfg, now_ns)
        except ValueError:
            self.reset()
            raise
        previous = self.samples[-1] if self.samples else None
        if previous:
            gap = int(row["stamp_ns"]) - int(previous["stamp_ns"])
            if gap == 0:
                return False
            if gap < 0 or gap > MAX_GAP_NS:
                self.reset()
                previous = None
        q = row["orientation_xyzw"]
        if previous and sum(a*b for a, b in zip(q, previous["orientation_xyzw"])) < 0:
            q = row["orientation_xyzw"] = [-n for n in q]
        x, y, z, w = q
        yaw = math.atan2(2*(w*z+x*y), 1-2*(y*y+z*z))
        if previous:
            last = previous["yaw_unwrapped_rad"]
            yaw = last + math.remainder(yaw-last, 2*math.pi)
        row["yaw_unwrapped_rad"] = yaw
        self.samples.append(row)
        return True

    def snapshot(self):
        keys = ("actor_ref", "source_id", "clock_id", "frame_id", "child_frame_id", "topic", "localization_epoch")
        return {"schema_version": 1, **{k:self.cfg[k] for k in keys}, "epoch": self.epoch,
                "samples": list(self.samples)}


def stationary_window(history, start_ns, end_ns, *, now_ns, actor_ref, frame_id, child_frame_id,
                      source_id="wheel.odometry", linear_limit_mps=0.025, angular_limit_radps=0.05,
                      displacement_limit_m=0.015, rotation_limit_rad=0.035):
    """Bracket the acquisition interval with fresh wheel samples and check translation, rotation and twist.

    Short acquisitions include preceding wheel evidence to cover at least 450 ms. Callers must wait
    for the first wheel sample at or after end_ns. The consumer supplies its actual Unix clock.
    """
    require(isinstance(history, dict) and history.get("schema_version") == 1
            and all(identifier(v) and history.get(k) == v for k, v in
                    (("actor_ref", actor_ref), ("source_id", source_id), ("frame_id", frame_id),
                     ("child_frame_id", child_frame_id))) and history.get("clock_id") == "unix"
            and all(identifier(history.get(k)) for k in ("epoch", "localization_epoch"))
            and isinstance(history.get("topic"), str) and TOPIC.fullmatch(history["topic"]),
            "sensing.odometry_binding_mismatch")
    require(all(ns(n) and n > 0 for n in (start_ns, end_ns, now_ns)) and start_ns <= end_ns <= now_ns
            and end_ns - start_ns <= MAX_WINDOW_NS, "sensing.odometry_interval_invalid")
    require(now_ns-end_ns <= MAX_END_AGE_NS, "sensing.odometry_stale")
    limits = {"linear_mps": linear_limit_mps, "angular_radps": angular_limit_radps,
              "displacement_m": displacement_limit_m, "rotation_rad": rotation_limit_rad}
    require(all(type(n) in (int, float) and math.isfinite(n) and n > 0 for n in limits.values()),
            "sensing.invalid_odometry_limits")
    rows = history.get("samples")
    require(isinstance(rows, list) and 2 <= len(rows) <= RETAIN and all(isinstance(r, dict) for r in rows),
            "sensing.odometry_coverage_missing")
    stamps = [_stamp(r) for r in rows]
    require(0 <= now_ns-stamps[-1] <= MAX_AGE_NS, "sensing.odometry_stale")
    require(all(0 < b-a <= MAX_GAP_NS for a, b in zip(stamps, stamps[1:])), "sensing.odometry_gap")
    earliest = min(start_ns, end_ns-MIN_WINDOW_NS)
    left = next((i for i in range(len(stamps)-1, -1, -1) if stamps[i] <= earliest), None)
    right = next((i for i, n in enumerate(stamps) if n >= end_ns), None)
    require(left is not None and right is not None, "sensing.odometry_coverage_missing")
    selected = rows[left:right+1]
    q = [_unit_quaternion(row.get("orientation_xyzw")) for row in selected]
    p = [_vector(row.get("position_m"), 3) for row in selected]
    speeds = [math.hypot(*_vector(row.get("linear_mps"), 3)) for row in selected]
    spin = [math.hypot(*_vector(row.get("angular_radps"), 3)) for row in selected]
    metrics = {"linear_mps": max(speeds), "angular_radps": max(spin),
               "displacement_m": max(math.dist(a, b) for a, b in combinations(p, 2)),
               "rotation_rad": max(2*math.acos(min(1., abs(sum(x*y for x, y in zip(a, b)))))
                                   for a, b in combinations(q, 2))}
    require(all(metrics[k] <= limit for k, limit in limits.items()), "sensing.odometry_motion_detected")
    return {"schema_version": 1, "stationary": True,
            **{k:history[k] for k in ("actor_ref", "source_id", "frame_id", "child_frame_id",
                                      "clock_id", "epoch", "localization_epoch", "topic")},
            "first_ns": stamps[left], "last_ns": stamps[right], "sample_count": len(selected),
            "max_gap_ns": max(b-a for a, b in zip(stamps[left:right], stamps[left+1:right+1])),
            "limits": limits, **{"max_"+k: v for k, v in metrics.items()}}


def main():
    import fcntl
    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.validate_full_topic_name import validate_full_topic_name

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_bytes())
    history = OdomHistory(cfg)
    validate_full_topic_name(cfg["topic"])
    args.output.mkdir(parents=True, exist_ok=True)
    require(args.output.resolve(strict=True) == args.output.absolute(), "sensing.invalid_odometry_directory")
    lock = os.open(args.output, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    path = args.output / "history.json"
    path.unlink(missing_ok=True)
    rclpy.init()
    node = rclpy.create_node("edge_stationary_odometry_reader", enable_rosout=False, start_parameter_services=False)

    def receive(msg):
        try:
            if history.append(msg, time.time_ns()):
                replace_bytes(path, json.dumps(history.snapshot(), allow_nan=False, separators=(",", ":")).encode())
        except (ValueError, OSError):
            path.unlink(missing_ok=True)

    try:
        node.create_subscription(Odometry, cfg["topic"], receive, qos_profile_sensor_data)
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
        os.close(lock)


if __name__ == "__main__":
    main()
