"""Publish the latest measured wheel pose to a local file for read-only planning."""
import argparse
import json
import math
import os
from pathlib import Path
import time
import uuid


def stamp_ns(stamp):
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def xyz(v):
    values = [v.x, v.y, v.z]
    if not all(math.isfinite(i) for i in values):
        raise ValueError("nonfinite vector")
    return values


def rotation(q):
    values = [q.x, q.y, q.z, q.w]
    if not all(math.isfinite(i) for i in values) or abs(math.hypot(*values) - 1) > 0.01:
        raise ValueError("invalid quaternion")
    yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
    return values, yaw


def save(path, value):
    temp = path.with_name(".pose-" + str(os.getpid()) + ".tmp")
    temp.write_text(json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n", encoding="utf8")
    os.replace(temp, path)


def measured(msg, a, received):
    source = stamp_ns(msg.header.stamp)
    if (msg.header.frame_id != a.odom_frame or msg.child_frame_id != a.base_frame or source <= 0
            or not -50_000_000 <= received - source <= a.ttl_ms * 1_000_000):
        raise ValueError("odometry frame or source timestamp unavailable")
    position = xyz(msg.pose.pose.position)
    quaternion, yaw = rotation(msg.pose.pose.orientation)
    linear, angular = xyz(msg.twist.twist.linear), xyz(msg.twist.twist.angular)
    covariance = [msg.pose.covariance[6 * i + j] for i in (0, 1, 5) for j in (0, 1, 5)]
    if not all(math.isfinite(i) for i in covariance):
        raise ValueError("nonfinite pose covariance")
    return {"valid": True, "stamp_ns": str(source), "wall_received_ns": str(received),
            "frame_id": msg.header.frame_id, "child_frame_id": msg.child_frame_id,
            "x_m": position[0], "y_m": position[1], "z_m": position[2], "yaw_rad": yaw,
            "position_m": position, "orientation_xyzw": quaternion, "planar_covariance": covariance,
            "linear_velocity_mps": linear, "angular_velocity_rps": angular,
            "linear_mps": math.hypot(*linear[:2]), "angular_rps": angular[2]}


def map_transform(tf, a, sample):
    if (tf.header.frame_id, tf.child_frame_id) != (a.map_frame, a.odom_frame):
        raise ValueError("wrong map transform")
    position = xyz(tf.transform.translation)
    quaternion, yaw = rotation(tf.transform.rotation)
    return {"parent_frame": tf.header.frame_id, "child_frame": tf.child_frame_id,
            "stamp_ns": str(stamp_ns(tf.header.stamp)), "lookup_stamp_ns": sample["stamp_ns"],
            "translation_m": position, "orientation_xyzw": quaternion, "yaw_rad": yaw}


def run(a):
    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.time import Time
    from tf2_ros import Buffer, TransformException, TransformListener

    rclpy.init()
    node = rclpy.create_node("edge_field_plan_pose_observer", enable_rosout=False, start_parameter_services=False)
    buffer = Buffer()
    listener = TransformListener(buffer, node)
    epoch = uuid.uuid4().hex
    latest = None
    seq = 0
    last_source = 0
    reported_state = None
    def emit(payload):
        save(a.output, {"schema_version": 1, "basis": "wheel_odometry", "clock_id": "unix",
                        "source_topic": a.topic, "ros_domain_id": node.context.get_domain_id(),
                        "epoch": epoch, "epoch_basis": "reader_continuity", "seq": seq,
                        "max_age_ms": a.ttl_ms, **payload})

    def receive(msg):
        nonlocal latest, seq, last_source, epoch
        try:
            sample = measured(msg, a, time.time_ns())
        except ValueError:
            latest = None
            return
        source = int(sample["stamp_ns"])
        if source == last_source:
            return
        if last_source and (source < last_source or source - last_source > a.ttl_ms * 1_000_000):
            epoch = uuid.uuid4().hex
        last_source = source
        seq += 1
        latest = sample

    def publish_file():
        nonlocal reported_state
        if latest is None or time.time_ns() - last_source > a.ttl_ms * 1_000_000:
            state = "odometry_unavailable"
            if reported_state != state:
                emit({"valid": False, "reason": state, "map_from_odom": None})
                reported_state = state
            return
        if reported_state == seq:
            return
        try:
            tf = buffer.lookup_transform(a.map_frame, a.odom_frame, Time(nanoseconds=last_source))
            mapping = map_transform(tf, a, latest)
        except (ValueError, TransformException):
            mapping = None
        emit({**latest, "map_from_odom": mapping,
              "map_tf_status": "available" if mapping else "unavailable_at_odom_stamp"})
        reported_state = seq

    a.output.parent.mkdir(parents=True, exist_ok=True)
    publish_file()
    node.create_subscription(Odometry, a.topic, receive, qos_profile_sensor_data)
    node.create_timer(0.1, publish_file)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        listener.unregister()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--topic", default="/lingao/odom")
    p.add_argument("--odom-frame", default="odom")
    p.add_argument("--base-frame", default="base_foot")
    p.add_argument("--map-frame", default="map")
    p.add_argument("--output", type=Path, default=Path("/pose/pose.json"))
    p.add_argument("--ttl-ms", type=int, default=500)
    args = p.parse_args()
    if not 100 <= args.ttl_ms <= 1000 or len({args.odom_frame, args.base_frame, args.map_frame}) != 3:
        p.error("invalid freshness bound or ambiguous frames")
    run(args)
