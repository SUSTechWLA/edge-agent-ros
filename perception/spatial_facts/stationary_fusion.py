"""Stationary, body-local surface returns from calibrated spatial observation artifacts.

A grid cell is positive evidence of sampled surfaces in the configured height band.
Empty cells stay unknown. Local snapshots never acquire map/TF or motion authority.
"""
import hashlib
import json
import math
import re
import struct
import zlib

from edge_contracts.spatial_facts import MAX_STAMP_NS, SpatialFactError, check_batch, digest_ref, identifier, require
from edge_contracts.image_frames import TOPIC
from perception.map3d.geometry import rotation


LIMITS = {
    "resolution_m": (.2, .05, 1.0), "radius_m": (6.0, .5, 15.0),
    "z_min_m": (.10, -3., 4.), "z_max_m": (1.8, -2., 8.), "min_range_m": (0., 0., 3.),
    "max_age_ns": (2_000_000_000, 1, 5_000_000_000),
    "max_skew_ns": (300_000_000, 0, 500_000_000),
    "max_gap_ns": (200_000_000, 1, 300_000_000),
    "min_witness_ns": (450_000_000, 250_000_000, 3_000_000_000),
    "max_linear_mps": (.04, 0., .06), "max_angular_radps": (.08, 0., .12),
    "max_displacement_m": (.025, 0., .05),
}


def finite(value):
    return type(value) in (float, int) and -1e12 <= value <= 1e12


def stamp(value):
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,18}", value):
        value = int(value)
    require(type(value) is int and 0 <= value <= MAX_STAMP_NS, "fusion.invalid_stamp")
    return value


def vector(value, size):
    return isinstance(value, (list, tuple)) and len(value) == size and all(finite(n) for n in value)


def policy(profile):
    require(isinstance(profile, dict) and all(identifier(profile.get(k)) for k in
            ("actor_ref", "body_frame", "witness_source")) and isinstance(profile.get("sources"), dict)
            and 2 <= len(profile["sources"]) <= 6 and all(identifier(k) for k in profile["sources"]),
            "fusion.invalid_profile")
    settings = {}
    for key, (default, low, high) in LIMITS.items():
        value = profile.get(key, default)
        require(finite(value) and low <= value <= high and (not key.endswith("_ns") or type(value) is int),
                "fusion.invalid_limit")
        settings[key] = value
    require(settings["z_min_m"] < settings["z_max_m"] and settings["min_range_m"] < settings["radius_m"]
            and math.ceil(settings["radius_m"] / settings["resolution_m"]) <= 100, "fusion.invalid_grid")
    return settings


def content(ref, store, limit):
    require(digest_ref(ref) and isinstance(store, dict), "fusion.invalid_artifact")
    data = store.get(ref)
    require(isinstance(data, bytes) and 0 < len(data) <= limit, "fusion.artifact_unavailable")
    require(hashlib.sha256(data).hexdigest() == ref[7:], "fusion.artifact_digest_mismatch")
    return data


def json_object(raw):
    try:
        row = json.loads(raw)
    except (ValueError, RecursionError) as err:
        raise SpatialFactError("fusion.invalid_artifact_json") from err
    require(isinstance(row, dict), "fusion.invalid_artifact_json")
    return row


def batch_samples(batch, profile):
    require(isinstance(batch, dict) and isinstance(batch.get("samples"), list)
            and 0 < len(batch["samples"]) <= 32, "fusion.invalid_batch")
    normalized = dict(batch)
    normalized["as_of_ns"] = stamp(batch.get("as_of_ns"))
    samples = []
    for sample in batch["samples"]:
        require(isinstance(sample, dict), "fusion.invalid_batch")
        samples.append({**sample, "stamp_ns": stamp(sample.get("stamp_ns"))})
    normalized["samples"] = samples
    check_batch(normalized)
    require(batch["actor_ref"] == profile["actor_ref"] and batch["clock_id"] == "unix"
            and batch["basis"] == "published", "fusion.batch_binding_mismatch")
    return {s["source_id"]: s for s in samples if s["kind"] == "artifact"}


def source_data(batch, artifacts, profile):
    samples = batch_samples(batch, profile)
    inputs = {}
    for sid, cfg in sorted(profile["sources"].items()):
        require(sid in samples, "fusion.source_missing")
        sample = samples[sid]
        require(isinstance(cfg, dict) and identifier(cfg.get("frame_id")) and sample["frame_id"] == cfg["frame_id"]
                and sample["clock_id"] == "unix", "fusion.source_binding_mismatch")
        first = last = sample["stamp_ns"]
        if cfg.get("kind") == "livox":
            require(sample["media_type"] == "application/json", "fusion.wrong_media")
            row = json_object(content(sample["artifact_ref"], artifacts, 65536))
            require(row.get("type") == "livox.mid360.preview" and row.get("schema_version") == 1
                    and row.get("source_id") == sid and row.get("frame_id") == sample["frame_id"]
                    and row.get("stamp_basis") == "host_packet_receive", "fusion.preview_binding_mismatch")
            first, last = stamp(row.get("first_receive_ns")), stamp(row.get("last_receive_ns"))
            require(last == sample["stamp_ns"] and 0 <= last - first <= 400_000_000, "fusion.invalid_window")
            require(type(row.get("device_time_type")) is int and row["device_time_type"] in (0, 1, 2)
                    and stamp(row.get("first_device_stamp_ns")) <= stamp(row.get("last_device_stamp_ns")),
                    "fusion.invalid_device_time")
            data = row
        else:
            require(cfg.get("kind") == "depth16" and sample["media_type"] == "image/png", "fusion.wrong_media")
            data = content(sample["artifact_ref"], artifacts, 4 * 1024 * 1024)
        inputs[sid] = (sample, cfg, first, last, data)
    return inputs


def source_windows(batch, artifacts, profile):
    """Timing helper for a caller collecting an independent bracketing odometry window."""
    policy(profile)
    return {sid: (first, last) for sid, (_, _, first, last, _) in source_data(batch, artifacts, profile).items()}


def validate_witness(witness, profile, rules, first, last, now):
    require(isinstance(witness, dict) and witness.get("actor_ref") == profile["actor_ref"]
            and witness.get("source_id") == profile["witness_source"] and witness.get("clock_id") == "unix"
            and witness.get("frame_id") == profile.get("witness_frame", "odom")
            and witness.get("body_frame", witness.get("child_frame_id")) == profile["body_frame"],
            "fusion.witness_binding_mismatch")
    require(witness.get("stationary") is True, "fusion.motion_detected")
    start, end = stamp(witness.get("first_ns")), stamp(witness.get("last_ns"))
    require(start <= first <= last <= end <= now and rules["min_witness_ns"] <= end - start <= 5_000_000_000
            and now - end <= rules["max_age_ns"], "fusion.witness_coverage")
    n, gap = witness.get("sample_count"), stamp(witness.get("max_gap_ns"))
    require(type(n) is int and 3 <= n <= 8192 and 0 < gap <= rules["max_gap_ns"]
            and (n - 1) * gap >= end - start, "fusion.witness_gap")
    for key in ("max_linear_mps", "max_angular_radps", "max_displacement_m"):
        require(finite(witness.get(key)) and 0 <= witness[key] <= rules[key], "fusion.motion_detected")
    # Only publish the validated facts, keeping large raw histories outside tool evidence.
    fields = ("actor_ref", "source_id", "frame_id", "clock_id", "sample_count",
              "max_linear_mps", "max_angular_radps", "max_displacement_m")
    result = {**{k: witness[k] for k in fields}, "body_frame": profile["body_frame"], "stationary": True,
              "first_ns": str(start), "last_ns": str(end), "max_gap_ns": str(gap)}
    for field in ("epoch", "localization_epoch"):
        if field in witness:
            require(identifier(witness[field]), "fusion.witness_binding_mismatch")
            result[field] = witness[field]
    if "topic" in witness:
        require(isinstance(witness["topic"], str) and TOPIC.fullmatch(witness["topic"]),
                "fusion.witness_binding_mismatch")
        result["topic"] = witness["topic"]
    if "limits" in witness:
        limits = witness["limits"]
        require(isinstance(limits, dict) and set(limits) == {
                "linear_mps", "angular_radps", "displacement_m", "rotation_rad"} and
                all(finite(value) and value > 0 for value in limits.values()), "fusion.invalid_witness_limits")
        result["limits"] = limits
    if "max_rotation_rad" in witness:
        require(finite(witness["max_rotation_rad"]) and 0 <= witness["max_rotation_rad"] <= .1, "fusion.motion_detected")
        result["max_rotation_rad"] = witness["max_rotation_rad"]
    return result


def livox_points(row):
    points = row.get("points")
    require(row.get("point_fields") == ["x_m", "y_m", "z_m", "reflectivity"] and isinstance(points, list)
            and type(row.get("sampled_point_count")) is int and len(points) == row["sampled_point_count"]
            and 0 < len(points) <= 1200, "fusion.invalid_points")
    require(all(vector(p, 4) and all(abs(x) <= 1000 for x in p[:3]) and any(p[:3])
                and type(p[3]) is int and 0 <= p[3] <= 255 for p in points), "fusion.invalid_points")
    return (p[:3] for p in points)


def depth_raster(png):
    """Decode the bounded 16-bit grayscale, row-filter-zero PNG produced by depth_png."""
    require(png[:8] == b"\x89PNG\r\n\x1a\n", "fusion.invalid_depth_png")
    offset = 8
    chunks = []
    for expected in (b"IHDR", b"IDAT", b"IEND"):
        require(offset + 12 <= len(png), "fusion.invalid_depth_png")
        size, kind = struct.unpack_from(">I4s", png, offset)
        end = offset + 8 + size
        require(kind == expected and end + 4 <= len(png)
                and zlib.crc32(png[offset + 4:end]) == struct.unpack_from(">I", png, end)[0],
                "fusion.invalid_depth_png")
        chunks.append(png[offset + 8:end])
        offset = end + 4
    header, compressed, tail = chunks
    require(offset == len(png) and not tail and len(header) == 13, "fusion.invalid_depth_png")
    width, height, bits, color, fmt, filt, interlace = struct.unpack(">IIBBBBB", header)
    require(0 < width <= 4096 and 0 < height <= 4096 and width * height <= 2097152
            and (bits, color, fmt, filt, interlace) == (16, 0, 0, 0, 0), "fusion.depth_layout_unsupported")
    stride = 2 * width + 1
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(compressed, height * stride + 1)
    except zlib.error as err:
        raise SpatialFactError("fusion.invalid_depth_png") from err
    require(len(raw) == height * stride and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
            and all(b == 0 for b in raw[::stride]), "fusion.invalid_depth_png")
    return raw, width, height, stride


def depth_points(png, cal, cfg):
    scale = cfg.get("depth_scale_m")
    require(finite(scale) and 0 < scale <= .1 and identifier(cfg.get("depth_scale_ref")), "fusion.unverified_depth_scale")
    raw, w, h, stride = depth_raster(png)
    k, d, roi = cal.get("k"), cal.get("d"), cal.get("roi")
    require(cal.get("type") == "sensor_msgs/msg/CameraInfo" and cal.get("frame_id") == cfg["frame_id"]
            and (cal.get("width"), cal.get("height")) == (w, h) and vector(k, 9) and k[0] > 0 and k[4] > 0
            and all(k[i] == 0 for i in (1, 3, 6, 7)) and k[8] == 1 and isinstance(d, list)
            and len(d) <= 32 and all(finite(x) and x == 0 for x in d)
            and cal.get("distortion_model") in ("plumb_bob", "none")
            and cal.get("binning_x") in (0, 1) and cal.get("binning_y") in (0, 1)
            and isinstance(roi, dict) and roi.get("x_offset") == roi.get("y_offset") == 0
            and (roi.get("width"), roi.get("height")) in ((0, 0), (w, h)),
            "fusion.depth_intrinsics_unsupported")
    step = max(1, math.ceil(math.sqrt(w * h / 1200)))
    for y in range(min(h - 1, step // 2), h, step):
        for x in range(min(w - 1, step // 2), w, step):
            z = struct.unpack_from(">H", raw, y * stride + 1 + 2 * x)[0] * scale
            if z > 0:
                yield ((x - k[2]) * z / k[0], (y - k[5]) * z / k[4], z)


def fuse_stationary(batch, artifacts, profile, witness, *, now_ns):
    """Project selected immutable samples into the body frame under a bracketed stationary witness."""
    now = stamp(now_ns)
    rules = policy(profile)
    inputs = source_data(batch, artifacts, profile)
    first, last = min(x[2] for x in inputs.values()), max(x[3] for x in inputs.values())
    earliest_end = min(x[3] for x in inputs.values())
    batch_stamp = stamp(batch["as_of_ns"])
    require(first <= earliest_end <= last <= batch_stamp <= now and now - first <= rules["max_age_ns"],
            "fusion.sample_stale")
    require(last - earliest_end <= rules["max_skew_ns"], "fusion.time_skew")
    stationary = validate_witness(witness, profile, rules, first, last, now)
    cells, voxels, provenance = {}, {}, {}
    nearest = dict.fromkeys(("front", "left", "rear", "right"))
    res, radius = rules["resolution_m"], rules["radius_m"]
    half = math.ceil(radius / res)
    for bit, (sid, (sample, cfg, start, end, data)) in enumerate(inputs.items()):
        t, q = cfg.get("translation_m"), cfg.get("quaternion_xyzw")
        basis = cfg.get("extrinsic_basis", "site_configured")
        require(vector(t, 3) and all(abs(n) <= 10 for n in t) and vector(q, 4) and identifier(basis)
                and identifier(cfg.get("extrinsic_ref")) and abs(sum(n*n for n in q) - 1) <= .01,
                "fusion.invalid_extrinsic")
        transform = rotation(t, q)
        source = {"frame_id": sample["frame_id"], "kind": cfg["kind"], "artifact_ref": sample["artifact_ref"],
                  "calibration_ref": sample["calibration_ref"], "extrinsic_ref": cfg["extrinsic_ref"],
                  "extrinsic_basis": basis, "translation_m": t, "quaternion_xyzw": q,
                  "clock_id": sample["clock_id"], "first_ns": str(start), "last_ns": str(end)}
        if cfg["kind"] == "depth16":
            cal = json_object(content(sample["calibration_ref"], artifacts, 8192))
            points = depth_points(data, cal, cfg)
            source.update(stamp_basis="ros_header", depth_scale_m=cfg.get("depth_scale_m"),
                          depth_scale_ref=cfg.get("depth_scale_ref"))
        else:
            points = livox_points(data)
            for key in ("stamp_basis", "device_time_type", "first_device_stamp_ns", "last_device_stamp_ns"):
                source[key] = data[key]
        total = retained = 0
        for p in points:
            total += 1
            x, y, z = (r[0]*p[0] + r[1]*p[1] + r[2]*p[2] + r[3] for r in transform)
            distance = math.hypot(x, y)
            if not (rules["min_range_m"] <= distance < radius and rules["z_min_m"] <= z < rules["z_max_m"]):
                continue
            ix, iy, iz = math.floor(x/res) + half, math.floor(y/res) + half, math.floor((z-rules["z_min_m"])/res)
            counter = cells.setdefault((ix, iy), [0, 0])
            counter[0] += 1
            counter[1] |= 1 << bit
            voxel = (ix, iy, iz)
            voxels[voxel] = voxels.get(voxel, 0) | (1 << bit)
            sector = ("front" if x >= 0 else "rear") if abs(x) >= abs(y) else ("left" if y >= 0 else "right")
            if nearest[sector] is None or distance < nearest[sector]:
                nearest[sector] = distance
            retained += 1
        require(total > 0, "fusion.source_has_no_returns")
        source.update(input_points=total, retained_points=retained)
        provenance[sid] = source
    return {"schema_version": 1, "type": "perception.stationary_fusion", "actor_ref": profile["actor_ref"],
            "body_frame": profile["body_frame"], "clock_id": "unix", "first_ns": str(first), "last_ns": str(last),
            "stamp_ns": str(last), "as_of_ns": str(now), "source_ids": list(provenance), "sources": provenance,
            "limits": rules, "witness": stationary,
            "grid": {"resolution_m": res, "origin_xy_m": [-half*res, -half*res], "width": 2*half, "height": 2*half,
                     "z_min_m": rules["z_min_m"], "z_max_m": rules["z_max_m"], "unknown": "unobserved",
                     "cells": [[x, y, n, mask] for (x, y), (n, mask) in sorted(cells.items())]},
            "occupied_cells": len(cells), "overlap_cells": sum(mask.bit_count() > 1 for _, mask in cells.values()),
            "occupied_voxels": len(voxels), "overlap_voxels": sum(mask.bit_count() > 1 for mask in voxels.values()),
            "sector_nearest_m": nearest}
