"""Local appearance grounding on hash-bound, co-registered RGB-D observations."""
import math
import re
import struct
import zlib
from statistics import median

from edge_contracts.spatial_facts import SpatialFactError, identifier, require
from perception.spatial_facts.stationary_fusion import (
    batch_samples, content, depth_raster, finite, json_object, stamp, vector,
)


def target_descriptor(query):
    """The appearance provider accepts only its declared positive descriptions."""
    require(isinstance(query, str) and 0 < len(query) <= 256, "grounding.unsupported_target")
    match = re.fullmatch(
        r"(?:请)?(?:识别|寻找|定位|查找)?(?:画面|图像)?(?:中)?(?:的)?"
        r"(左侧|右侧|左边|右边|中央|中间)(?:的)?(?:大块|大面积|大片)(?:的)?"
        r"(?:浅灰色|浅灰)(?:的)?平面(?:[，,](?:给出|返回)(?:其|它的)?二维边框和三维位置)?[。！!]?",
        query.strip())
    require(match is not None, "grounding.unsupported_target")
    return {"左侧": "left", "左边": "left", "右侧": "right", "右边": "right",
            "中间": "center", "中央": "center"}[match[1]]


def rgb_raster(png):
    """Read the bounded RGB8 PNG layout of edge_contracts.image_frames.raw_frame."""
    code = "grounding.invalid_rgb_png"
    require(png.startswith(b"\x89PNG\r\n\x1a\n"), code)
    chunks = []
    offset = 8
    for name in (b"IHDR", b"IDAT", b"IEND"):
        require(offset + 12 <= len(png), code)
        n, tag = struct.unpack_from(">I4s", png, offset)
        end = offset + 8 + n
        require(tag == name and end + 4 <= len(png) and
                zlib.crc32(png[offset + 4:end]) == struct.unpack_from(">I", png, end)[0], code)
        chunks.append(png[offset + 8:end])
        offset = end + 4
    header, encoded, tail = chunks
    require(offset == len(png) and len(header) == 13 and not tail, code)
    w, h, bits, color, comp, filt, interlace = struct.unpack(">IIBBBBB", header)
    require(8 <= w <= 640 and 8 <= h <= 480 and (bits, color, comp, filt, interlace) == (8, 2, 0, 0, 0), code)
    row = 1 + 3 * w
    decoder = zlib.decompressobj()
    try:
        pixels = decoder.decompress(encoded, h * row + 1)
    except zlib.error as err:
        raise SpatialFactError(code) from err
    require(len(pixels) == h * row and decoder.eof and not decoder.unconsumed_tail
            and not decoder.unused_data and not any(pixels[::row]), code)
    return pixels, w, h, row


def calibrated_inputs(batch, artifacts, profile, now_ns):
    require(isinstance(profile, dict) and identifier(profile.get("actor_ref")) and
            isinstance(profile.get("grounding"), dict), "grounding.invalid_profile")
    g = profile["grounding"]
    keys = [g.get(k) for k in ("rgb_source", "depth_source", "calibration_source")]
    require(all(identifier(k) for k in keys) and len(set(keys)) == 3, "grounding.invalid_profile")
    sources = batch_samples(batch, profile)
    require(all(k in sources for k in keys), "grounding.missing_input")
    rgb, depth, info = [sources[k] for k in keys]
    cfg = profile.get("sources", {}).get(keys[1], g.get("depth_profile", {}))
    require(isinstance(cfg, dict) and cfg.get("kind") == "depth16" and identifier(cfg.get("frame_id"))
            and all(s["frame_id"] == cfg["frame_id"] and s["clock_id"] == "unix" for s in (rgb, depth, info))
            and rgb["media_type"] == depth["media_type"] == "image/png" and info["media_type"] == "application/json"
            and rgb["calibration_ref"] == depth["calibration_ref"] == info["artifact_ref"],
            "grounding.registration_mismatch")
    skew_limit = g.get("max_skew_ns", 40_000_000)
    age_limit = g.get("max_age_ns", 2_000_000_000)
    require(type(skew_limit) is int and 0 <= skew_limit <= 50_000_000 and type(age_limit) is int
            and 0 < age_limit <= 5_000_000_000, "grounding.invalid_profile")
    now = stamp(now_ns)
    times = [s["stamp_ns"] for s in (rgb, depth, info)]
    require(max(times) <= now and now - min(times) <= age_limit, "grounding.stale_input")
    # CameraInfo describes calibration. Its content hash is pinned by both dynamic images.
    require(abs(times[0] - times[1]) <= skew_limit, "grounding.unsynchronized_input")
    scale = cfg.get("depth_scale_m")
    require(finite(scale) and 0 < scale <= .1 and identifier(cfg.get("depth_scale_ref")),
            "grounding.unverified_depth_scale")
    image = rgb_raster(content(rgb["artifact_ref"], artifacts, 1024 * 1024))
    depths = depth_raster(content(depth["artifact_ref"], artifacts, 4 * 1024 * 1024))
    cal = json_object(content(info["artifact_ref"], artifacts, 64 * 1024))
    w, h = image[1:3]
    k, p, d, roi = (cal.get(a) for a in ("k", "p", "d", "roi"))
    require((w, h) == depths[1:3] == (cal.get("width"), cal.get("height"))
            and cal.get("type") == "sensor_msgs/msg/CameraInfo" and cal.get("frame_id") == cfg["frame_id"]
            and vector(k, 9) and k[0] > 0 and k[4] > 0 and k[8] == 1 and all(k[i] == 0 for i in (1, 3, 6, 7))
            and isinstance(d, list) and len(d) <= 32 and all(finite(z) and z == 0 for z in d)
            and cal.get("distortion_model") in ("none", "plumb_bob") and vector(p, 12)
            and p == [k[0], 0, k[2], 0, 0, k[4], k[5], 0, 0, 0, 1, 0]
            and cal.get("r") == [1, 0, 0, 0, 1, 0, 0, 0, 1] and cal.get("binning_x") in (0, 1)
            and cal.get("binning_y") in (0, 1) and isinstance(roi, dict)
            and roi.get("x_offset") == roi.get("y_offset") == 0 and roi.get("do_rectify") is False
            and (roi.get("width"), roi.get("height")) in ((0, 0), (w, h)), "grounding.intrinsics_unsupported")
    binding = {"actor_ref": profile["actor_ref"], "frame_id": cfg["frame_id"], "clock_id": "unix",
               "width": w, "height": h, "depth_scale_m": scale, "depth_scale_ref": cfg["depth_scale_ref"],
               "rgb_depth_skew_ns": abs(times[0] - times[1]), "max_pair_skew_ns": skew_limit}
    for name, sample in zip(("rgb", "depth", "calibration"), (rgb, depth, info)):
        binding[name + "_source_id"] = sample["source_id"]
        binding[name + "_ref"] = sample["artifact_ref"]
        binding[name + "_stamp_ns"] = str(sample["stamp_ns"])
    return image, depths, k, binding


def component_mask(image, side):
    raw, w, h, row = image
    stride = max(1, math.ceil(w / 160), math.ceil(h / 120))
    gw, gh = math.ceil(w / stride), math.ceil(h / stride)
    pending = set()
    for j in range(gh):
        base = min(j * stride + stride // 2, h - 1) * row + 1
        for i in range(gw):
            off = base + min(i * stride + stride // 2, w - 1) * 3
            r, g, b = raw[off:off + 3]
            if 115 <= (r + g + b) / 3 <= 224 and max(r, g, b) - min(r, g, b) <= 30:
                pending.add((i, j))
    candidates = []
    while pending:
        queue = [pending.pop()]
        region = []
        while queue:
            i, j = queue.pop()
            region.append((i, j))
            for neighbor in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
                if neighbor in pending:
                    pending.remove(neighbor)
                    queue.append(neighbor)
        if len(region) < .12 * gw * gh:
            continue
        x = sum(i + .5 for i, _ in region) / (len(region) * gw)
        if (side == "right" and x >= .56 or side == "left" and x <= .44 or side == "center" and .44 < x < .56):
            candidates.append(region)
    require(candidates, "grounding.target_absent")
    candidates.sort(key=len, reverse=True)
    require(len(candidates) == 1 or len(candidates[1]) < len(candidates[0]) * .60, "grounding.target_ambiguous")
    return sorted(candidates[0], key=lambda p: (p[1], p[0])), stride, gw, gh


def plane_model(rows):
    # A plane not containing the optical center has inverse Z affine in normalized pixel coordinates.
    count = len(rows)
    mx = sum(r[2] for r in rows) / count
    my = sum(r[3] for r in rows) / count
    mz = sum(1 / r[4] for r in rows) / count
    xx = xy = yy = xz = yz = 0.
    for _, _, x, y, z in rows:
        x, y, iz = x - mx, y - my, 1 / z - mz
        xx += x*x
        yy += y*y
        xy += x*y
        xz += x*iz
        yz += y*iz
    det = xx * yy - xy * xy
    require(det > 1e-12, "grounding.plane_indeterminate")
    a, b = (xz * yy - yz * xy) / det, (yz * xx - xz * xy) / det
    return a, b, mz - a * mx - b * my


def residual(model, row):
    a, b, c = model
    inverse_z = a * row[2] + b * row[3] + c
    return abs(1 / inverse_z - row[4]) if inverse_z > 0 else float("inf")


def estimate_surface(region, stride, depths, k, scale):
    raw, w, h, row = depths
    measured = []
    for i, j in region:
        x, y = min(i * stride + stride // 2, w - 1), min(j * stride + stride // 2, h - 1)
        z16 = struct.unpack_from(">H", raw, y * row + 1 + x * 2)[0]
        if 0 < z16 < 65535:
            measured.append((x, y, (x - k[2]) / k[0], (y - k[5]) / k[4], z16 * scale))
    valid = len(measured) / len(region)
    require(valid >= .65 and len(measured) >= 30, "grounding.depth_insufficient")
    z = median(r[4] for r in measured)
    mad = median(abs(r[4] - z) for r in measured)
    seed = [r for r in measured if abs(r[4] - z) <= max(.08, .12*z, 3.5*mad)]
    require(len(seed) >= max(30, .65 * len(measured)), "grounding.plane_unverified")
    model = plane_model(seed)
    tolerance = max(.008, 3 * scale, .012 * z)
    for _ in range(2):
        inliers = [r for r in measured if residual(model, r) <= tolerance]
        require(len(inliers) >= max(30, .75 * len(measured)), "grounding.plane_unverified")
        model = plane_model(inliers)
    inliers = [r for r in measured if residual(model, r) <= tolerance]
    require(len(inliers) >= max(30, .75 * len(measured)), "grounding.plane_unverified")
    cx, cy = sum(r[0] for r in inliers) / len(inliers), sum(r[1] for r in inliers) / len(inliers)
    x, y, nx, ny, z = min(inliers, key=lambda r: (r[0] - cx)**2 + (r[1] - cy)**2)
    errors = sorted(residual(model, r) for r in inliers)
    return {"point_pixel": [x, y], "point_camera_optical_m": [nx * z, ny * z, z], "point_depth_raw": round(z / scale),
            "quality": {"sampled_mask_cells": len(region), "valid_depth_fraction": valid,
                        "plane_inlier_fraction": len(inliers) / len(measured), "plane_inlier_count": len(inliers),
                        "residual_threshold_m": tolerance, "median_residual_m": median(errors),
                        "p95_residual_m": errors[math.ceil(.95 * len(errors)) - 1],
                        "depth_quantization_step_m": scale, "absolute_accuracy": "uncharacterized"}}


def ground_target(query, batch, artifacts, profile, *, now_ns):
    side = target_descriptor(query)
    image, depths, k, binding = calibrated_inputs(batch, artifacts, profile, now_ns)
    region, stride, gw, gh = component_mask(image, side)
    surface = estimate_surface(region, stride, depths, k, binding["depth_scale_m"])
    runs = []
    for i, j in region:
        if runs and runs[-1][0] == j and runs[-1][2] == i:
            runs[-1][2] += 1
        else:
            runs.append([j, i, i + 1])
    require(len(runs) <= 2048, "grounding.mask_too_fragmented")
    xs, ys = zip(*region)
    box = [min(xs)*stride, min(ys)*stride, min(image[1], (max(xs) + 1)*stride), min(image[2], (max(ys) + 1)*stride)]
    return {"schema_version": 1, "query": query, "method": "light_gray_component_depth_plane.v1",
            "target_descriptor": {"color": "light_gray", "shape": "planar_patch", "side": side},
            "bbox_xyxy": box, "mask": {"stride": stride, "grid_width": gw, "grid_height": gh, "runs": runs},
            **binding, **surface}
