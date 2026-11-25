"""Independent Gazebo test operator and acquisition-clock perturbation fixture."""
import argparse
import json
import math
from pathlib import Path
import threading
import time


# Truth exists only in the test fixture. Values are added to unaltered source
# acquisition stamps; no delay or resampling of the original measurements.
INJECTION_NS = {"camera": 47_000_000, "info": 47_000_000,
                "lidar_left": 164_000_000, "lidar_right": -32_000_000}
TOPICS = {"camera": "/calibration/rgbd/image", "info": "/calibration/rgbd/camera_info",
          "lidar_left": "/calibration/lidar_left/points",
          "lidar_right": "/calibration/lidar_right/points"}


def relay():
    import rclpy
    from rclpy.qos import qos_profile_sensor_data as qos
    from sensor_msgs.msg import CameraInfo, Image, PointCloud2

    rclpy.init()
    node = rclpy.create_node("bench_acquisition_clock_injector")
    history = {}
    for role, topic in TOPICS.items():
        msg_type = {"camera": Image, "info": CameraInfo}.get(role, PointCloud2)
        pub = node.create_publisher(msg_type, "/clock_bench" + topic, qos)

        def deliver(message, role=role, pub=pub):
            stamp = message.header.stamp
            original = stamp.sec * 1_000_000_000 + stamp.nanosec
            shifted = original + INJECTION_NS[role]
            if shifted < 0:
                return
            stamp.sec, stamp.nanosec = divmod(shifted, 1_000_000_000)
            pub.publish(message)
            stats = history.setdefault(role, {"count": 0, "first_original_ns": original})
            stats.update(count=stats["count"] + 1, original_ns=original,
                         injected_ns=shifted, frame=message.header.frame_id)
        node.create_subscription(msg_type, topic, deliver, qos)

    def audit():
        path = Path("/tmp/clock-relay-audit.json")
        pending = path.with_suffix(".partial")
        pending.write_text(json.dumps(history))
        pending.replace(path)
    node.create_timer(1., audit)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


def board_holder(mode):
    from gz.msgs10.boolean_pb2 import Boolean
    from gz.msgs10.pose_pb2 import Pose
    from gz.transport13 import Node

    gz_node = Node()
    start = last_success = time.monotonic()
    while True:
        # The 0.5x bench gives the renderer time to deliver 15 samples per simulated second.
        t = .5 * (time.monotonic() - start)
        displacement = 0.0
        if mode == "moving":
            displacement = (.24 * math.sin(2.0*t + .48*math.sin(.59*t)) +
                            .095 * math.sin(3.91*t+.77) + .052 * math.sin(1.17*t+1.5))
        pose = Pose(name="calibration_board")
        pose.position.x, pose.position.y, pose.position.z = 1.63 + displacement, .01, .88
        pose.orientation.y = math.sin(.075)
        pose.orientation.w = math.cos(.075)
        success, answer = gz_node.request("/world/calibration_bench/set_pose", pose, Pose, Boolean, 2000)
        if not success or not answer.data:
            if time.monotonic() - last_success > 20:
                raise RuntimeError(f"Gazebo did not accept board pose: {success}, {answer}")
            continue
        last_success = time.monotonic()
        if mode == "static":
            # Gazebo now owns a fixed board pose; keep the operator alive without further updates.
            threading.Event().wait()
        time.sleep(.03)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("relay", "moving", "static"))
    role = parser.parse_args().role
    if role == "relay":
        relay()
    else:
        board_holder(role)
