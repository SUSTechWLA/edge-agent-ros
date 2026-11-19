"""Planar wheel/gyro odometry and inverse sensor model for a mounted 2D lidar."""
import json
import math
import time
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass

import rclpy
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import Imu, LaserScan
from std_msgs.msg import String


def seconds(stamp):
    return stamp.sec + stamp.nanosec * 1e-9


def wrap(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def wheel_yaw(q):
    norm_squared = q.x*q.x + q.y*q.y + q.z*q.z + q.w*q.w
    if not math.isfinite(norm_squared) or abs(norm_squared - 1) > 0.05:
        return None
    return math.atan2(2*(q.w*q.z + q.x*q.y), 1-2*(q.y*q.y + q.z*q.z))


@dataclass(frozen=True)
class Pose:
    t: float
    x: float
    y: float
    yaw: float


class Grid:
    """Fixed local odom grid; one free/occupied vote per cell per scan."""
    def __init__(self, size, resolution):
        self.size = size
        self.res = resolution
        self.origin = -size * resolution / 2
        self.score = [0] * (size * size)
        self.data = [-1] * (size * size)

    def cell(self, x, y):
        return math.floor((x-self.origin)/self.res), math.floor((y-self.origin)/self.res)

    def ray(self, x, y, ex, ey):
        dx, dy = abs(ex-x), -abs(ey-y)
        sx, sy = (1 if x < ex else -1), (1 if y < ey else -1)
        error = dx+dy
        while 0 <= x < self.size and 0 <= y < self.size:
            yield y*self.size+x
            if x == ex and y == ey:
                return
            doubled = 2*error
            if doubled >= dy:
                error += dy
                x += sx
            if doubled <= dx:
                error += dx
                y += sy

    def integrate(self, scan, pose, mount, clear_infinite):
        mx, my, turn = mount
        c, s = math.cos(pose.yaw), math.sin(pose.yaw)
        ox, oy = pose.x+c*mx-s*my, pose.y+s*mx+c*my
        start = self.cell(ox, oy)
        cleared, occupied = set(), set()
        for i, value in enumerate(scan.ranges):
            hit = math.isfinite(value) and scan.range_min <= value < scan.range_max
            if not hit and not (clear_infinite and (value == math.inf or value == scan.range_max)):
                continue
            length = value if hit else scan.range_max
            angle = pose.yaw+turn+scan.angle_min+i*scan.angle_increment
            end = self.cell(ox+length*math.cos(angle), oy+length*math.sin(angle))
            cleared.update(self.ray(*start, *end))
            if hit and 0 <= end[0] < self.size and 0 <= end[1] < self.size:
                occupied.add(end[1]*self.size+end[0])
        for index in cleared - occupied:
            self.score[index] = max(-4, self.score[index]-1)
            self.data[index] = 100 if self.score[index] > 0 else 0
        for index in occupied:
            self.score[index] = min(4, self.score[index]+3)
            self.data[index] = 100 if self.score[index] > 0 else 0
        return bool(cleared or occupied)


class Perception(Node):
    def __init__(self):
        super().__init__("edge_perception")
        def param(key, default):
            return self.declare_parameter(key, default).value
        self.odom_frame = param("odom_frame", "odom")
        self.base_frame = param("base_frame", "base_link")
        self.imu_frame = param("imu_frame", "imu_link")
        self.laser_frame = param("laser_frame", "laser_link")
        self.mount = tuple(param(name, v) for name, v in
                           (("lidar_x", 0.08), ("lidar_y", 0.0), ("lidar_yaw", 0.0)))
        self.clear_infinite = param("clear_positive_infinity", True)
        self.size = param("grid_cells", 120)
        self.res = param("resolution_m", 0.10)
        if not 8 <= self.size <= 512 or not math.isfinite(self.res) or self.res <= 0:
            raise ValueError("grid_cells must be in [8,512] and resolution_m positive")
        if not all(math.isfinite(v) for v in self.mount):
            raise ValueError("lidar mount must have finite x, y, yaw")
        self.pose_pub = self.create_publisher(Odometry, param("pose_topic", "/edge/perception/pose"), 1)
        map_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.map_pub = self.create_publisher(OccupancyGrid, param("map_topic", "/edge/perception/map"), map_qos)
        self.status_pub = self.create_publisher(String, param("status_topic", "/edge/perception/status"), 1)
        self.last_clock = None
        self.reset("waiting for fresh wheel odometry")
        self.create_subscription(Imu, param("imu_topic", "/imu"), self.on_imu, qos_profile_sensor_data)
        self.create_subscription(Odometry, param("odom_topic", "/odom"), self.on_odom, qos_profile_sensor_data)
        self.create_subscription(LaserScan, param("scan_topic", "/scan"), self.on_scan, qos_profile_sensor_data)
        self.create_subscription(Clock, param("clock_topic", "/clock"), self.on_clock, qos_profile_sensor_data)
        self.create_timer(0.1, self.process_scans)
        self.create_timer(1, self.publish_summary)

    def reset(self, reason):
        self.gyros = deque(maxlen=256)
        self.poses = deque(maxlen=256)
        self.pending = deque()
        self.last_odom = None
        self.odom_at = None
        self.last_scan_stamp = None
        self.mapped_stamp = None
        self.published_map_stamp = None
        self.grid = Grid(self.size, self.res)
        self.mode, self.reason = "waiting", reason
        self.matched, self.dropped = 0, 0
        self.scan_reason = "waiting for scan"

    def on_clock(self, msg):
        now = seconds(msg.clock)
        if self.last_clock is not None and now < self.last_clock:
            self.reset("simulation clock rewound; map and sensor history cleared")
            self.publish_grid(msg.clock)
        self.last_clock = now

    def future_stamp(self, t):
        # Allow normal DDS delivery skew between /clock and sensor writers.
        return self.last_clock is not None and t > self.last_clock + 1.0

    def on_imu(self, msg):
        t = seconds(msg.header.stamp)
        wz = msg.angular_velocity.z
        if msg.header.frame_id != self.imu_frame or not math.isfinite(wz) or self.future_stamp(t):
            return
        if msg.angular_velocity_covariance[0] < 0 or (self.gyros and t <= self.gyros[-1][0]):
            return
        self.gyros.append((t, wz))

    def gyro_turn(self, begin, end):
        # Integrate sampled z rate using its capture time, holding each sample
        # only until the next sample. Refuse gaps longer than 100 milliseconds.
        previous = None
        cursor = begin
        integral = 0.0
        for t, rate in self.gyros:
            if t <= begin:
                previous = (t, rate)
                continue
            if t > end:
                break
            if previous is None or t - previous[0] > 0.1:
                return None
            integral += previous[1]*(t-cursor)
            previous, cursor = (t, rate), t
        if previous is None or end - previous[0] > 0.1:
            return None
        return integral + previous[1]*(end-cursor)

    def on_odom(self, msg):
        t = seconds(msg.header.stamp)
        p = msg.pose.pose.position
        wy = wheel_yaw(msg.pose.pose.orientation)
        if (msg.header.frame_id != self.odom_frame or msg.child_frame_id != self.base_frame or
                wy is None or not math.isfinite(p.x) or not math.isfinite(p.y) or self.future_stamp(t) or
                (self.last_odom and t <= self.last_odom.t)):
            return
        new = Pose(t, p.x, p.y, wy)
        old = self.last_odom
        dt = t-old.t if old else 0.0
        turn = self.gyro_turn(old.t, t) if old and dt < 0.5 else None
        self.mode = "wheel_only" if turn is None else "wheel_imu"
        self.reason = "wheel odometry with no aligned gyro interval" if turn is None else "wheel displacement and gyro yaw with wheel heading correction"
        if old and dt < 0.5:
            last = self.poses[-1]
            predicted = last.yaw+turn if turn is not None else last.yaw+wrap(wy-old.yaw)
            # Time-based correction toward wheel heading, independent of update rate.
            fyaw = wrap(predicted+(1-math.exp(-dt/1.0))*wrap(wy-predicted))
            wheel_mid = old.yaw + 0.5*wrap(wy-old.yaw)
            fused_mid = last.yaw + 0.5*wrap(fyaw-last.yaw)
            angle = wrap(fused_mid-wheel_mid)
            dx, dy = p.x-old.x, p.y-old.y
            fused = Pose(t, last.x+dx*math.cos(angle)-dy*math.sin(angle),
                         last.y+dx*math.sin(angle)+dy*math.cos(angle), fyaw)
        else:
            if old:
                self.reset("odometry sampling gap; reanchored on wheel pose")
                self.publish_grid(msg.header.stamp)
            self.mode = "wheel_only"
            fused = new
        self.last_odom = new
        self.odom_at = time.monotonic()
        self.poses.append(fused)
        out = Odometry()
        out.header = msg.header
        out.child_frame_id = self.base_frame
        out.pose.pose.position.x, out.pose.pose.position.y = fused.x, fused.y
        out.pose.pose.orientation.z = math.sin(fused.yaw/2)
        out.pose.pose.orientation.w = math.cos(fused.yaw/2)
        out.twist = msg.twist
        self.pose_pub.publish(out)

    def on_scan(self, msg):
        t = seconds(msg.header.stamp)
        if self.future_stamp(t):
            self.dropped += 1
            self.scan_reason = "scan ahead of simulation clock"
            return
        if (msg.header.frame_id != self.laser_frame or not msg.ranges or
                not all(math.isfinite(v) for v in [msg.angle_min, msg.angle_increment,
                                                    msg.range_min, msg.range_max]) or
                msg.angle_increment == 0 or not 0 <= msg.range_min < msg.range_max or
                (self.last_scan_stamp is not None and t <= self.last_scan_stamp)):
            self.dropped += 1
            self.scan_reason = "invalid frame, ranges or repeated scan stamp"
            return
        self.last_scan_stamp = t
        if len(self.pending) >= 10:
            self.pending.popleft()
            self.dropped += 1
        self.pending.append((time.monotonic(), msg))

    def process_scans(self):
        times = [pose.t for pose in self.poses]
        while self.pending:
            arrival, msg = self.pending[0]
            t = seconds(msg.header.stamp)
            if time.monotonic()-arrival > 0.5 or (times and t < times[0]):
                self.dropped += 1
                self.scan_reason = "no matching recent pose"
                self.pending.popleft()
                continue
            i = bisect_left(times, t)
            if i == len(times):
                return  # Wait for the next odom frame to bracket capture time.
            self.pending.popleft()
            b = self.poses[i]
            a = self.poses[i-1] if i and b.t != t else b
            if b.t-a.t > 0.15:
                self.dropped += 1
                self.scan_reason = "pose gap exceeds 150 ms"
                continue
            ratio = (t-a.t)/(b.t-a.t) if a.t != b.t else 0.0
            pose = Pose(t, a.x+ratio*(b.x-a.x), a.y+ratio*(b.y-a.y),
                        wrap(a.yaw+ratio*wrap(b.yaw-a.yaw)))
            if not self.grid.integrate(msg, pose, self.mount, self.clear_infinite):
                self.dropped += 1
                self.scan_reason = "no grid evidence from scan"
                continue
            self.matched += 1
            self.scan_reason = "scan integrated"
            self.mapped_stamp = msg.header.stamp

    def publish_grid(self, stamp):
        m = OccupancyGrid()
        m.header.frame_id = self.odom_frame
        m.header.stamp = stamp
        m.info.width = m.info.height = self.size
        m.info.resolution = self.res
        m.info.origin.position.x = m.info.origin.position.y = self.grid.origin
        m.info.origin.orientation.w = 1.0
        m.data = self.grid.data
        self.map_pub.publish(m)

    def publish_summary(self):
        mode = self.mode
        reason = self.reason
        if self.odom_at is not None and time.monotonic()-self.odom_at > 1:
            mode, reason = "stale_odom", "wheel odometry timed out"
        # A delayed/stopped scan stream must not advertise a freshly updated map.
        if self.mapped_stamp and self.mapped_stamp != self.published_map_stamp:
            self.publish_grid(self.mapped_stamp)
            self.published_map_stamp = self.mapped_stamp
        occupied = self.grid.data.count(100)
        free = self.grid.data.count(0)
        report = {"mode": mode, "reason": reason, "matched_scans": self.matched,
                  "dropped_scans": self.dropped,"scan_reason": self.scan_reason,
                  "occupied_cells": occupied, "free_cells": free,
                  "odom_frame": self.odom_frame, "base_frame": self.base_frame,
                  "laser_frame": self.laser_frame, "imu_frame": self.imu_frame,
                  "mount": dict(zip(("x","y","yaw"), self.mount))}
        self.status_pub.publish(String(data=json.dumps(report, separators=(",",":"))))


def main():
    rclpy.init()
    node = Perception()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
