"""Validate the physical sensor bindings used by the read-only calibration station."""
import argparse
import json
from pathlib import Path
import re


def validate_hardware_profile(profile):
    def name(value):
        return isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./-]{0,127}", value)

    if (profile.get("environment") != "hardware" or not name(profile.get("robot_id"))
            or not name(profile.get("clock_domain")) or profile.get("clock_domain") == "gazebo_sim"):
        raise ValueError("set the physical robot identity and measured clock domain")
    camera, lidars, solver = profile["camera"], profile["lidars"], profile["solver"]
    sensors = [camera, *lidars]
    if not lidars or any(not name(s.get(k)) for s in sensors for k in ("serial", "id", "frame")):
        raise ValueError("set device serials, source IDs and native frames")
    for k in ("serial", "frame", "id"):
        if len({s[k] for s in sensors}) != len(sensors):
            raise ValueError(f"duplicate device {k}")
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", s["id"]) for s in sensors) or any(
            s["id"] in ("camera", "info") for s in lidars):
        raise ValueError("invalid or reserved sensor role")
    topics = [camera.get("info_topic"), *(s.get("topic") for s in sensors)]
    if any(not isinstance(t,str) or not re.fullmatch(r"/[A-Za-z_][A-Za-z_0-9]*(/[A-Za-z_][A-Za-z_0-9]*)*", t) for t in topics) or len(set(topics)) != len(topics):
        raise ValueError("set distinct absolute ROS topic names")
    if solver["camera_frame"] != camera["frame"] or solver["lidar_frames"] != {s["id"]:s["frame"] for s in lidars}:
        raise ValueError("solver frames differ from physical sources")
    if (camera.get("image_geometry") not in ("raw", "rectified") or
            type(camera.get("clock_exposure_bound_ns")) is not int or not 0 < camera["clock_exposure_bound_ns"] <= 100_000_000):
        raise ValueError("set RGB image geometry and the measured exposure/readout bound in ns")
    for s in lidars:
        p = s.get("clock_point_time", {})
        if not name(p.get("field")) or p.get("mode") not in ("relative", "absolute") or p.get("unit") not in ("s", "ns"):
            raise ValueError("set the per-point acquisition time convention for each LiDAR")
    if type(profile.get("max_skew_ns")) is not int or not 0 < profile["max_skew_ns"] <= 50_000_000:
        raise ValueError("time association budget must be positive and at most 50 ms")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    args = parser.parse_args()
    try:
        validate_hardware_profile(json.loads(args.profile.read_bytes()))
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.exit(2, f"Invalid profile: {exc}\n")
    print("Read-only hardware profile is complete. Check the bindings against the live ROS publishers.")
