"""Simulation-only ROS observations and Core's bounded mission protocol."""
from collections import deque
import json
import math
import os
import time
from xml.etree.ElementTree import ParseError

import rclpy
from edge_interfaces.srv import CancelMission, GetMission, SubmitMission
from geometry_msgs.msg import Point
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import CameraInfo, Image, LaserScan
from std_msgs.msg import String
from std_srvs.srv import Trigger

from client.mission import MissionClient
from exploration.survey_freshness import observation_issue


def ns(stamp):
    return stamp.sec * 10**9 + stamp.nanosec


class Rover(MissionClient):
    def __init__(self, reset_name, semantic_name):
        super().__init__()
        self.grid = self.pose = self.scan = self.clock = None
        self.rewind = False
        self.observation_issue = None
        self.observations = deque(maxlen=96)
        self.motion_events = deque(maxlen=64)
        self.raw_scans = deque(maxlen=20)
        self.pose_samples = deque(maxlen=120)
        self.dynamic_samples = deque(maxlen=20)
        self.action_results = {}
        self.view = None
        if world := os.getenv("EDGE_SURVEY_VIEW_WORLD"):
            from exploration.local_observations import LocalObservations
            try:
                self.view = LocalObservations(world)
            except (OSError, ParseError, ValueError) as error:
                self.get_logger().warning(f"Local map observations unavailable: {error}")
        if self.view and self.view.camera_topic:
            self.create_subscription(Image, self.view.camera_topic+"/depth/image_raw",
                                     lambda msg: self.view.camera("depth", msg), qos_profile_sensor_data)
            self.create_subscription(CameraInfo, self.view.camera_topic+"/camera_info",
                                     lambda msg: self.view.camera("info", msg), qos_profile_sensor_data)
        self.reset = self.create_client(Trigger, reset_name)
        self.submit_path = self.create_client(SubmitMission, "/edge/submit_path")
        self.create_subscription(Clock, "/clock", self.on_clock, qos_profile_sensor_data)
        self.create_subscription(Odometry, "/edge/perception/pose", self.on_pose, qos_profile_sensor_data)
        self.create_subscription(OccupancyGrid, "/edge/perception/map", self.on_grid,
                                 QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.create_subscription(LaserScan, "/scan", self.on_scan, qos_profile_sensor_data)
        self.create_subscription(String, semantic_name, self.on_semantic, qos_profile_sensor_data)
        self.create_subscription(String, "/edge/events", self.on_event, 100)
        self.create_subscription(String, "/edge/perception/dynamic_obstacles", self.on_dynamic, 20)

    def spin(self):
        rclpy.spin_once(self, timeout_sec=.04)

    def on_clock(self, message):
        now = ns(message.clock)
        if self.clock and now < self.clock[0]:
            self.rewind = True
        self.clock = now, time.monotonic()

    def on_pose(self, message):
        if self.view:
            self.view.pose(message)
        if message.child_frame_id != "base_link":
            return
        p, q = message.pose.pose.position, message.pose.pose.orientation
        speed, yaw_rate = message.twist.twist.linear.x, message.twist.twist.angular.z
        yaw = math.atan2(2 * (q.w*q.z + q.x*q.y), 1 - 2 * (q.y*q.y + q.z*q.z))
        if all(map(math.isfinite, (p.x, p.y, yaw, speed, yaw_rate))):
            self.pose = ns(message.header.stamp), time.monotonic(), dict(
                x=p.x, y=p.y, yaw=yaw, frame=message.header.frame_id, speed=speed, yaw_rate=yaw_rate)
            self.pose_samples.append(dict(self.pose[2],stamp_ns=self.pose[0]))

    def on_grid(self, message):
        o = message.info.origin
        if abs(o.orientation.x) + abs(o.orientation.y) + abs(o.position.z) > 1e-5:
            return
        self.grid = ns(message.header.stamp), time.monotonic(), dict(
            frame=message.header.frame_id, width=message.info.width, height=message.info.height,
            resolution=message.info.resolution, cells=list(message.data),
            origin=[o.position.x, o.position.y, 2*math.atan2(o.orientation.z, o.orientation.w)])

    def on_scan(self, message):
        if message.header.frame_id != "laser_link" or not message.ranges:
            return
        forward = [v for i, v in enumerate(message.ranges)
                   if abs(message.angle_min+i*message.angle_increment) < .30 and
                   math.isfinite(v) and message.range_min <= v < message.range_max]
        self.scan = ns(message.header.stamp), time.monotonic(), min(forward, default=None)
        self.raw_scans.append(dict(stamp_ns=self.scan[0], frame=message.header.frame_id,
                                   angle_min=message.angle_min, angle_max=message.angle_max,
                                   angle_increment=message.angle_increment, range_min=message.range_min,
                                   range_max=message.range_max,
                                   ranges=[message.range_max if x == math.inf else x if math.isfinite(x) else None
                                           for x in message.ranges]))

    def on_semantic(self, message):
        if len(message.data) > 8000:
            return
        try:
            result = json.loads(message.data)
        except ValueError:
            return
        if isinstance(result, dict) and isinstance(result.get("stamp_s"), (int, float)):
            self.observations.append(result)

    def on_event(self, message):
        try:
            row = json.loads(message.data)
        except ValueError:
            return
        if isinstance(row, dict) and isinstance(row.get("operation_id"), str):
            self.motion_events.append(row)
            if row.get("module")=="edge.gateway" and row.get("kind")=="action.result":
                key = row["operation_id"]
                self.action_results[key] = row
                while len(self.action_results)>120:
                    self.action_results.pop(next(iter(self.action_results)))

    def on_dynamic(self, message):
        try:
            report = json.loads(message.data)
        except ValueError:
            return
        if isinstance(report,dict) and type(report.get("stamp_ns")) is int:
            self.dynamic_samples.append(report)

    def review_window(self):
        scans = {r["stamp_ns"]:r for r in self.raw_scans}
        poses = {r["stamp_ns"]:r for r in self.pose_samples}
        window = []
        for detection in self.dynamic_samples:
            scan,pose = scans.get(detection["stamp_ns"]),poses.get(detection.get("odom_stamp_ns"))
            if scan is not None and pose is not None:
                window.append(dict(scan=scan,pose=pose,dynamic=detection))
        return window[-12:], self.clock[0] if self.clock else 0

    def snapshot(self):
        self.observation_issue = observation_issue(time.monotonic(),self.rewind,self.clock,self.pose,self.grid,self.scan)
        if self.observation_issue:
            return None
        return {"stamp": self.grid[0], "grid": self.grid[2], "pose": self.pose[2], "scan_m": self.scan[2],
                "scan_stamp_ns":self.scan[0], "pose_stamp_ns":self.pose[0],
                "local_observation": self.view.snapshot(self.clock[0], self.raw_scans) if self.view else None}

    def begin(self):
        reply = self.call(self.reset, Trigger.Request(), time.monotonic()+2)
        if not reply.success:
            raise ValueError("mapping_epoch_unavailable")
        return int(reply.message)

    def dispatch(self, op, goal):
        return self.call(self.submit, SubmitMission.Request(mission_id=op, waypoints=[Point(x=goal[0], y=goal[1])]),
                         time.monotonic()+1.5)

    def status(self, op):
        return self.call(self.query, GetMission.Request(mission_id=op), time.monotonic()+1.5)

    def dispatch_path(self, op, points):
        waypoints = [Point(x=float(x), y=float(y)) for x, y in points]
        return self.call(self.submit_path, SubmitMission.Request(mission_id=op, waypoints=waypoints),
                         time.monotonic()+1.5)

    def stop(self, op):
        return self.call(self.cancel, CancelMission.Request(mission_id=op), time.monotonic()+1.5)
