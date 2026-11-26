"""Validate the physical sensor bindings used by the read-only calibration station."""
import argparse
import json
import math
from pathlib import Path
import re


METHODS_DIR = Path(__file__).resolve().parent / "station" / "methods"


def _positive_meter(value, lo, hi, label):
    if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"{label} must be in metres within [{lo}, {hi}] (check mm vs m units)")
    return float(value)


def validate_board_metrics(profile):
    method = profile.get("calibration_method", "checkerboard")
    installed = {p.stem for p in METHODS_DIR.glob("*.json")} if METHODS_DIR.is_dir() else {"checkerboard", "four_hole_board"}
    if not isinstance(method, str) or method not in installed:
        raise ValueError(f"unknown calibration_method {method!r}; installed: {', '.join(sorted(installed))}")
    if method == "four_hole_board":
        board = profile.get("four_hole_board")
        if board is not None and not isinstance(board, dict):
            raise ValueError("four_hole_board must be an object")
        if board and (board.get("confirmed") is True or any(k in board for k in (
                "marker_size_m", "marker_spacing_m", "hole_spacing_m", "hole_diameter_m"))):
            marker = _positive_meter(board.get("marker_size_m"), 0.02, 1.0, "four_hole_board.marker_size_m")
            hole = _positive_meter(board.get("hole_diameter_m"), 0.03, 1.0, "four_hole_board.hole_diameter_m")
            m_space = board.get("marker_spacing_m")
            h_space = board.get("hole_spacing_m")
            if (not isinstance(m_space, list) or len(m_space) != 2 or
                    not isinstance(h_space, list) or len(h_space) != 2):
                raise ValueError("four_hole_board marker_spacing_m and hole_spacing_m must be [x_m, y_m]")
            mx = _positive_meter(m_space[0], marker, 4.0, "four_hole_board.marker_spacing_m[0]")
            my = _positive_meter(m_space[1], marker, 4.0, "four_hole_board.marker_spacing_m[1]")
            hx = _positive_meter(h_space[0], hole, 4.0, "four_hole_board.hole_spacing_m[0]")
            hy = _positive_meter(h_space[1], hole, 4.0, "four_hole_board.hole_spacing_m[1]")
            if mx <= marker or my <= marker or hx <= hole or hy <= hole:
                raise ValueError("center-to-center spacing must exceed marker size / hole diameter")


def validate_hardware_profile(profile, *, require_clock=True):
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
    if camera.get("image_geometry") not in ("raw", "rectified"):
        raise ValueError("set RGB image geometry to raw or rectified")
    if require_clock and (type(camera.get("clock_exposure_bound_ns")) is not int
                          or not 0 < camera["clock_exposure_bound_ns"] <= 100_000_000):
        raise ValueError("set the measured exposure/readout bound in ns")
    for s in lidars:
        kind = s.get("message_type", "sensor_msgs/msg/PointCloud2")
        if kind not in ("sensor_msgs/msg/PointCloud2", "livox_ros_driver2/msg/CustomMsg"):
            raise ValueError(f"unsupported calibration point message type: {kind}")
        if not require_clock:
            continue
        if kind != "sensor_msgs/msg/PointCloud2":
            raise ValueError("the temporal calibration observer requires PointCloud2 with explicit acquisition times")
        p = s.get("clock_point_time", {})
        if not name(p.get("field")) or p.get("mode") not in ("relative", "absolute") or p.get("unit") not in ("s", "ns"):
            raise ValueError("set the per-point acquisition time convention for each LiDAR")
    if type(profile.get("max_skew_ns")) is not int or not 0 < profile["max_skew_ns"] <= 50_000_000:
        raise ValueError("time association budget must be positive and at most 50 ms")
    validate_board_metrics(profile)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--spatial-only", action="store_true",
                        help="validate static extrinsic acquisition without requiring per-point timing")
    args = parser.parse_args()
    try:
        validate_hardware_profile(json.loads(args.profile.read_bytes()), require_clock=not args.spatial_only)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.exit(2, f"Invalid profile: {exc}\n")
    print("Read-only hardware profile is complete. Check the bindings against the live ROS publishers.")
