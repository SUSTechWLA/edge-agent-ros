"""Bounded, read-only ROS RGB/CameraInfo/PointCloud2 acquisition for board calibration."""
from collections import deque
import time

import numpy as np
from calibration.camera_geometry import decode_rgb


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
    def __init__(self, profile):
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import CameraInfo, Image, PointCloud2

        self.camera = profile["camera"]
        lidar_entries = profile.get("lidars", [])
        self.lidars = {entry["id"]: entry for entry in lidar_entries}
        self.skew = profile["max_skew_ns"]
        if (len(self.lidars) != len(lidar_entries) or
                set(self.lidars) & {"camera", "info"} or not 0 <= self.skew <= 50_000_000):
            raise ValueError("invalid calibration stream roles or synchronization tolerance")
        channels = {"camera": (Image, self.camera["topic"], self.camera["frame"]),
                    "info": (CameraInfo, self.camera["info_topic"], self.camera["frame"])}
        channels.update({k:(PointCloud2, src["topic"], src["frame"]) for k, src in self.lidars.items()})
        self.buffers = {key:deque(maxlen=6) for key in channels}
        self.last = {key:-1 for key in channels}
        self.frame_errors = {}
        self.ctx = Context()
        rclpy.init(args=[], context=self.ctx)
        self.node = rclpy.create_node("edge_board_recorder", context=self.ctx)
        self.executor = SingleThreadedExecutor(context=self.ctx)
        self.executor.add_node(self.node)
        try:
            for key, (kind, topic, frame) in channels.items():
                self.node.create_subscription(kind, topic,
                    lambda msg,k=key,f=frame:self._receive(k,f,msg), qos_profile_sensor_data)
        except BaseException:
            self.close()
            raise

    def _receive(self, key, frame, msg):
        if msg.header.frame_id != frame:
            self.frame_errors[key] = f"{key}: received frame {msg.header.frame_id} instead of {frame}"
            self.buffers[key].clear()
            return
        self.frame_errors.pop(key,None)
        ns = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
        if ns > self.last[key]:
            self.buffers[key].append((ns, time.monotonic(), msg))

    def next_frame(self, timeout_s=1.):
        if not self.lidars:
            raise ValueError("board calibration requires at least one configured LiDAR")
        return self._next_frame(timeout_s,self.lidars)

    def next_rgb_frame(self, timeout_s=1.):
        """A camera consumer needs only RGB and matching CameraInfo."""
        return self._next_frame(timeout_s,())

    def _next_frame(self, timeout_s, lidars):
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
                image, info = group["camera"][2], group["info"][2]
                rgb, k_matrix = decode_rgb(image, info, self.camera)
                clouds = {}
                for key in lidars:
                    cloud = group[key][2]
                    xyz, _ = decode_cloud(cloud)
                    clouds[key] = xyz[np.isfinite(xyz).all(axis=1)].copy()
                self.last.update(stamps)
                return dict(stamp_ns=ns, stamps_ns=stamps, rgb=rgb, k=k_matrix,
                            clouds=clouds, frames={k:v[2].header.frame_id for k,v in group.items() if k!="info"})
        return None

    def close(self):
        self.executor.shutdown()
        self.node.destroy_node()
        self.ctx.try_shutdown()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
