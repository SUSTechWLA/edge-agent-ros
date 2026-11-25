"""Capture one synchronized Gazebo Harmonic measurement from a stationary simulated rig.

Run inside the project's Gazebo image, with ROS Jazzy sourced and a private GZ_PARTITION.
Only GZ_TO_ROS bridges are created. The output directory must be empty.
"""
from collections import defaultdict
import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from xml.etree import ElementTree as ET

import numpy as np
import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, Image, PointCloud2
from sensor_msgs_py.point_cloud2 import read_points_numpy


WORLD = Path(__file__).resolve().parents[2] / "sim/rgb_lidar_bench.sdf"
CLOCK = "gazebo_sim"
LASERS = ("lidar_left", "lidar_right")
STREAMS = {
    "rgb": ("/fusion/rgbd/image", Image, "Image"),
    "depth": ("/fusion/rgbd/depth_image", Image, "Image"),
    "intrinsics": ("/fusion/rgbd/camera_info", CameraInfo, "CameraInfo"),
    **{lidar: (f"/fusion/{lidar}/points", PointCloud2, "PointCloudPacked") for lidar in LASERS},
}


def write_json(path, obj):
    raw = json.dumps(obj, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return "sha256:" + sha256(raw).hexdigest()


def start_owned(stack, out, name, argv):
    log = stack.enter_context((out / (name + ".log")).open("wb"))
    proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def close():
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    stack.callback(close)
    return proc


def record(node, processes):
    windows = defaultdict(dict)
    counts = defaultdict(int)
    seen = {}

    def receive(key, msg):
        stamp = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
        counts[key] += 1
        seen[key] = (stamp, msg.header.frame_id)
        if stamp > 0:
            windows[stamp][key] = msg
            while len(windows) > 15:
                del windows[min(windows)]

    for key, (topic, cls, _) in STREAMS.items():
        node.create_subscription(cls, topic, lambda m, k=key: receive(k, m), qos_profile_sensor_data)
    deadline = time.monotonic() + 65
    while time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=.1)
        for stamp, messages in windows.items():
            if stamp >= 1_000_000_000 and len(messages) == len(STREAMS):
                # Several renderers emit a first black exposure during startup.
                image = np.frombuffer(messages["rgb"].data, dtype=np.uint8).reshape(-1,3)
                if np.count_nonzero(np.ptp(image, axis=1) > 40) < image.shape[0] // 10:
                    continue
                print("Complete sensor window", stamp, dict(counts), flush=True)
                return stamp, messages
        if any(p.poll() is not None for p in processes):
            break
    raise RuntimeError(f"No complete 5-stream acquisition. Counts {dict(counts)}, last headers {seen}, "
                       f"process exit codes {[p.poll() for p in processes]}")


def yaw_matrix(x, y, z, yaw):
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, -s, 0, x], [s, c, 0, y], [0, 0, 1, z], [0, 0, 0, 1]])


def confirm_rig_pose(out, expected):
    result = subprocess.run(["gz", "topic", "-e", "-n", "1", "--json-output",
                             "-t", "/world/fusion_bench/pose/info"], capture_output=True, check=True, timeout=12)
    reply = json.loads(result.stdout)
    rig = next(row for row in reply["pose"] if row["name"] == "rig")
    p, q = rig["position"], rig["orientation"]
    assert abs(float(q.get("x",0))) < 1e-9 and abs(float(q.get("y",0))) < 1e-9
    actual = yaw_matrix(*(float(p.get(k, 0)) for k in "xyz"), 2*math.atan2(q.get("z",0),q["w"]))
    assert np.allclose(actual, expected, atol=1e-6)
    write_json(out/"rig-pose.json", {"header": reply.get("header",{}), "rig":rig, "topic":"/world/fusion_bench/pose/info"})
    return actual


def serialize(stamp, msgs, out, world_rig, static_ref, rig_ref):
    sensors = ET.parse(WORLD).find("world/model[@name='rig']/link[@name='mount']")
    if sensors is None:
        raise ValueError("Missing measuring rig")
    info, color, depth = (msgs[k] for k in ("intrinsics", "rgb", "depth"))
    assert color.encoding == "rgb8" and depth.encoding == "32FC1"
    assert color.header.frame_id == depth.header.frame_id == info.header.frame_id == "rgbd_optical"
    assert (color.width, color.height) == (depth.width, depth.height) == (info.width, info.height)
    assert info.k[0] > 0 and info.k[4] > 0 and not any(info.d)
    w, h = info.width, info.height
    rgb = np.frombuffer(color.data, np.uint8).reshape(h, color.step)[:, :3*w].tobytes()
    met = np.frombuffer(depth.data, ">f4" if depth.is_bigendian else "<f4").reshape(h, depth.step//4)
    depth_raw = met[:, :w].astype("<f4").tobytes()

    def pose(name, optical=False):
        text = sensors.findtext(f"sensor[@name='{name}']/pose")
        x, y, z, roll, pitch, yaw = map(float, text.split())
        assert roll == pitch == 0
        transform = world_rig @ yaw_matrix(x,y,z,yaw)
        if optical:  # Gazebo +X forward, ROS optical +Z forward, +X right, +Y down
            transform = transform @ [[0, 0, 1, 0], [-1, 0, 0, 0], [0, -1, 0, 0], [0, 0, 0, 1]]
        return transform.tolist()

    camera = {"frame_id": info.header.frame_id, "width": w, "height": h,
              "k": list(info.k), "T_map_camera": pose("rgbd", optical=True)}
    mounts = [{"source_id": key, "frame_id": key, "T_map_lidar": pose(key)} for key in LASERS]
    calib = {"frame_id": "world", "clock_id": CLOCK, "source": "gazebo_sdf",
             "world_file_sha256": sha256((out/"world.sdf").read_bytes()).hexdigest(),
             "static_world_ref": static_ref, "rig_ref": rig_ref,"T_world_rig":world_rig.tolist(),
             "rig_pose_source":"gazebo_pose_info","camera": camera, "lidars": mounts}
    files = {"calibration.json": write_json(out/"calibration.json", calib)}
    for name, data in (("rgb.raw", rgb), ("depth.f32", depth_raw)):
        (out/name).write_bytes(data)
        files[name] = "sha256:" + sha256(data).hexdigest()
    clouds = []
    for mount in mounts:
        cloud = msgs[mount["source_id"]]
        assert cloud.header.frame_id == mount["frame_id"]
        xyz = read_points_numpy(cloud, field_names=("x", "y", "z")).reshape(-1, 3)
        pts = xyz[np.isfinite(xyz).all(axis=1)]
        assert len(pts) >= 500
        clouds.append({**mount, "stamp_ns": stamp, "clock_id": CLOCK, "points": pts.tolist()})
    return write_json(out/"capture.json", {
        "schema_version": 1, "frame_id": "world", "clock_id": CLOCK, "files": files,
        "camera": {**camera, "clock_id": CLOCK, "stamp_ns": stamp, "depth_stamp_ns": stamp,
                   "depth_frame_id": depth.header.frame_id, "depth_file": "depth.f32", "rgb_file": "rgb.raw",
                   "projection_model": "rectified_pinhole", "depth_convention": "optical_z_m"},
        "lidars": clouds})


def main(out, x, y, yaw):
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError(f"Use an empty capture directory: {out}")
    tree = ET.parse(WORLD)
    world = tree.find("world")
    rig = world.find("model[@name='rig']")
    world.remove(rig)
    static_ref = "sha256:" + sha256(ET.tostring(world)).hexdigest()
    rig_ref = "sha256:" + sha256(ET.tostring(rig)).hexdigest()
    ET.SubElement(rig, "pose").text = f"{x} {y} 0 0 0 {yaw}"
    world.append(rig)
    tree.write(out/"world.sdf", encoding="utf-8", xml_declaration=True)
    with ExitStack() as stack:
        server = start_owned(stack, out, "gazebo", ["gz", "sim", "-s", "-r", "--headless-rendering", str(out/"world.sdf")])
        bridge = start_owned(stack, out, "bridge", ["ros2", "run", "ros_gz_bridge", "parameter_bridge", *[
            f"{topic}@sensor_msgs/msg/{cls.__name__}[gz.msgs.{gztype}" for topic, cls, gztype in STREAMS.values()]])
        rclpy.init()
        stack.callback(rclpy.shutdown)
        node = rclpy.create_node("rgb_lidar_archive_reader")
        stack.callback(node.destroy_node)
        stamp, msgs = record(node, [server, bridge])
        actual = confirm_rig_pose(out, yaw_matrix(x,y,0,yaw))
        print("Capture reference", serialize(stamp, msgs, out, actual, static_ref, rig_ref), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out",type=Path)
    parser.add_argument("--pose",nargs=3, type=float, metavar=("X_M","Y_M","YAW_RAD"),default=(0,0,0))
    args=parser.parse_args()
    main(args.out,*args.pose)
