"""Collect immutable evidence on the Jetson and verify a recorded task offline.

    export_field_plan_evidence.py ref RECORDING_DIR
    export_field_plan_evidence.py collect SNAPSHOT_REF NEW_ZIP  # on the Jetson
    export_field_plan_evidence.py verify RECORDING_DIR ZIP      # on the workstation
    export_field_plan_evidence.py verify RECORDING_DIR          # verify the extracted files

Collection reads only bound content and a projection of live container configuration.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import time
import zipfile


PROJECT = "edge-field-plan-v1"
SERVICES = ("agent", "facts", "rover-preview", "rover-status", "web")
CLOUDS = ("mid360.front", "mid360.rear")


def require(condition, explanation):
    if not condition:
        raise ValueError(explanation)


def digest(blob):
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def hex_part(ref):
    require(isinstance(ref, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", ref), "invalid digest")
    return ref[7:]


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True).stdout


def runtime():
    services = {}
    for name in SERVICES:
        ids = docker("ps", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}",
                     "--filter", f"label=com.docker.compose.service={name}").split()
        require(len(ids) == 1, f"expected one running {name}")
        services[name] = ids[0].decode()
    services["pose-observer"] = "edge-field-plan-odom-v1"
    rows = {}
    for service, container in services.items():
        item = json.loads(docker("inspect", container))[0]
        host = item["HostConfig"]
        networks = {}
        for network in item["NetworkSettings"]["Networks"]:
            data = json.loads(docker("network", "inspect", network))[0]
            networks[network] = {"driver": data["Driver"], "internal": data["Internal"]}
        rows[service] = {
            "name": item["Name"].lstrip("/"), "image_id": item["Image"],
            "compose_project": item["Config"]["Labels"]["com.docker.compose.project"],
            "state": item["State"]["Status"], "started": item["State"]["StartedAt"],
            "restart_policy": host["RestartPolicy"]["Name"], "restart_count": item["RestartCount"],
            "rootfs_read_only": host["ReadonlyRootfs"], "privileged": host["Privileged"],
            "cap_drop": host["CapDrop"], "security_options": host["SecurityOpt"],
            "devices": [d["PathInContainer"] for d in (host["Devices"] or [])],
            "device_requests": len(host.get("DeviceRequests") or []),
            "network_mode": host["NetworkMode"], "networks": networks, "pid_mode": host["PidMode"],
            "ports": item["NetworkSettings"]["Ports"],
            "mounts": {m["Destination"]: {"type": m["Type"], "name": m.get("Name"), "writable": m["RW"]}
                       for m in item["Mounts"]},
        }
        if service == "agent":
            docker("exec", container, "python3", "-B", "-c",
                   "import os,sys; p=os.environ.get('EDGE_MOTION_TOKEN_FILE'); "
                   "sys.exit(0 if p=='/run/no-motion-grant' and not os.path.lexists(p) else 1)")
            rows[service]["motion_grant_absent"] = True
    return services["facts"], {"sampled_at_ns": str(time.time_ns()), "services": rows}


def media_refs(snapshot):
    source = snapshot["source_evidence"]
    clouds = source["lidars"]
    require(len(clouds) == 2 and {c["source_id"] for c in clouds} == set(CLOUDS), "dual LiDAR evidence absent")
    camera = source["camera"]
    require(camera and camera["source_id"] == "d435.color.image", "bound D435 frame absent")
    return {**{c["source_id"] + ".json": c["artifact_ref"] for c in clouds}, "d435.color.png": camera["ref"]}


def collect(ref, output):
    container, state = runtime()

    def fetch(kind, key, suffix):
        data = docker("exec", container, "cat", f"/evidence/{kind}/{hex_part(key)}{suffix}")
        require(digest(data) == key, "immutable artifact digest differs")
        return data

    snapshot = fetch("snapshots", ref, ".json")
    contents = {"snapshot.json": snapshot, "isolation.json": encoded(state),
                "sensor-calibration.json": docker("exec", container, "cat", "/site/jetson-rover.json")}
    for name, key in media_refs(json.loads(snapshot)).items():
        contents[name] = fetch("artifacts", key, Path(name).suffix)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, blob in contents.items():
            archive.writestr(name, blob)


def task_step(task):
    require(task["state"] == "SUCCEEDED", "task did not finish successfully")
    steps = [s for s in task["steps"] if s["tool"] == "preview_rover_obstacle_path"]
    require(len(steps) == 1 and steps[0]["status"] == "SUCCEEDED" and steps[0]["operation_id"],
            "one successful preview operation required")
    return steps[0]


def verify(directory, source):
    task_blob = (directory / "task.json").read_bytes()
    window_blob = (directory / "pose-window.json").read_bytes()
    task, window = json.loads(task_blob), json.loads(window_blob)
    step = task_step(task)
    report = step["evidence"]
    require(task["id"] == window["task_id"] and window["status"] == "completed", "pose window not bound")
    refs = {"snapshot.json": report["source_evidence"]["snapshot_ref"]}
    with zipfile.ZipFile(source) if source else nullcontext() as archive:
        def read(name):
            return archive.read(name) if archive else (directory / "sensor-evidence" / name).read_bytes()
        blobs = {k: read(k) for k in ("snapshot.json", "isolation.json", "sensor-calibration.json")}
        snap = json.loads(blobs["snapshot.json"])
        refs.update(media_refs(snap))
        for name in refs.keys() - blobs.keys():
            blobs[name] = read(name)
    for name, ref in refs.items():
        require(digest(blobs[name]) == ref, f"digest mismatch for {name}")
    evidence = {**snap["source_evidence"], "snapshot_ref": refs["snapshot.json"]}
    require(report["source_evidence"] == evidence, "archived sources differ from the tool receipt")
    canonical = json.dumps({**snap, "source_evidence": evidence}, sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    require(report["scan_sha256"] == digest(canonical), "provider's canonical scan digest differs")
    for key in ("source_id", "base_frame", "scan_stamp_ns", "calibration_ref"):
        require(snap[key] == report[key], f"tool/snapshot mismatch: {key}")
    require(snap["odom"]["stamp_ns"] == report["initial_pose"]["stamp_ns"], "initial odometry mismatch")
    cloud_times = []
    calibration = json.loads(blobs["sensor-calibration.json"])
    require(calibration["model_evidence"]["digest"] == snap["calibration_ref"], "calibration reference differs")
    for meta in evidence["lidars"]:
        cloud = json.loads(blobs[meta["source_id"] + ".json"])
        source_calibration = calibration["sources"][meta["source_id"]]
        require(source_calibration["frame_id"] == meta["frame_id"]
                and source_calibration["extrinsic_ref"] == snap["calibration_ref"], "LiDAR calibration differs")
        require(cloud["type"] == "livox.mid360.preview" and cloud["source_id"] == meta["source_id"]
                and cloud["frame_id"] == meta["frame_id"] and int(cloud["last_receive_ns"]) == int(meta["stamp_ns"])
                and int(meta["sampled_points"]) == cloud["sampled_point_count"] == len(cloud["points"]) > 0,
                "LiDAR source, frame, stamp or point count differs")
        cloud_times.append(int(meta["stamp_ns"]))
    require(max(cloud_times) == int(snap["scan_stamp_ns"]) and max(cloud_times)-min(cloud_times) <= 300_000_000,
            "scan/LiDAR stamp mismatch")
    png = blobs["d435.color.png"]
    require(png.startswith(b"\x89PNG\r\n\x1a\n") and png[12:16] == b"IHDR", "invalid color frame")
    size = struct.unpack(">II", png[16:24])
    require(min(size) > 0, "empty color frame")
    t0, t1 = [int(window[key]["pose"]["stamp_ns"]) for key in ("start_pose", "end_pose")]
    a, b = [int(report[key]["stamp_ns"]) for key in ("initial_pose", "end_observed_pose")]
    require(t0 <= a < b <= t1, "tool odometry samples outside task pose window")
    state = json.loads(blobs["isolation.json"])["services"]
    require(all(s["state"] == "running" and s["restart_policy"] == "unless-stopped" and s["rootfs_read_only"]
                and not s["privileged"] and "ALL" in s["cap_drop"] and not s["pid_mode"]
                and "no-new-privileges:true" in s["security_options"]
                and not s["devices"] and not s["device_requests"] for s in state.values()), "unexpected isolation")
    require(set(state) == set(SERVICES) | {"pose-observer"} and state["agent"]["motion_grant_absent"]
            and all(state[n]["network_mode"] != "host" for n in SERVICES)
            and all(not state["facts"]["mounts"][v]["writable"] for v in ("/pose", "/lidar", "/rgb"))
            and all(n["internal"] for svc in ("facts", "rover-preview", "rover-status")
                    for n in state[svc]["networks"].values()),
            "unexpected plan service access")
    manifest = {
        "schema_version": 1, "task_id": task["id"], "operation_id": step["operation_id"],
        "tool": step["tool"], "preview_status": report["status"], "source_id": snap["source_id"],
        "base_frame": snap["base_frame"], "calibration_ref": snap["calibration_ref"],
        "provider_scan_sha256": report["scan_sha256"], "lidars": evidence["lidars"],
        "color": {**evidence["camera"], "width": size[0], "height": size[1],
                  "signed_offset_from_scan_ms": (int(evidence["camera"]["stamp_ns"])-max(cloud_times))/1e6},
        "stamp_ns": {"task_start_pose": str(t0), "tool_start_pose": str(a),
                     "tool_end_pose": str(b), "task_end_pose": str(t1)},
        "files": {name: {"sha256": digest(blob)[7:], "bytes": len(blob)} for name, blob in {
            "../task.json": task_blob, "../pose-window.json": window_blob, **blobs}.items()},
    }
    output = directory / "sensor-evidence"
    blobs["manifest.json"] = encoded(manifest)
    if output.exists():
        require(all((output / name).read_bytes() == blob for name, blob in blobs.items()), "export differs")
        return
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        stage = Path(temporary) / "verified"
        stage.mkdir()
        for name, blob in blobs.items():
            (stage / name).write_bytes(blob)
        stage.rename(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="cmd", required=True)
    commands.add_parser("ref").add_argument("recording_dir", type=Path)
    capture = commands.add_parser("collect")
    capture.add_argument("snapshot_ref")
    capture.add_argument("new_zip", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("recording_dir", type=Path)
    check.add_argument("zip", type=Path, nargs="?")
    args = parser.parse_args()
    if args.cmd == "ref":
        report = task_step(json.loads((args.recording_dir / "task.json").read_bytes()))["evidence"]
        print("sha256:" + hex_part(report["source_evidence"]["snapshot_ref"]))
    elif args.cmd == "collect":
        collect(args.snapshot_ref, args.new_zip)
    else:
        verify(args.recording_dir, args.zip)
