"""Fit base <- sensor from paired metric fixture observations; emit a candidate only."""
import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np


def rotation_xyzw(rotation):
    # Davenport symmetric matrix for the active rotation; stable at 180 degrees.
    r = rotation
    axis = [r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]]
    k = np.zeros((4, 4))
    k[:3, :3] = r + r.T - np.trace(r) * np.eye(3)
    k[3, :3] = k[:3, 3] = axis
    k[3, 3] = np.trace(r)
    q = np.linalg.eigh(k)[1][:, -1]
    return (q if q[-1] >= 0 else -q).tolist()


def fit(raw, tolerance_m=.025):
    if not math.isfinite(tolerance_m) or not 0 < tolerance_m <= .1:
        raise ValueError("invalid independent error limit in metres")
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("unit") != "m":
        raise ValueError("expected paired 3D points in metres")
    result = {}
    for key in ("robot_id", "serial", "reference", "parent_frame", "child_frame"):
        value = doc.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("missing " + key)
        result[key] = value
    if result["parent_frame"] == result["child_frame"]:
        raise ValueError("parent and child frames must differ")
    labels, used = set(), {"sensor": set(), "base": set()}

    def points(group, minimum):
        rows = doc.get(group)
        if not isinstance(rows, list) or not minimum <= len(rows) <= 4096:
            raise ValueError(f"{group} needs {minimum} to 4096 independently identified points")
        out = {name: [] for name in used}
        for row in rows:
            label = row.get("id") if isinstance(row, dict) else None
            if not isinstance(label, str) or not label.strip() or label in labels:
                raise ValueError("missing or reused target ID")
            labels.add(label)
            for frame in out:
                xyz = row.get(frame)
                if (not isinstance(xyz, list) or len(xyz) != 3 or
                        any(type(v) not in (int, float) or not math.isfinite(v) for v in xyz)):
                    raise ValueError("invalid 3D point for " + label)
                key = tuple(round(v, 6) for v in xyz)
                if key in used[frame]:
                    raise ValueError("reused target location in " + frame)
                used[frame].add(key)
                out[frame].append(xyz)
        for frame, values in out.items():
            a = np.asarray(values, dtype=float)
            centered = a - a.mean(axis=0)
            spans = np.linalg.svd(centered, compute_uv=False) / math.sqrt(len(values))
            if spans[1] < max(.025, spans[0] * .02):
                raise ValueError(f"{group}: inadequate target spread in {frame}")
        return np.asarray(out["sensor"]), np.asarray(out["base"])

    src, dst = points("fit", 4)
    check_src, check_dst = points("validation", 3)
    u, _, vt = np.linalg.svd((src - src.mean(axis=0)).T @ (dst - dst.mean(axis=0)))
    sign = np.diag([1., 1., 1. if np.linalg.det(vt.T @ u.T) >= 0 else -1.])
    rotation = vt.T @ sign @ u.T
    offset = dst.mean(axis=0) - rotation @ src.mean(axis=0)
    for group, a, b in (("fit", src, dst), ("validation", check_src, check_dst)):
        residual = np.linalg.norm(a @ rotation.T + offset - b, axis=1)
        result.update({group + "_rms_m": float(np.sqrt(np.mean(residual**2))),
                       group + "_max_m": float(max(residual)), group + "_count": len(a)})
        if not np.all(residual <= tolerance_m):
            raise ValueError(f"{group}: residual {max(residual):.4f} m exceeds {tolerance_m:.4f} m")
    return dict(result, method="target_fit", input_sha256=hashlib.sha256(raw).hexdigest(),
                translation_m=offset.tolist(), rotation_xyzw=rotation_xyzw(rotation))


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("measurement", type=Path)
    cli.add_argument("--tolerance-m", type=float, default=.025)
    args = cli.parse_args()
    try:
        print(json.dumps(fit(args.measurement.read_bytes(), args.tolerance_m), indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, OverflowError, np.linalg.LinAlgError) as exc:
        cli.exit(2, f"Calibration candidate rejected: {exc}\n")
