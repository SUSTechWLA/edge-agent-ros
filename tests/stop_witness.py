"""Independent Gazebo world-pose witness started before a fault is injected."""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data


class StopWitness(Node):
    def __init__(self, kind):
        super().__init__("stop_witness_" + kind)
        self.origin = time.monotonic()
        self.rows = []
        self.duplicates = 0
        self.create_subscription(Odometry, "/ground_truth", self.record, qos_profile_sensor_data)

    def elapsed(self):
        return time.monotonic() - self.origin

    def record(self, msg):
        stamp = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
        if self.rows and stamp == self.rows[-1]["sim_ns"]:
            self.duplicates += 1
            return
        if self.rows and stamp < self.rows[-1]["sim_ns"]:
            raise ValueError("world clock regressed during fault witness")
        p = msg.pose.pose.position
        v = msg.twist.twist
        speed = math.hypot(v.linear.x, v.linear.y)
        if not all(math.isfinite(value) for value in (p.x, p.y, speed, v.angular.z)):
            raise ValueError("nonfinite world-pose measurement")
        self.rows.append(dict(t=self.elapsed(), sim_ns=stamp, x=p.x, y=p.y,
                              speed=speed, yaw_rate=v.angular.z))

    def run(self, ready, inject, limit_s, limit_m, meas):
        if inject.exists():
            raise RuntimeError("injection marker predates witness startup")
        # Marker detection time is bounded by the last filesystem poll; use the
        # earlier timestamp to conservatively include the host's kill dispatch.
        announced = False
        while True:
            before_check = self.elapsed()
            if inject.exists():
                if not announced:
                    raise RuntimeError("fault injected before fresh world truth was available")
                detected = self.elapsed()
                break
            last_absent = before_check
            if not announced and len(self.rows) >= 3 and before_check - self.rows[-1]["t"] < 0.2:
                ready.touch()
                announced = True
            if before_check >= 60:
                raise TimeoutError("no injection marker within 60 seconds")
            rclpy.spin_once(self, timeout_sec=0.015)

        meas.update(marker_last_absent_s=last_absent, marker_seen_s=detected)
        before = [i for i, row in enumerate(self.rows) if row["t"] <= last_absent]
        if not before:
            raise AssertionError("missing pre-injection truth")
        anchor = before[-1]
        recent = [row for row in self.rows[:anchor + 1] if last_absent - row["t"] <= 0.25]
        if len(recent) < 2 or last_absent - recent[-1]["t"] > 0.15:
            raise AssertionError("pre-injection world poses were stale")
        recent_motion = math.hypot(recent[-1]["x"] - recent[0]["x"],
                                   recent[-1]["y"] - recent[0]["y"])
        meas.update(anchor_index=anchor, pre_speed_mps=recent[-1]["speed"],
                    pre_window_displacement_m=recent_motion)
        if recent[-1]["speed"] < 0.08 or recent_motion < 0.005:
            raise AssertionError("pre-injection truth did not establish linear motion")

        index = anchor + 1
        still_from = None
        length = 0.0
        while self.elapsed() <= last_absent + limit_s + 0.5:
            while index < len(self.rows):
                a, b = self.rows[index - 1], self.rows[index]
                ds = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
                ds_sim = (b["sim_ns"] - a["sim_ns"]) / 1e9
                if b["t"] - a["t"] > 0.25 or ds_sim > 0.25:
                    raise AssertionError("gap in independent truth across the stopping interval")
                length += ds
                meas["path_m"] = length
                if length > limit_m:
                    raise AssertionError("stopping path exceeded distance budget")
                stable = (b["speed"] <= 0.04 and abs(b["yaw_rate"]) <= 0.10 and
                          ds / ds_sim <= 0.05 and b["t"] >= detected)
                if not stable:
                    still_from = None
                elif still_from is None:
                    still_from = index
                else:
                    first = self.rows[still_from]
                    if b["t"] - first["t"] >= 0.4 and b["sim_ns"] - first["sim_ns"] >= 400_000_000:
                        duration = b["t"] - last_absent
                        meas.update(first_still_index=still_from, confirmed_index=index,
                                    first_still_s=first["t"] - last_absent, confirmed_s=duration,
                                    stable_sample_count=index - still_from + 1,
                                    stable_world_s=(b["sim_ns"] - first["sim_ns"]) / 1e9)
                        if duration > limit_s:
                            raise AssertionError("confirmed stop exceeded time budget")
                        return
                index += 1
            rclpy.spin_once(self, timeout_sec=0.025)
        raise TimeoutError("continuous fresh stationary truth was not observed before deadline")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("crash", "bridge"))
    kind = parser.parse_args().kind
    limit_s, limit_m = {"crash": (3.5, 0.95), "bridge": (2.0, 0.45)}[kind]
    out = Path("/evidence")
    out.mkdir(parents=True, exist_ok=True)
    prefix = out / ("watch-" + kind)
    rclpy.init()
    witness = StopWitness(kind)
    meas = {}
    result = dict(kind=kind, passed=False, meas=meas, limit_s=limit_s, limit_m=limit_m,
                  clock="seconds since witness startup, monotonic; Gazebo stamp in sim_ns")
    try:
        witness.run(prefix.with_suffix(".ready"), prefix.with_suffix(".inject"), limit_s, limit_m, meas)
        result["passed"] = True
    except Exception as ex:
        result["error"] = str(ex)
    finally:
        result["samples"] = witness.rows
        result["duplicate_samples_ignored"] = witness.duplicates
        tmp = prefix.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        tmp.replace(prefix.with_suffix(".json"))
        print(json.dumps({k: v for k, v in result.items() if k != "samples"}), flush=True)
        witness.destroy_node()
        rclpy.shutdown()
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
