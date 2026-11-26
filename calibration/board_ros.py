"""Bounded, read-only ROS RGB/CameraInfo/PointCloud2 acquisition for board calibration."""
from collections import deque
import json
import math
import time

import numpy as np
from calibration.camera_geometry import decode_rgb
from edge_contracts.capture_navigation import pose_at, scan_at

POINTCLOUD2 = "sensor_msgs/msg/PointCloud2"
LIVOX_CUSTOM = "livox_ros_driver2/msg/CustomMsg"


def lidar_message_type(sensor):
    kind = sensor.get("message_type", POINTCLOUD2)
    if kind == POINTCLOUD2:
        from sensor_msgs.msg import PointCloud2
        return PointCloud2
    if kind == LIVOX_CUSTOM:
        try:
            from livox_ros_driver2.msg import CustomMsg
        except ImportError as exc:
            raise ValueError("该点云为 Livox CustomMsg。请为只读采集容器安装与现场驱动匹配的 livox_ros_driver2 消息接口。") from exc
        return CustomMsg
    raise ValueError(f"unsupported calibration point message type: {kind}")


def lidar_xyz(msg, sensor):
    if sensor.get("message_type", POINTCLOUD2) == LIVOX_CUSTOM:
        count = msg.point_num
        if not 0 < count <= 2_000_000 or len(msg.points) != count:
            raise ValueError("invalid Livox CustomMsg point count")
        return np.asarray([(p.x, p.y, p.z) for p in msg.points], dtype=np.float32)
    return decode_cloud(msg)[0]


def decode_cloud(msg, time_field=None):
    """Read metric XYZ and optional acquisition time without changing input bytes or precision."""
    count = msg.width * msg.height
    data = memoryview(msg.data)
    if (not 0 < count <= 2_000_000 or msg.point_step <= 0 or
            msg.row_step < msg.point_step * msg.width or data.nbytes != msg.row_step * msg.height):
        raise ValueError("invalid PointCloud2 dimensions or stride")
    requested = ["x", "y", "z"]
    if time_field is not None:
        if not time_field or time_field in requested:
            raise ValueError("invalid per-point acquisition time binding")
        requested.append(time_field)
    formats = {1:"i1", 2:"u1", 3:"i2", 4:"u2", 5:"i4", 6:"u4", 7:"f4", 8:"f8"}
    coords = {}
    occupied = set()
    for f in msg.fields:
        if f.name not in requested:
            continue
        if (f.name in coords or f.count != 1 or f.datatype not in formats
                or (f.name in ("x", "y", "z") and f.datatype not in (7, 8))):
            raise ValueError(f"invalid PointCloud2 field {f.name}")
        dtype = np.dtype((">" if msg.is_bigendian else "<") + formats[f.datatype])
        span = range(f.offset, f.offset + dtype.itemsize)
        if f.offset < 0 or span.stop > msg.point_step or occupied.intersection(span):
            raise ValueError(f"invalid PointCloud2 offset for {f.name}")
        occupied.update(span)
        coords[f.name] = np.ndarray((msg.height, msg.width), dtype=dtype, buffer=data, offset=f.offset,
                                    strides=(msg.row_step, msg.point_step)).reshape(-1)
    if set(coords) != set(requested):
        raise ValueError("missing PointCloud2 fields: " + ", ".join(sorted(set(requested) - set(coords))))
    return np.column_stack([coords[k] for k in ("x", "y", "z")]), coords.get(time_field)


class BoardSource:
    def __init__(self, profile, *, preview_only=False, node_name=None):
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import QoSProfile, qos_profile_sensor_data
        from sensor_msgs.msg import CameraInfo, Image

        self.camera = profile["camera"]
        lidar_entries = [] if preview_only else profile.get("lidars", [])
        self.lidars = {entry["id"]: entry for entry in lidar_entries}
        self.skew = profile["max_skew_ns"]
        if (len(self.lidars) != len(lidar_entries) or
                set(self.lidars) & {"camera", "info"} or not 0 <= self.skew <= 50_000_000):
            raise ValueError("invalid calibration stream roles or synchronization tolerance")
        channels = {"camera": (Image, self.camera["topic"], self.camera["frame"]),
                    "info": (CameraInfo, self.camera["info_topic"], self.camera["frame"])}
        channels.update({k:(lidar_message_type(src), src["topic"], src["frame"]) for k, src in self.lidars.items()})
        self.buffers = {key:deque(maxlen=6) for key in channels}
        self.last = {key:-1 for key in channels}
        self.receptions = {}
        self.frames = {k:frame for k, (_, _, frame) in channels.items()}
        self.frame_errors = {}
        self.ctx = Context()
        rclpy.init(args=[], context=self.ctx)
        self.node = rclpy.create_node(node_name or ("edge_board_viewfinder" if preview_only else "edge_board_recorder"),
                                      context=self.ctx)
        self.executor = SingleThreadedExecutor(context=self.ctx)
        self.executor.add_node(self.node)
        self.navigation = {}
        try:
            qos = (QoSProfile(depth=1, reliability=qos_profile_sensor_data.reliability,
                              durability=qos_profile_sensor_data.durability) if preview_only else qos_profile_sensor_data)
            for key, (kind, topic, frame) in channels.items():
                self.node.create_subscription(kind, topic,
                    lambda msg,k=key,f=frame:self._receive(k,f,msg), qos)
            if not preview_only and profile.get("capture_navigation"):
                from sensor_msgs.msg import LaserScan
                from std_msgs.msg import String
                for key, kind, topic, decode in (
                        ("robot", String, "/fixed/capture/boot_pose", lambda msg:json.loads(msg.data)),
                        ("scan", LaserScan, "/fixed/perimeter_scan", self._scan)):
                    self.navigation[key] = deque(maxlen=16)
                    self.node.create_subscription(kind, topic, lambda msg,k=key,d=decode:self._nav(k,d,msg), qos)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _scan(msg):
        ranges = [None if r == math.inf else r for r in msg.ranges]
        if not all(r is None or math.isfinite(r) and msg.range_min <= r <= msg.range_max for r in ranges):
            raise ValueError("invalid_perimeter_beam")
        return dict(frame=msg.header.frame_id, stamp_ns=msg.header.stamp.sec*10**9+msg.header.stamp.nanosec,
                    angle_min=msg.angle_min, angle_step=msg.angle_increment, range_m=msg.range_max, ranges_m=ranges)

    def _nav(self, key, decode, msg):
        try:
            value = decode(msg)
            clock = value["stamp_ns"]
            if self.navigation[key] and clock < self.navigation[key][-1][0]:
                self.navigation[key].clear()
            self.navigation[key].append((clock,time.monotonic(),value))
        except (ValueError, KeyError, TypeError):
            self.navigation[key].clear()

    def _receive(self, key, frame, msg):
        self.receptions[key] = (time.monotonic(), msg.header.frame_id)
        if msg.header.frame_id != frame:
            self.frame_errors[key] = f"{key}: received frame {msg.header.frame_id} instead of {frame}"
            self.buffers[key].clear()
            return
        self.frame_errors.pop(key,None)
        ns = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
        if ns < 0:
            return
        if self.last[key] >= 0 and ns + 1_000_000_000 < self.last[key]:
            # Sensor/simulator clock stepped backwards (e.g. driver or Gazebo restart):
            # discard pre-reset samples on this channel so new frames are not silently dropped forever.
            self.buffers[key].clear()
            self.last[key] = -1
        if ns > self.last[key]:
            self.buffers[key].append((ns, time.monotonic(), msg))

    def stream_status(self):
        now = time.monotonic()
        result = {}
        for key, expected in self.frames.items():
            reception = self.receptions.get(key)
            age = now - reception[0] if reception else None
            result[key] = dict(received=bool(reception), frame=reception[1] if reception else expected,
                               age_s=round(age, 3) if reception else None,
                               fresh=bool(reception and age <= 1. and key not in self.frame_errors),
                               detail=self.frame_errors.get(key))
        return result

    def next_frame(self, timeout_s=1., *, require_navigation=False):
        if not self.lidars:
            raise ValueError("board calibration requires at least one configured LiDAR")
        return self._next_frame(timeout_s,self.lidars,require_navigation=require_navigation)

    def next_rgb_frame(self, timeout_s=1.):
        """A camera consumer needs only RGB and matching CameraInfo."""
        return self._next_frame(timeout_s,())

    def next_live_rgb_frame(self, timeout_s=1.4):
        """Require both volatile depth-one streams to advance after opening the viewfinder."""
        end = time.monotonic() + timeout_s
        for key in ("camera", "info"):
            self.buffers[key].clear()
            self.last[key] = -1
        while not all(self.buffers[k] for k in ("camera", "info")):
            remaining = end - time.monotonic()
            if remaining <= 0:
                return None
            self.executor.spin_once(timeout_sec=min(.1, remaining))
        for key in ("camera", "info"):
            self.last[key] = self.buffers[key][-1][0]
            self.buffers[key].clear()
        return self.next_rgb_frame(max(0., end - time.monotonic()))

    def _next_frame(self, timeout_s, lidars, *, require_navigation=False):
        roles = ("camera", "info", *lidars)
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            self.executor.spin_once(timeout_sec=min(.1, max(0., end-time.monotonic())))
            now = time.monotonic()
            for key in roles:
                if key in self.frame_errors:
                    raise ValueError(self.frame_errors[key])
            if not all(self.buffers[k] for k in roles):
                continue
            for ns, _, _ in reversed(self.buffers["camera"]):
                group = {k:min(self.buffers[k],key=lambda row: abs(row[0]-ns)) for k in roles}
                stamps = {k:v[0] for k,v in group.items()}
                if (max(stamps.values()) - min(stamps.values()) > self.skew or
                        any(now-v[1]>1. or stamps[k]<=self.last[k] for k,v in group.items())):
                    continue
                nav = {}
                for key, stream in self.navigation.items():
                    recent = [v for v in stream if now-v[1] <= .65]
                    if recent:
                        nav[key] = min(recent,key=lambda row:abs(row[0]-ns))[2]
                if require_navigation:
                    try:
                        pose_at(ns,nav.get("robot"))
                        scan_at(ns,nav.get("scan"))
                    except ValueError:
                        continue
                image, info = group["camera"][2], group["info"][2]
                rgb, k_matrix = decode_rgb(image, info, self.camera)
                clouds = {}
                for key in lidars:
                    cloud = group[key][2]
                    xyz = lidar_xyz(cloud, self.lidars[key])
                    clouds[key] = xyz[np.isfinite(xyz).all(axis=1)].copy()
                self.last.update(stamps)
                received_ns = time.time_ns() - int((time.monotonic() - group["camera"][1])*1e9)
                return dict(stamp_ns=ns, stamps_ns=stamps, received_unix_ns=received_ns, rgb=rgb, k=k_matrix,
                            camera_info=published_camera_info(info), clouds=clouds,
                            navigation=nav,
                            frames={k:v[2].header.frame_id for k,v in group.items() if k!="info"})
        return None

    def close(self):
        self.executor.shutdown()
        self.node.destroy_node()
        self.ctx.try_shutdown()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def published_camera_info(info):
    """Preserve published metadata separately from the K used to decode the stream."""
    def finite_or_none(values):
        array = np.asarray(values, dtype=float)
        return array.tolist() if array.size and np.isfinite(array).all() else None
    return dict(k=finite_or_none(info.k), p=finite_or_none(info.p), d=finite_or_none(info.d),
                distortion_model=info.distortion_model or None, resolution=[info.width,info.height],
                binning=[info.binning_x,info.binning_y],
                roi={f:getattr(info.roi,f) for f in ("x_offset","y_offset","width","height","do_rectify")})
