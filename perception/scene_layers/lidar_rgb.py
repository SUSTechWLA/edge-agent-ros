"""Register finite lidar returns against a rectified RGB-D exposure in a pinned map frame."""
from array import array
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path
import struct
import sys

from calibration.checks import count, ident, pinhole, vector
from .core import SceneError, bounded_bytes, canonical, digest, is_ref, make_layer, number, reference, require


def _timestamp(value):
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19:
        value = int(value)
    require(count(value, 1, 2**63 - 1), "scene.time_mismatch", "Expected a positive acquisition stamp in ns")
    return value


def _transform(matrix):
    require(isinstance(matrix, list) and len(matrix) == 4 and all(vector(row, 4) for row in matrix)
            and matrix[3] == [0, 0, 0, 1], "scene.invalid_calibration", "Expected a finite 4x4 rigid transform")
    a, b, c = [row[:3] for row in matrix[:3]]
    # Reject scale, skew and reflections. A transpose is an inverse only for a proper rotation.
    def dot(u, v):
        return sum(x*y for x, y in zip(u, v))
    orthogonal = max(abs(dot(u, u) - 1) for u in (a, b, c)) < 1e-5
    perpendicular = max(abs(dot(u, v)) for u, v in ((a, b), (b, c), (c, a))) < 1e-5
    determinant = dot(a, (b[1]*c[2] - b[2]*c[1], b[2]*c[0] - b[0]*c[2], b[0]*c[1] - b[1]*c[0]))
    require(orthogonal and perpendicular and abs(determinant - 1) < 1e-5, "scene.invalid_calibration",
            "The optical/lidar extrinsic must contain a proper rotation")
    return matrix[:3]


def _map_to_optical(matrix, point):
    d = [p - r[3] for p, r in zip(point, matrix)]
    return [sum(matrix[i][j]*d[i] for i in range(3)) for j in range(3)]


def _sample(optical, camera, rgb, depth, tolerance):
    x, y, z = optical
    if z <= 0:
        return None, "behind_camera"
    k, w, h = camera["k"], camera["width"], camera["height"]
    u, v = k[0]*x/z + k[2], k[4]*y/z + k[5]
    if not (-.5 <= u < w-.5 and -.5 <= v < h-.5):
        return None, "outside_fov"
    ix, iy = math.floor(u+.5), math.floor(v+.5)
    idx = iy*w + ix
    visible_z = depth[idx]
    if not math.isfinite(visible_z) or visible_z <= 0:
        return None, "unknown_depth"
    if z > visible_z + tolerance:
        return None, "occluded"
    if z < visible_z - tolerance:
        return None, "depth_disagreement"
    # Avoid borrowing a colour across a silhouette on the RGB grid.
    for nx, ny in ((ix-1, iy), (ix+1, iy), (ix, iy-1), (ix, iy+1)):
        if 0 <= nx < w and 0 <= ny < h:
            adjacent = depth[ny*w + nx]
            if not math.isfinite(adjacent) or adjacent <= 0 or abs(visible_z-adjacent) > tolerance:
                return None, "depth_boundary"
    return list(rgb[3*idx:3*idx+3]), None


def fuse_clouds(capture, calibration, rgb, depth_m, *, max_skew_ms=50., depth_tolerance_m=.08):
    """Fuse one static/pose-compensated window. XYZ is in metres and depth is optical Z.

    Calibration is a separately pinned record. A directory adapter verifies its bytes before calling here.
    Each output point retains its input source and index, including those without an observed colour.
    """
    require(number(max_skew_ms) and max_skew_ms >= 0 and number(depth_tolerance_m) and depth_tolerance_m > 0,
            "scene.invalid_query", "Time and depth agreement thresholds must be finite and nonnegative")
    frame, clock = capture["frame_id"], capture["clock_id"]
    camera, clouds = capture["camera"], capture["lidars"]
    require(ident(frame) and ident(clock) and frame == calibration.get("frame_id") and
            clock == calibration.get("clock_id") and ident(calibration.get("source")), "scene.missing_registration",
            "A site-pinned acquisition frame, clock and calibration source are required")
    require(ident(camera.get("frame_id")) and camera.get("depth_frame_id") == camera["frame_id"]
            and camera.get("projection_model") == "rectified_pinhole"
            and camera.get("depth_convention") == "optical_z_m"
            and not any(camera.get("d", [])), "scene.invalid_calibration", "RGB requires registered rectified optical depth")
    expected_cam = calibration.get("camera", {})
    require(all(k in expected_cam and camera.get(k) == expected_cam[k] for k in
                ("frame_id", "width", "height", "k", "T_map_camera")), "scene.missing_registration",
            "Camera projection or pose differs from the site calibration")
    require(pinhole(camera) and camera["k"][1] == 0 and camera["width"]*camera["height"] <= 2_073_600,
            "scene.invalid_calibration", "Unsupported pinhole model or image size")
    require(isinstance(rgb, (bytes, bytearray)) and len(rgb) == 3*len(depth_m)
            and len(depth_m) == camera["width"]*camera["height"], "scene.invalid_image",
            "RGB and depth sizes must match the pinned intrinsics")
    optical = _transform(camera["T_map_camera"])
    require(isinstance(clouds, list) and 1 <= len(clouds) <= 16 and
            isinstance(calibration.get("lidars"), list), "scene.missing_registration", "Lidar poses are required")
    require(all(row.get("clock_id") == clock for row in [camera, *clouds])
            and camera.get("depth_clock_id", camera["clock_id"]) == clock,
            "scene.clock_mismatch", "All sensors must share one evidenced clock")
    stamps = [_timestamp(x["stamp_ns"]) for x in [camera, *clouds]] + [_timestamp(camera["depth_stamp_ns"])]
    span_ms = (max(stamps)-min(stamps))/1_000_000
    require(span_ms <= max_skew_ms, "scene.time_skew", f"Acquisition span {span_ms:.3f} ms exceeds {max_skew_ms} ms")
    rigs = {r["source_id"]: r for r in calibration["lidars"]}
    require(len(rigs) == len(calibration["lidars"]) and len({s["source_id"] for s in clouds}) == len(clouds),
            "scene.source_mismatch", "Duplicate lidar identity")
    require(all(isinstance(s.get("points"), list) for s in clouds)
            and sum(len(s["points"]) for s in clouds) <= 65536, "scene.capture_size", "Exceeded per-window point budget")
    points, colors, contributors = [], [], []
    rejected, counts = Counter(), {}
    for cloud in clouds:
        source = cloud["source_id"]
        rig = rigs.get(source)
        require(ident(source) and ident(cloud.get("frame_id")) and rig
                and all(k in rig and cloud.get(k) == rig[k] for k in ("frame_id", "T_map_lidar")),
                "scene.missing_registration", "Lidar source frame or pose differs from the site calibration")
        transform = _transform(cloud["T_map_lidar"])
        stats = {"input": len(cloud["points"]), "finite": 0, "colored": 0}
        counts[source] = stats
        for idx, xyz in enumerate(cloud["points"]):
            if not vector(xyz, 3):
                rejected["invalid_xyz"] += 1
                continue
            point = [r[0]*xyz[0] + r[1]*xyz[1] + r[2]*xyz[2] + r[3] for r in transform]
            color, reason = _sample(_map_to_optical(optical, point), camera, rgb, depth_m, depth_tolerance_m)
            stats["finite"] += 1
            if reason:
                rejected[reason] += 1
            else:
                stats["colored"] += 1
            points.append(point)
            colors.append(color)
            contributors.append([source, idx])
    colored = sum(r["colored"] for r in counts.values())
    return {"schema_version": 1, "frame_id": frame, "clock_id": clock, "stamp_ns": str(stamps[0]),
            "acquisition": [str(min(stamps)), str(max(stamps))], "points": points, "colors": colors,
            "contributors": contributors, "motion_authorized": False, "calibration_source": calibration["source"],
            "max_skew_ms": max_skew_ms, "depth_tolerance_m": depth_tolerance_m,
            "stats": {"points": len(points), "colored": colored, "uncolored": len(points)-colored,
                      "sources": counts, "rejections": dict(rejected), "acquisition_span_ms": span_ms}}


class FusionArchive:
    """DSH read-only adapter for one immutable registered RGB-D / lidar capture."""
    def __init__(self, directory, manifest_ref, *, max_skew_ms=50., depth_tolerance_m=.08):
        root = Path(directory).resolve()
        raw = bounded_bytes(root, "capture.json", 12*1024*1024)
        require(is_ref(manifest_ref) and digest(raw) == manifest_ref, "scene.manifest_mismatch",
                "Fusion capture differs from the separately pinned revision")
        try:
            capture = json.loads(raw)
            require(type(capture.get("schema_version")) is int and capture["schema_version"] == 1,
                    "scene.archive_invalid", "Unsupported fusion archive format")
            files, camera = capture["files"], capture["camera"]
            require(camera["rgb_file"] == "rgb.raw" and camera["depth_file"] == "depth.f32", "scene.invalid_image",
                    "Expected packed RGB24 and little-endian float32 optical depth")

            def read(name, limit):
                raw = bounded_bytes(root, name, limit)
                require(digest(raw) == files.get(name), "scene.artifact_mismatch", f"Digest differs for {name}")
                return raw

            calibration = json.loads(read("calibration.json", 65536))
            rgb = read("rgb.raw", 3*2_073_600)
            z = array("f")
            z.frombytes(read("depth.f32", 4*2_073_600))
            if sys.byteorder != "little":
                z.byteswap()
            fused = fuse_clouds(capture, calibration, rgb, z, max_skew_ms=max_skew_ms, depth_tolerance_m=depth_tolerance_m)
            refs = {"capture": manifest_ref, "calibration": files["calibration.json"], "rgb": files["rgb.raw"],
                    "aligned_depth": files["depth.f32"]}
            fused["source_refs"] = refs
            self.raw = canonical(fused)
            require(len(self.raw) <= 5*1024*1024, "scene.capture_size", "Fused window exceeds the DSH reply budget")
            meta = {"schema_version": 1, "kind": "fusion", "representation": "registered_lidar_rgb_points",
                    "frame_id": fused["frame_id"], "clock_id": fused["clock_id"], "stamp_ns": fused["stamp_ns"],
                    "acquisition": fused["acquisition"], "stamp_basis": "sensor_acquisition", "source_refs": refs,
                    "space_ref": reference(["registered_window", manifest_ref, fused["frame_id"]]),
                    "calibration_ref": refs["calibration"], "content_ref": digest(self.raw), "stats": fused["stats"],
                    "quality": {"coordinate_unit": "m", "registration": calibration["source"],
                                "color_validity": "aligned_optical_depth", "outside_observations": "unknown",
                                "resolution_m": None, "error_bound_m": None}}
            self.layer = make_layer(meta, manifest_ref)
        except SceneError:
            raise
        except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
            raise SceneError("scene.archive_invalid", f"Invalid registered sensor capture: {exc}") from exc

    def describe(self, kind):
        require(kind == "fusion", "scene.unsupported_layer", "Only the registered lidar RGB layer is available")
        return deepcopy(self.layer)

    def read(self, kind, layer_ref):
        layer = self.describe(kind)
        require(layer_ref == layer["layer_ref"], "scene.layer_mismatch", "Fused map revision differs")
        return {"layer": layer, "data": json.loads(self.raw)}


def encode_ply(data):
    """PLY retains uncoloured returns and records colour validity and input identity."""
    sources = sorted(data["stats"]["sources"])
    source_indices = {key: i for i, key in enumerate(sources)}
    station_ids, station_indices = [], bytearray()
    total = len(data["points"])
    header = ["ply", "format binary_little_endian 1.0", "comment frame " + data["frame_id"],
              "comment unit m", "comment source_ids " + json.dumps(sources), "comment stamp_ns " + data["stamp_ns"]]
    if "windows" in data:
        windows = data["windows"]
        require(isinstance(windows, list) and 1 <= len(windows) <= 256, "scene.invalid_station",
                "Expected a bounded station index for every multi-window cloud")
        for i, window in enumerate(windows):
            require(isinstance(window, dict) and ident(window.get("id")) and window["id"] not in station_ids
                    and type(window.get("start")) is int and window["start"] == len(station_indices)
                    and count(window.get("count"), 0, total-len(station_indices)), "scene.invalid_station",
                    "Station ranges must uniquely and consecutively cover the point cloud")
            station_ids.append(window["id"])
            station_indices.extend(bytes([i])*window["count"])
        require(len(station_indices) == total, "scene.invalid_station", "Incomplete station ranges")
        header.append("comment station_ids " + json.dumps(station_ids))
    header.extend([f"element vertex {total}", "property float x", "property float y", "property float z",
                   "property uchar red", "property uchar green", "property uchar blue", "property uchar color_valid",
                   "property uchar source", "property uint source_index"])
    if station_ids:
        header.append("property uchar station")
    header.extend(["end_header", ""])
    payload = bytearray("\n".join(header).encode("ascii"))
    pack = struct.Struct("<fffBBBBBI").pack
    rows = zip(data["points"], data["colors"], data["contributors"], strict=True)
    for i, (xyz, rgb, (source, index)) in enumerate(rows):
        payload.extend(pack(*xyz, *(rgb or (0, 0, 0)), int(rgb is not None), source_indices[source], index))
        if station_ids:
            payload.append(station_indices[i])
    return bytes(payload)
