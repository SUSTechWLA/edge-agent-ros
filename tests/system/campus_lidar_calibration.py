"""Check raw Gazebo GPU rays against asymmetric targets in a temporary copy of the campus."""
import argparse
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from threading import Event
from xml.etree import ElementTree as ET


TARGETS = ((-135, 2.0), (-45, 3.2), (45, 1.1), (135, 4.3))
THIN_TARGET = (10, .90)


def add(parent, tag, text=None, **attributes):
    node = ET.SubElement(parent, tag, attributes)
    if text is not None:
        node.text = str(text)
    return node


def fixture(campus):
    scene = ET.parse(campus)
    world = scene.getroot().find("world")
    rover = world.find("model[@name='edge_rover']")
    lidar = deepcopy(rover.find(".//sensor[@name='horizontal_lidar']"))
    lidar.find("topic").text = "/calibration/campus_lidar"
    # Place this second copy of the same instrument above the roofs, away from other objects.
    rig = add(world, "model", name="static_lidar_bench")
    add(rig, "static", "true")
    add(rig, "pose", "0 0 80 0 0 0")
    link = add(rig, "link", name="target_mount")
    link.append(lidar)
    sx, sy, sz, roll, pitch, yaw = map(float, lidar.findtext("pose").split())
    if any((roll, pitch, yaw)):
        raise ValueError("Expected the surveyed horizontal sensor")
    for degree, distance in (*TARGETS, THIN_TARGET):
        angle = math.radians(degree)
        thickness = .10
        radius = distance + thickness / 2
        width = .018 if degree == THIN_TARGET[0] else .52
        visual = add(link, "visual", name=f"board_{degree}")
        add(visual, "pose", f"{sx+radius*math.cos(angle)} {sy+radius*math.sin(angle)} {sz} 0 0 {angle}")
        add(add(add(visual, "geometry"), "box"), "size", f"{thickness} {width} .6")
    return scene


def measure(world_file):
    from gz.msgs10.laserscan_pb2 import LaserScan
    from gz.transport13 import Node

    done, scans = Event(), {}
    node = Node()

    def consume(msg):
        stamp = (msg.header.stamp.sec, msg.header.stamp.nsec)
        if len(scans) < 5:
            scans[stamp] = (float(msg.angle_min), float(msg.angle_step), list(msg.ranges))
        if len(scans) == 5:
            done.set()

    node.subscribe(LaserScan, "/calibration/campus_lidar", consume)
    with tempfile.TemporaryFile() as log:
        sim = subprocess.Popen(["gz", "sim", "-s", "-r", "--headless-rendering", str(world_file)],
                               stdout=log, stderr=log, start_new_session=True)
        try:
            for _ in range(90):
                if done.wait(.5) or sim.poll() is not None:
                    break
            if not done.is_set():
                log.seek(0)
                raise RuntimeError("Five unique sensor frames required. " + log.read()[-2000:].decode(errors="replace"))
        finally:
            if sim.poll() is None:
                os.killpg(sim.pid, signal.SIGTERM)
                try:
                    sim.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(sim.pid, signal.SIGKILL)
                    sim.wait()
    return scans


def check(scans):
    results = []
    for stamp, (start, step, ranges) in sorted(scans.items()):
        if len(ranges) != 360 or abs(step-math.radians(1)) > 1e-5:
            raise ValueError("Required the original campus 360-beam instrument")
        rays = []
        for degree, distance in (*TARGETS, THIN_TARGET):
            center = round((math.radians(degree)-start)/step)
            for offset in (-1, 0, 1):
                r = ranges[center+offset]
                expected = distance / math.cos(start+(center+offset)*step-math.radians(degree))
                if (degree, distance) == THIN_TARGET and offset:
                    expected = None
                valid = (not math.isfinite(r) or r > 7.9) if expected is None else abs(r-expected) < .06
                rays.append(dict(degrees=degree+offset, measured_m=r if math.isfinite(r) else None,
                                 expected_m=expected, passed=valid))
        results.append(dict(stamp=stamp, rays=rays))
    return dict(passed=all(r["passed"] for frame in results for r in frame["rays"]), scans=results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campus", type=Path, default=Path("/campus/world.sdf"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as work:
        sdf = Path(work)/"campus.sdf"
        fixture(args.campus).write(sdf, encoding="utf-8", xml_declaration=True)
        report = check(measure(sdf))
    args.out.write_text(json.dumps(report, allow_nan=False, indent=2), encoding="utf-8")
    print("Campus target ranges", [(r["degrees"],r["measured_m"]) for r in report["scans"][-1]["rays"]])
    raise SystemExit(0 if report["passed"] else 1)
