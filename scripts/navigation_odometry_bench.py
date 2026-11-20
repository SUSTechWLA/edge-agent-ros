"""Generate an explicitly synthetic odometry-frame test map and read-only ROS bindings."""
import argparse
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_contracts.navigation import map_reference


def settings(topic, frame, child_frame, actor, epoch):
    width = 20
    cells = [100 if x == 12 and 8 <= y <= 11 else 0 for y in range(width) for x in range(width)]
    grid = {"site_id": "odometry-software-bench", "map_id": "synthetic-grid", "revision": "1",
            "kind": "occupancy", "frame_id": frame, "geometry": {"width": width, "height": width,
            "origin": [-2, -2, 0], "resolution": 0.2, "frame": frame, "cells": cells}}
    profile = "planar-bench"
    site = {"maps": {"test": grid}, "profiles": {profile: {"route_type": "ground", "dimensions": 2,
            "algorithm_ref": "astar", "clearance_m": 0.10}}, "actors": {actor: {"profile_ref": profile, "map_key": "test"}},
            "destinations": {"bench-marker": [{"map_key": "test", "profiles": [profile], "position_m": [1.1, 0.1]}]}}
    src, calibration = "wheel.odometry", "driver-reported-xy-covariance-v1"
    common = {"localization_epoch": epoch, "frame_id": frame, "clock_id": "unix", "map_ref": map_reference(grid)}
    policy = {**common, "sources": {src: calibration}, "max_age_ns": 1000000000, "max_skew_ns": 0}
    capture = {**common, "actor_ref": actor, "source_id": src, "calibration_ref": calibration,
               "topic": topic, "child_frame_id": child_frame}
    return {"navigation.maps": {"catalog": site}, "localization.weighted": {"actors": {actor: policy}}}, capture


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("topic", "frame", "child-frame", "actor"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    policies, source = settings(args.topic, args.frame, args.child_frame, args.actor, uuid.uuid4().hex)
    args.out.mkdir(parents=True, exist_ok=False)
    for name, data in (("settings", policies), ("odometry", source)):
        (args.out / (name + ".json")).write_text(json.dumps(data, indent=2) + "\n", encoding="utf8")
