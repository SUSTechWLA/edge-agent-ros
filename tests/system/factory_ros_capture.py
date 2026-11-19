"""Exercise the production factory CLI with independent DDS publishers in a private ROS domain."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time

import rclpy
from rclpy.duration import Duration
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, Image, Imu, PointCloud2, PointField

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from test_factory_calibration import fixture


def build_streams(node, profile):
    channels = profile["sensors"]
    camera = profile["camera_intrinsics"]
    width, height = camera["width"], camera["height"]
    lidar = PointCloud2(height=1, width=80, is_bigendian=False, point_step=12, row_step=960)
    lidar.fields = [PointField(name=axis, offset=i*4, datatype=PointField.FLOAT32, count=1)
                    for i, axis in enumerate("xyz")]
    lidar.data = b"".join(struct.pack("<fff", i*.015, -.02, 1.5) for i in range(80))
    depth = Image(height=height, width=width, encoding="16UC1", step=width*2)
    depth.data = struct.pack("<H", 1500) * (width*height)
    color = Image(height=height, width=width, encoding="rgb8", step=width*3)
    color.data = bytes([32, 64, 128]) * (width*height)
    info = CameraInfo(height=height, width=width, k=camera["k"])
    imu = Imu()
    imu.linear_acceleration.z = 9.81
    imu.angular_velocity.x = .002
    imu.angular_velocity.y = -.001
    messages = {"lidar":lidar, "rgbd":depth, "imu":imu, "color":color, "info":info}
    streams = {}
    for name, msg in messages.items():
        source = channels[name if name in channels else "rgbd"]
        msg.header.frame_id = source["frame"]
        topic = source[name+"_topic" if name in ("color", "info") else "topic"]
        streams[name] = node.create_publisher(type(msg), topic, qos_profile_sensor_data), msg
    return streams


def run_capture(node, streams, profile, run, phase):
    command = [sys.executable, "-m", "calibration.ros_capture", "--profile", str(run/"profile.json"),
               "--identity", str(run/"identity.json"), "--robot-id", profile["robot_id"],
               "--state", str(run), "--seconds", "4"]
    with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
        end = time.monotonic()+22
        try:
            while child.poll() is None:
                if time.monotonic() > end:
                    raise TimeoutError("capture CLI failed to finish its bounded window")
                now = node.get_clock().now()
                for key, (publisher, msg) in streams.items():
                    if phase == "lidar_unplugged" and key == "lidar":
                        continue
                    if phase == "camera_info_absent" and key == "info":
                        continue
                    msg.header.stamp = ((now+Duration(seconds=20)).to_msg() if phase == "camera_info_wrong_clock"
                                        and key == "info" else now.to_msg())
                    publisher.publish(msg)
                time.sleep(.10)
        finally:
            if child.poll() is None:
                child.kill()
            out, err = child.communicate(timeout=5)
    if not out.strip():
        raise RuntimeError(f"{phase}: capture CLI produced no JSON: {err}")
    report = json.loads(out.splitlines()[-1])
    expected_code = 0 if phase in ("healthy","sensors_restored") else 2
    assert child.returncode == expected_code, (phase, err, report)
    assert report["motion_authorized"] is False
    status = subprocess.run([sys.executable, "-m", "calibration.factory", "status", "--state", str(run),
                             "--robot-id", profile["robot_id"], "--environment", "hardware"],
                            cwd=ROOT, capture_output=True, text=True, timeout=8)
    assert status.returncode == expected_code, (status.stdout, status.stderr)
    persisted = json.loads(status.stdout)
    assert all(persisted[k] == report[k] for k in persisted)
    if expected_code == 0:
        assert report["status"] == "VERIFIED"
        assert all(report["statistics"][key]["samples"] >= 5 for key in ("lidar","imu","rgbd"))
        assert report["calibration"]["gyro_bias_rad_s"] == [.002,-.001,0]
    else:
        assert report["status"] == "NEEDS_CALIBRATION" and report["revision"] is None
        expected_fault = {"lidar_unplugged":("lidar","capture.missing_stream"),
                          "camera_info_absent":("rgbd","capture.acquisition"),
                          "camera_info_wrong_clock":("rgbd","camera.sync")}[phase]
        assert expected_fault in {(i["source"],i["code"]) for i in report["issues"]}, report
    return {"phase":phase, "cli_exit":child.returncode, "status_exit":status.returncode,
            **{k:report[k] for k in ("status", "event_id", "operation_id", "revision", "last_verified_revision",
                                     "capture_sha256", "profile_sha256", "motion_authorized", "issues","statistics")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    profile, _ = fixture("dds-station-test-rover", "hardware")
    sources = profile["sensors"]
    sources["rgbd"].update(color_topic="/test/color", info_topic="/test/camera_info")
    bindings = {"robot_id":profile["robot_id"], "clock_domain":"ROS system clock",
                "stationary_reference":"synthetic-fixed-jig", "sensors":sources}
    with tempfile.TemporaryDirectory() as scratch:
        run = Path(scratch)
        for name, value in (("profile",profile), ("identity",bindings)):
            (run/(name+".json")).write_text(json.dumps(value), encoding="utf-8")
        rclpy.init(args=[])
        node = rclpy.create_node("independent_factory_station_publisher")
        try:
            streams = build_streams(node, profile)
            results = [run_capture(node, streams, profile, run, phase)
                       for phase in ("healthy","lidar_unplugged","camera_info_absent",
                                     "camera_info_wrong_clock","sensors_restored")]
        finally:
            node.destroy_node()
            rclpy.try_shutdown()
        assert all(r["last_verified_revision"] == results[0]["revision"] for r in results[1:-1])
        assert results[-1]["revision"] != results[0]["revision"]
        with closing(sqlite3.connect(run/"module-events.sqlite")) as db:
            events = [json.loads(row[0]) for row in db.execute("SELECT event FROM module_bus_events ORDER BY seq")]
            assert [e["event_id"] for e in events] == [r["event_id"] for r in results]
            assert [e.get("parent_event_id") for e in events[1:]] == [r["event_id"] for r in results[:-1]]
            assert db.execute("SELECT count(*) FROM factory_revision").fetchone() == (2,)
            for r in results:
                body = db.execute("SELECT body FROM factory_input WHERE hash=?", (r["capture_sha256"],)).fetchone()[0]
                assert hashlib.sha256(body.encode()).hexdigest() == r["capture_sha256"]
    evidence = {"spec":"SDD-038", "pass":True, "measured_at":datetime.now(timezone.utc).isoformat(),
                "ros_distro":os.environ.get("ROS_DISTRO"), "domain":os.environ.get("ROS_DOMAIN_ID"),
                "runtime_image":os.environ.get("FACTORY_TEST_IMAGE", ""),
                "base_image_revision":os.environ.get("EDGE_REVISION"),
                "scope":"Private Docker ROS domain. Real rclpy/DDS/CLI/SQLite, synthetic sensor messages and mounting evidence. No physical unit, driver, or actuator.",
                "sources_sha256":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
                                  [ROOT/"calibration/ros_capture.py", ROOT/"calibration/checks.py",
                                   ROOT/"calibration/factory.py", Path(__file__).resolve()]},
                "event_kinds":[e["kind"] for e in events], "phases":results}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    print(json.dumps({"pass":True,"phases":[r["status"] for r in results],"evidence":str(args.out)}))


if __name__ == "__main__":
    main()
