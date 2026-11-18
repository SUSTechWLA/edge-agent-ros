"""Fault injection against the actual ROS action gateway. Use an isolated DDS domain.

Default: synthetic ROS sensors in a network-isolated container. --gazebo: real
Gazebo sensors and a world-odometry witness; remap the gateway's /scan to
/motion_test/scan. Do not run this probe on physical hardware.
"""
import argparse
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import rclpy
from action_msgs.msg import GoalStatus
from ament_index_python.packages import get_package_prefix
from builtin_interfaces.msg import Time
from edge_interfaces.action import NavigateTo
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.action import ActionClient
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String


class MotionProbe:
    def __init__(self, gazebo):
        self.gazebo = gazebo
        self.node = rclpy.create_node("edge_motion_fault_probe")
        self.operation = ""
        self.fault = "healthy"
        self.clock_start = time.monotonic()
        self.epoch = 20
        self.fixed_stamp = None
        self.last_velocity = None
        self.truth = []
        self.braking_events = {}
        self.result_events = {}
        self.client = ActionClient(self.node, NavigateTo, "/edge/navigate_to")
        self.heartbeat = self.node.create_publisher(String, "/edge/active_operation", 1)
        self.scan = self.node.create_publisher(LaserScan, "/motion_test/scan", qos_profile_sensor_data)
        self.node.create_subscription(Twist, "/cmd_vel", self.velocity, 10)
        self.node.create_subscription(String, "/edge/events", self.on_event, 100)
        if gazebo:
            self.node.create_subscription(LaserScan, "/scan", self.relay, qos_profile_sensor_data)
            self.node.create_subscription(Odometry, "/ground_truth", self.world_pose, qos_profile_sensor_data)
        else:
            self.odom = self.node.create_publisher(Odometry, "/odom", qos_profile_sensor_data)
        self.node.create_timer(.03, self.tick)

    def velocity(self, msg):
        self.last_velocity = msg.linear.x

    def on_event(self, msg):
        event = json.loads(msg.data)
        if event["kind"] == "action.braking":
            self.braking_events[event["operation_id"]] = event
        if event["kind"] == "action.result":
            self.result_events[event["operation_id"]] = event

    def world_pose(self, msg):
        self.truth.append((time.monotonic(), msg.pose.pose.position.x, msg.pose.pose.position.y))

    def relay(self, scan):
        if self.fault == "drop":
            return
        if self.fault == "wrong_frame":
            scan.header.frame_id = "unregistered_sensor"
        if self.fault == "duplicate":
            if self.fixed_stamp is None:
                self.fixed_stamp = scan.header.stamp
            scan.header.stamp = self.fixed_stamp
        if self.fault == "clock_ahead":
            scan.header.stamp.sec += 8
        if self.fault == "bad_geometry":
            scan.angle_min = math.nan
        if self.fault in ("central_gap", "sparse_obstacle", "too_close", "bad_ranges"):
            for i in range(len(scan.ranges)):
                angle = scan.angle_min + i * scan.angle_increment
                if self.fault == "central_gap" and abs(angle) < .23:
                    scan.ranges[i] = math.nan
                elif self.fault == "sparse_obstacle":
                    scan.ranges[i] = .22 if abs(angle) < .006 else math.nan
                elif self.fault == "too_close" and abs(angle) < .006:
                    scan.ranges[i] = -math.inf
                elif self.fault == "bad_ranges":
                    scan.ranges[i] = scan.range_max + 50.
        self.scan.publish(scan)

    def tick(self):
        if self.operation:
            self.heartbeat.publish(String(data=self.operation))
        if self.gazebo:
            return
        elapsed = time.monotonic() - self.clock_start
        stamp = Time(sec=self.epoch + int(elapsed), nanosec=int(elapsed % 1 * 1e9))
        if self.fault == "rewind":
            stamp.sec -= 15
        odom = Odometry()
        odom.header.stamp = stamp
        odom.header.frame_id = "odom"
        odom.child_frame_id = "base_link"
        odom.pose.pose.orientation.w = 1.
        self.odom.publish(odom)
        scan = LaserScan()
        scan.header.frame_id = "laser_link"
        scan.header.stamp = Time(sec=stamp.sec, nanosec=stamp.nanosec)
        scan.angle_min, scan.angle_max, scan.angle_increment = -.5, .5, .02
        scan.range_min, scan.range_max = .1, 8.
        scan.ranges = [math.inf] * 51
        self.relay(scan)

    def wait(self, predicate, seconds, reason):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(self.node, timeout_sec=.025)
            if predicate():
                return
        raise TimeoutError(reason)

    def spin(self, seconds):
        until = time.monotonic() + seconds
        self.wait(lambda: time.monotonic() >= until, seconds+1, "timer")

    def send(self, op):
        self.operation = op
        future = self.client.send_goal_async(NavigateTo.Goal(operation_id=op, x=1.4))
        self.wait(future.done, 4, "action admission")
        return future.result()

    def exercise(self, fault, code):
        self.fault = "healthy"
        self.epoch += 20
        self.fixed_stamp = None
        self.last_velocity = None
        self.spin(.5)
        goal = self.send("test-" + fault)
        if not goal.accepted:
            raise RuntimeError("valid initial action refused")
        result = goal.get_result_async()
        self.wait(lambda: self.last_velocity is not None and self.last_velocity > .1, 4, "initial motion")
        if self.gazebo:
            self.wait(lambda: self.truth and self.truth[-1][1] > .08, 5, "no measured world motion")
        self.fault = fault
        start = time.monotonic()
        pose = self.truth[-1][1:] if self.truth else None
        record = {"case": fault, "expected_code": code, "brake_command_s": None}
        try:
            self.wait(lambda: self.last_velocity == 0., 2, "no brake command")
            record["brake_command_s"] = round(time.monotonic()-start, 4)
        except TimeoutError:
            pass
        # The original binary must be recoverable between negative cases.
        if record["brake_command_s"] is None:
            cancel = goal.cancel_goal_async()
            self.wait(cancel.done, 3, "cannot clean up original action")
        self.wait(result.done, 5, "no stop receipt")
        receipt = result.result()
        record.update(action_status=receipt.status, receipt=receipt.result.detail)
        record["passed"] = (receipt.status == GoalStatus.STATUS_ABORTED
                            and f"[code:{code}]" in receipt.result.detail
                            and record["brake_command_s"] is not None)
        if code in ("navigation.scan_time_mismatch", "navigation.scan_invalid", "navigation.path_blocked"):
            record["passed"] &= record["brake_command_s"] is not None and record["brake_command_s"] < .5
            event = self.braking_events.get(self.operation, {})
            data = event.get("data", {})
            record["braking_event"] = {"code": event.get("code"), "data": data}
            record["passed"] &= event.get("code") == code
            if fault == "central_gap":
                record["passed"] &= data.get("forward_gap_rad", 0) > .12
            if fault == "bad_geometry":
                record["passed"] &= data.get("scan_geometry_valid") is False
            if fault == "clock_ahead":
                record["passed"] &= abs(data.get("scan_stamp_ns", 0) - data.get("odom_stamp_ns", 0)) > 1e9
        self.wait(lambda: self.operation in self.result_events,2,"missing terminal gateway evidence")
        window=self.result_events[self.operation]["data"]["stop_evidence"]
        record["stop_evidence"]=window
        if fault!="rewind":
            record["passed"] &= window["accepted_samples"]>=window["quiet_samples"]>=3 and min(
                window["quiet_acquisition_ms"],window["quiet_arrival_ms"])>=300
        if fault == "rewind":
            record["passed"] &= window["quiet_samples"]==0 and window["last_odom_age_ms"]>=900
            self.fault = "healthy"
            self.spin(.4)
            record["new_action_refused"] = not self.send("post-reset").accepted
            record["passed"] &= record["new_action_refused"]
        if self.gazebo:
            self.spin(.45)
            samples = [p for p in self.truth if p[0] >= start]
            tail = [p for p in samples if p[0] >= samples[-1][0] - .3]
            travel = max(math.dist(pose, p[1:]) for p in samples)
            drift = max(math.dist(tail[0][1:], p[1:]) for p in tail)
            record.update(independent_topic="/ground_truth", samples=len(samples),
                          world_travel_m=round(travel, 4), last_300ms_drift_m=round(drift, 4))
            record["passed"] &= len(tail) >= 4 and travel < .20 and drift < .015
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gazebo", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    gateway = None
    report = {"utc": datetime.now(timezone.utc).isoformat(), "source_revision": os.getenv("EDGE_REVISION"),
              "mode": "gazebo" if args.gazebo else "ros_sensor_fixture", "cases": [], "passed": False}
    try:
        rclpy.init()
        probe = MotionProbe(args.gazebo)
        if not args.gazebo:
            exe = Path(get_package_prefix("edge_gateway"), "lib/edge_gateway/edge_gateway_node")
            gateway = subprocess.Popen([exe, "--ros-args", "-p", "require_scan:=true", "-r", "/scan:=/motion_test/scan"])
        if not probe.client.wait_for_server(timeout_sec=20):
            raise RuntimeError("missing gateway")
        if args.gazebo:
            probe.wait(lambda: len(probe.truth) >= 4, 30, "missing independent Gazebo truth")
        cases = {"central_gap": "navigation.scan_invalid"}
        if not args.gazebo:
            cases.update(sparse_obstacle="navigation.path_blocked", too_close="navigation.path_blocked",
                         bad_ranges="navigation.scan_invalid",
                         bad_geometry="navigation.scan_invalid", clock_ahead="navigation.scan_time_mismatch",
                         duplicate="navigation.scan_stale", drop="navigation.scan_stale",
                         wrong_frame="navigation.scan_stale", rewind="navigation.stop_unconfirmed")
        for fault, code in cases.items():
            report["cases"].append(probe.exercise(fault, code))
        report["passed"] = all(row["passed"] for row in report["cases"])
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if gateway:
            gateway.terminate()
            gateway.wait(timeout=8)
        rclpy.try_shutdown()
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
