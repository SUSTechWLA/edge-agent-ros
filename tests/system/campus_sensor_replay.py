"""Capture real Gazebo campus frames, then exercise three isolated sensor.health instances."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.request

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.serialization import deserialize_message, serialize_message
from sensor_msgs.msg import Image, PointCloud2


SENSORS = {
    "crazyflie": ("rgb", Image, "/model/crazyflie/front/image_raw"),
    "go1": ("points", PointCloud2, "/model/go1/lidar/points"),
    "g1": ("depth", Image, "/model/g1/rgbd/depth/image_raw"),
}


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n")


def capture(root):
    root.mkdir(parents=True, exist_ok=True)
    if (root / "manifest.json").exists():
        raise FileExistsError("choose a new directory to protect the existing capture")
    node = Node("readonly_campus_frame_recorder", enable_rosout=False, start_parameter_services=False)
    frames = {name: [] for name in SENSORS}

    def receive(name, msg):
        samples = frames[name]
        stamp = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
        if len(samples) == 5 or samples and stamp - samples[-1]["stamp_ns"] < 100_000_000:
            return
        data = serialize_message(msg)
        filename = f"{name}-{len(samples) + 1}.cdr"
        (root / filename).write_bytes(data)
        samples.append({"filename": filename, "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data), "stamp_ns": stamp, "frame": msg.header.frame_id,
                        "width": msg.width, "height": msg.height})

    for name, (_, kind, topic) in SENSORS.items():
        node.create_subscription(kind, topic, lambda msg, name=name: receive(name, msg), qos_profile_sensor_data)
    deadline = time.monotonic() + 90
    try:
        while time.monotonic() < deadline and any(len(stream) < 5 for stream in frames.values()):
            rclpy.spin_once(node, timeout_sec=.25)
    finally:
        node.destroy_node()
        save(root / "manifest.json", {"source": "running Gazebo campus, ROS 2 unmodified CDR",
             "domain": os.getenv("ROS_DOMAIN_ID"), "topics": {n: s[2] for n, s in SENSORS.items()}, "samples": frames})
    counts = {name: len(v) for name, v in frames.items()}
    print("captured", counts, flush=True)
    assert min(counts.values()) == 5, counts


def corrupt(message, fault):
    if fault in ("occluded", "depth_holes"):
        message.data = bytes(len(message.data))
        return
    fields = {f.name: f for f in message.fields}
    coords = [(fields[name].offset, struct.Struct(
        (">" if message.is_bigendian else "<") + {7: "f", 8: "d"}[fields[name].datatype])) for name in "xyz"]
    chosen = bytearray()
    for y in range(message.height):
        for x in range(message.width):
            at = y * message.row_step + x * message.point_step
            if fault == "nan":
                for offset, fmt in coords:
                    fmt.pack_into(message.data, at + offset, float("nan"))
            elif fault == "sparse":
                xyz = [fmt.unpack_from(message.data, at + offset)[0] for offset, fmt in coords]
                if all(math.isfinite(v) for v in xyz) and any(v != 0 for v in xyz):
                    chosen.extend(message.data[at:at + message.point_step])
                    if len(chosen) == 8 * message.point_step:
                        message.width, message.height, message.row_step = 8, 1, len(chosen)
                        message.data = bytes(chosen)
                        return
            else:
                raise ValueError(fault)
    if fault == "sparse":
        raise AssertionError("source does not contain eight finite nonzero points")
    message.is_dense = False


def replay(root, out):
    manifest = json.loads((root / "manifest.json").read_text())
    if os.getenv("ROS_DOMAIN_ID") == manifest["domain"] or os.getenv("ROS_AUTOMATIC_DISCOVERY_RANGE") != "LOCALHOST":
        raise RuntimeError("replay requires a separate ROS domain and ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST")
    out.mkdir(parents=True, exist_ok=False)
    node = Node("isolated_campus_replay", enable_rosout=False)
    packets, publishers, processes, urls, logs, ledger = {}, {}, {}, {}, {}, {}
    ticks = 0
    sent = {}
    phases = []

    def status(name):
        with urllib.request.urlopen(urls[name] + "/status", timeout=1) as response:
            return json.load(response)["inputs"][SENSORS[name][0]]

    def push(faults):
        nonlocal ticks
        for name, (_, kind, _) in SENSORS.items():
            sample = ticks % len(packets[name])
            msg = deserialize_message(packets[name][sample], kind)
            if name in faults:
                corrupt(msg, faults[name])
            stamp = node.get_clock().now()
            msg.header.stamp = stamp.to_msg()
            sent[stamp.nanoseconds] = {"filename": manifest["samples"][name][sample]["filename"],
                                       "payload": faults.get(name, "original")}
            publishers[name].publish(msg)
        ticks += 1

    def phase(label, faults, expected):
        deadline = time.monotonic() + 10
        observations = {}
        while time.monotonic() < deadline:
            push(faults)
            time.sleep(.13)
            try:
                observations = {name: status(name) for name in SENSORS}
            except OSError:
                continue
            if all((r["state"], r["code"]) == expected[n] and r["latest"].get("measurement_code") == r["code"]
                   for n, r in observations.items()):
                if all(sent.get(r["latest"]["stamp_ns"], {}).get("payload") == faults.get(n, "original")
                       for n, r in observations.items()):
                    break
        else:
            raise AssertionError((label, expected, observations))
        phases.append({"phase": label, "inputs": faults or "original", "observations": observations})

    healthy = {name: ("observed", "ok") for name in SENSORS}
    try:
        for index, (name, (key, kind, topic)) in enumerate(SENSORS.items()):
            samples = manifest["samples"][name]
            assert topic == manifest["topics"][name] and len(samples) >= 3
            packets[name] = []
            for sample in samples:
                data = (root / sample["filename"]).read_bytes()
                assert hashlib.sha256(data).hexdigest() == sample["sha256"]
                packets[name].append(data)
            publishers[name] = node.create_publisher(kind, topic, qos_profile_sensor_data)
            port = 18137 + index
            urls[name] = f"http://127.0.0.1:{port}"
            ledger[name] = out / (name + ".sqlite")
            logs[name] = (out / (name + ".log")).open("w")
            env = {k: v for k, v in os.environ.items() if not k.startswith("EDGE_HEALTH_")}
            env.update({f"EDGE_HEALTH_{key.upper()}_TOPIC": topic,
                        f"EDGE_HEALTH_{key.upper()}_FRAME": samples[0]["frame"],
                        "EDGE_HEALTH_PORT": str(port), "EDGE_HEALTH_JOURNAL": str(ledger[name]),
                        "EDGE_HEALTH_RECHECK_S": "0.8", "EDGE_HEALTH_TIMEOUT_S": "8"})
            processes[name] = subprocess.Popen([sys.executable, "-B", "/opt/edge/perception/sensor_health/provider.py",
                "--ros-args", "-r", f"__ns:=/model/{name}"], env=env, stdout=logs[name], stderr=subprocess.STDOUT)
        phase("campus_baseline", {}, healthy)
        for name, fault, code in (("crazyflie", "occluded", "black_image"), ("go1", "sparse", "few_points"),
                                  ("go1", "nan", "invalid_cloud_points"), ("g1", "depth_holes", "depth_holes")):
            if fault == "sparse":
                healthy[name] = ("observed", code)
                phase("go1_eight_valid_points", {name: fault}, healthy)
                continue
            for state in ("rechecking", "needs_attention"):
                phase(f"{name}_{state}", {name: fault}, {**healthy, name: (state, code)})
            healthy[name] = ("recovered", "ok")
            phase(name + "_restored", {}, healthy)
        events = {}
        for name in SENSORS:
            db = sqlite3.connect(f"file:{ledger[name]}?mode=ro", uri=True)
            try:
                events[name] = [dict(seq=seq, **json.loads(body)) for seq, body in
                                db.execute("SELECT seq,event FROM module_bus_events ORDER BY seq")]
            finally:
                db.close()
        all_ids = set()
        for name, rows in events.items():
            transitions = [event for event in rows if event["kind"] != "sensor.health.started"]
            expected_events = [("observed", "ok")]
            if name == "go1":
                expected_events.append(("observed", "few_points"))
            error = {"crazyflie": "black_image", "go1": "invalid_cloud_points", "g1": "depth_holes"}[name]
            expected_events += [("rechecking", error), ("needs_attention", error), ("recovered", "ok")]
            assert [(r["data"]["phase"], r["code"].removeprefix("sensor.")) for r in transitions] == expected_events
            for i, entry in enumerate(transitions):
                assert entry.get("parent_event_id") == (transitions[i-1]["event_id"] if i else None)
                assert entry["data"]["topic"] == SENSORS[name][2]
                assert entry["event_id"] not in all_ids
                all_ids.add(entry["event_id"])
                entry["replay_input"] = sent[entry["data"]["observation"]["stamp_ns"]]
            assert transitions[-1]["data"]["consecutive_good"] == 3
        save(out / "result.json", {"passed": True, "source_manifest": manifest, "replay_domain": os.getenv("ROS_DOMAIN_ID"),
             "retiming": "only ROS header.stamp replaced by isolated wall-clock time; faults applied to payload",
             "published_frames_per_robot": ticks, "phases": phases, "journal": events})
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        summary = {"passed": True, "image": os.getenv("EDGE_VERIFICATION_IMAGE"), "capture_domain": manifest["domain"],
                   "replay_domain": os.getenv("ROS_DOMAIN_ID"), "phases": len(phases), "sensor_events": len(all_ids),
                   "manifest_sha256": digest(root / "manifest.json"), "result_sha256": digest(out / "result.json"),
                   "source": "five distinct original ROS 2 CDR frames per robot from running Gazebo campus",
                   "replay": "DDS loopback only; sensor payloads original except named injected faults; stamps retimed",
                   "robots": {}}
        for name, rows in events.items():
            samples = manifest["samples"][name]
            summary["robots"][name] = {"topic": SENSORS[name][2], "frame": samples[0]["frame"],
                "stamp_range_ns": [samples[0]["stamp_ns"], samples[-1]["stamp_ns"]],
                "source_samples": {s["filename"]: s["sha256"] for s in samples},
                "transitions": [{"seq": e["seq"], "event_id": e["event_id"], "parent": e.get("parent_event_id"),
                                 "utc": e["utc"], "state": e["data"]["phase"], "code": e["code"],
                                 "metrics": e["data"]["observation"]["metrics"], "elapsed_s": e["data"]["elapsed_s"],
                                 "consecutive_good": e["data"]["consecutive_good"], "input": e["replay_input"]}
                                for e in rows if e["kind"] != "sensor.health.started"]}
        save(out / "summary.json", summary)
        print(f"passed {len(phases)} phases, {len(all_ids)} sensor transitions", flush=True)
    finally:
        for proc in processes.values():
            proc.send_signal(signal.SIGTERM)
        for proc in processes.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for log in logs.values():
            log.close()
        node.destroy_node()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture", type=Path)
    mode.add_argument("--replay", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    rclpy.init()
    try:
        if args.capture:
            capture(args.capture)
        else:
            if args.out is None:
                parser.error("--replay requires --out")
            replay(args.replay, args.out)
    finally:
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
