"""Ground a configured Gazebo fiducial using paired RGB-D and odometry."""

import argparse
from collections import deque
from hashlib import sha256
import json
import math
from pathlib import Path
import time

import numpy as np

from edge_contracts.occupancy import finite
from exploration.survey_semantic import identity
from perception.color_marker import marker_pixels
from perception.rgbd.pairs import Synchronizer, stamp, validate


def configuration(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    keys = {"camera_frame", "source_id", "mount", "marker"}
    if not isinstance(data, dict) or set(data) != keys or not all(
        identity(data[key], 120) for key in ("camera_frame", "source_id", "marker")
    ):
        raise ValueError("invalid_fiducial_config")
    mount = data["mount"]
    if not isinstance(mount, list) or len(mount) != 3 or not all(finite(v) for v in mount):
        raise ValueError("invalid_fiducial_mount")
    return data


def interpolation(history, capture_ns):
    for i, sample in enumerate(history):
        t, pose = sample
        if t == capture_ns:
            return pose
        if t > capture_ns and i:
            begin, first = history[i-1]
            if t - begin > 180_000_000:
                return None
            f = (capture_ns - begin) / (t - begin)
            angular = math.atan2(math.sin(pose[2] - first[2]), math.cos(pose[2] - first[2]))
            return [first[0]+(pose[0]-first[0])*f, first[1]+(pose[1]-first[1])*f,
                    first[2]+angular*f]
        if t > capture_ns:
            return None
    return None


def detect(messages, settings, pose, frame):
    color, depth, info = messages
    for kind, msg in zip(("color", "depth", "info"), messages):
        validate(kind, msg)
        if (msg.header.frame_id != settings["camera_frame"] or
                (msg.width, msg.height) != (color.width, color.height)):
            return None
    timestamps = [stamp(msg) for msg in messages]
    if min(timestamps) <= 0 or max(timestamps) - min(timestamps) > 30_000_000:
        return None
    if color.encoding.lower() not in ("rgb8", "bgr8"):
        return None
    rgb = np.frombuffer(bytes(color.data), dtype="uint8").reshape(color.height, color.step)
    rgb = rgb[:, :color.width*3].reshape(color.height, color.width, 3)
    if color.encoding.lower() == "bgr8":
        rgb = rgb[:, :, ::-1]
    matched = marker_pixels(rgb)
    if matched is None:
        return None
    v, u, (x0, y0, x1, y1) = matched

    dtype = np.dtype((">" if depth.is_bigendian else "<") +
                     ("f4" if depth.encoding.lower() == "32fc1" else "u2"))
    values = np.ndarray((depth.height, depth.width), dtype=dtype, buffer=bytes(depth.data),
                        strides=(depth.step, dtype.itemsize))[v, u].astype("float64")
    if dtype.kind == "u":
        values *= .001
    usable = np.isfinite(values) & (.16 <= values) & (values < 5.8)
    if np.count_nonzero(usable) < max(20, len(values) * .70):
        return None
    center = np.median(values[usable])
    nearby = usable & (np.abs(values-center) <= max(.045, center*.047))
    if np.count_nonzero(nearby) < max(18, len(values) * .65):
        return None

    x, y, yaw = pose
    mount_x, mount_y, angle = settings["mount"]
    p_u, p_v = float(np.median(u[nearby])), float(np.median(v[nearby]))
    meters = float(np.median(values[nearby]))
    fx, fy, cx, cy = (float(info.k[i]) for i in (0, 4, 2, 5))
    left, up = (cx-p_u)*meters/fx, (cy-p_v)*meters/fy
    camera_x = x + math.cos(yaw)*mount_x-math.sin(yaw)*mount_y
    camera_y = y + math.sin(yaw)*mount_x+math.cos(yaw)*mount_y
    heading = yaw + angle
    point = [camera_x+math.cos(heading)*meters-math.sin(heading)*left,
             camera_y+math.sin(heading)*meters+math.cos(heading)*left]
    metadata = {"stamp_ns": timestamps, "box_px": [x0, y0, x1, y1], "pixel": [p_u, p_v],
                "intrinsics": [fx, fy, cx, cy], "camera_frame": settings["camera_frame"],
                "depth_m": meters, "optical_xyz": [-left, -up, meters], "pose": pose,
                "mount": settings["mount"]}
    evidence = sha256()
    evidence.update(json.dumps(metadata, sort_keys=True, allow_nan=False).encode())
    evidence.update(bytes(color.data))
    evidence.update(bytes(depth.data))
    return {"frame": frame, "source_id": settings["source_id"], "source_kind": "gazebo_rgbd_fiducial",
            "label": settings["marker"], "stamp_s": timestamps[0]/1e9, "pose": pose, "point": point,
            "confidence": float(min(.99, .70 + .29*np.count_nonzero(nearby)/len(values))),
            "evidence_ref": evidence.hexdigest(), "evidence": metadata}


def main():
    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from rosgraph_msgs.msg import Clock
    from sensor_msgs.msg import CameraInfo, Image
    from std_msgs.msg import String

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", default="odom")
    parser.add_argument("--site-config", required=True)
    parser.add_argument("--rgbd-prefix", default="/sdd058/rgbd")
    parser.add_argument("--pose-topic", default="/edge/perception/pose")
    parser.add_argument("--topic", default="/sdd058/semantic/observation")
    args = parser.parse_args()
    settings = configuration(args.site_config)

    class Signs(Node):
        def __init__(self):
            super().__init__("edge_survey_fiducials")
            self.chain = Synchronizer(settings["source_id"], skew_ms=30)
            self.history = deque(maxlen=45)
            self.clock_ns = 0
            self.last_image = 0
            self.out = self.create_publisher(String, args.topic, qos_profile_sensor_data)
            self.create_subscription(Clock, "/clock", self.clock, qos_profile_sensor_data)
            self.create_subscription(Odometry, args.pose_topic, self.pose, qos_profile_sensor_data)
            for kind, msg_type in (("color", Image), ("depth", Image), ("info", CameraInfo)):
                self.create_subscription(msg_type, f"{args.rgbd_prefix}/{kind}",
                                         lambda message, stream=kind: self.image(stream, message),
                                         qos_profile_sensor_data)
            self.create_timer(.04, self.ground)

        def clock(self, message):
            t = message.clock.sec * 1_000_000_000 + message.clock.nanosec
            if t < self.clock_ns:
                self.history.clear()
                self.last_image = 0
            self.clock_ns = t

        def pose(self, message):
            if message.header.frame_id != args.frame or message.child_frame_id != "base_link":
                return
            t = stamp(message)
            if not 0 < t <= self.clock_ns + 200_000_000 or (self.history and t <= self.history[-1][0]):
                return
            p, q = message.pose.pose.position, message.pose.pose.orientation
            yaw = math.atan2(2*(q.w*q.z+q.x*q.y), 1-2*(q.y*q.y+q.z*q.z))
            if all(math.isfinite(v) for v in (p.x, p.y, yaw)):
                self.history.append((t, [p.x, p.y, yaw]))

        def image(self, kind, message):
            self.chain.accept(kind, message, time.monotonic(), self.clock_ns)

        def ground(self):
            self.chain.refresh(time.monotonic(), self.clock_ns)
            for pair in reversed(self.chain.history):
                t = stamp(pair.messages[0])
                if t <= self.last_image or not 0 <= self.clock_ns-t < 500_000_000:
                    continue
                pose = interpolation(self.history, t)
                if pose is None:
                    continue
                self.last_image = t
                result = detect(pair.messages, settings, pose, args.frame)
                if result:
                    self.out.publish(String(data=json.dumps(result, ensure_ascii=False, allow_nan=False)))

    rclpy.init()
    node = Signs()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
