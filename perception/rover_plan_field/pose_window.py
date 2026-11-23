"""First-write receipts for two measured poses at the boundaries of an Agent task."""
from http.client import HTTPConnection, HTTPException
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from contextlib import closing


def current():
    """Read the sole pose provider; a missing observation is itself recordable."""
    conn = HTTPConnection("facts", 8150, timeout=2)
    try:
        conn.request("GET", "/pose/current")
        res = conn.getresponse()
        raw = res.read(16_385)
        if res.status != 200 or len(raw) > 16_384:
            return {"valid": False, "reason": f"pose_provider_http_{res.status}"}
        pose = json.loads(raw)
        if not isinstance(pose, dict):
            raise ValueError("pose must be an object")
        return {**pose, "boundary_received_ns": str(time.time_ns())}
    except (OSError, HTTPException, ValueError):
        return {"valid": False, "reason": "pose_provider_unavailable"}
    finally:
        conn.close()


def _sample(value):
    sampled = time.time_ns()
    raw = value if isinstance(value, dict) else {}
    try:
        raw = json.loads(json.dumps(raw, allow_nan=False))
        sampled = int(raw.pop("boundary_received_ns", sampled))
        if not raw.get("valid") is True:
            reason = raw.get("reason") or "odometry_unavailable"
        else:
            ttl = raw["max_age_ms"]
            age = sampled - int(raw["stamp_ns"])
            expected = ("frame_id", "child_frame_id", "epoch", "source_topic")
            numbers = (raw["x_m"], raw["y_m"], raw["z_m"], raw["yaw_rad"])
            if (raw["clock_id"] != "unix" or any(not isinstance(raw[k], str) or not raw[k] for k in expected)
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in numbers)
                    or type(ttl) is not int or not 0 < ttl <= 1000 or int(raw["stamp_ns"]) <= 0):
                reason = "odometry_contract_invalid"
            elif not -50_000_000 <= age <= ttl * 1_000_000:
                reason = "odometry_stale_at_boundary"
            else:
                reason = None
        return {"valid": reason is None, "reason": reason, "sampled_at_ns": str(sampled), "pose": raw}
    except (KeyError, ValueError, TypeError, OverflowError):
        return {"valid": False, "reason": "odometry_contract_invalid", "sampled_at_ns": str(sampled), "pose": None}


def _receipt(task_id, row):
    if row is None:
        return None
    before, after = (json.loads(part) if part else None for part in row)
    window = {"schema_version": 1, "task_id": task_id, "observation_scope": "task_boundary_samples",
              "continuous_motion_verified": False, "status": "pending", "start_pose": before, "end_pose": after,
              "delta": None, "reason": None}
    if after is None:
        return window
    reason = None
    if before is None or not before["valid"] or not after["valid"]:
        reason = "boundary_pose_unavailable"
    else:
        a, b = before["pose"], after["pose"]
        if any(a.get(k) != b.get(k) for k in ("source_topic", "clock_id", "ros_domain_id", "epoch", "frame_id", "child_frame_id")):
            reason = "pose_reference_changed"
        elif int(b["stamp_ns"]) <= int(a["stamp_ns"]):
            reason = "source_time_did_not_advance"
        else:
            dx, dy, dz = (b[k] - a[k] for k in ("x_m", "y_m", "z_m"))
            angle = b["yaw_rad"] - a["yaw_rad"]
            window["delta"] = {"frame_id": a["frame_id"], "dx_m": dx, "dy_m": dy, "dz_m": dz,
                               "displacement_m": math.hypot(dx, dy, dz), "planar_displacement_m": math.hypot(dx, dy),
                               "yaw_delta_rad": math.atan2(math.sin(angle), math.cos(angle)),
                               "source_elapsed_s": (int(b["stamp_ns"]) - int(a["stamp_ns"])) / 1e9}
    window.update(status="incomparable" if reason else "completed", reason=reason)
    return window


def _row(task_id, field=None, sample=None):
    if not isinstance(task_id, str) or not 1 <= len(task_id) <= 160 or "\0" in task_id:
        raise ValueError("invalid task ID")
    file = Path(os.environ.get("EDGE_FIELD_AUDIT_DIR", "/audit")) / "task-poses.sqlite"
    if field is None and not file.exists():
        return None
    file.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(file, timeout=8)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS task_poses (task TEXT PRIMARY KEY, before TEXT, after TEXT)")
            if field is not None:
                value = json.dumps(_sample(sample), ensure_ascii=False, allow_nan=False)
                # field is a private constant; task identifiers and observations are bound values.
                db.execute(f"INSERT INTO task_poses (task, {field}) VALUES (?, ?) ON CONFLICT(task) "
                           f"DO UPDATE SET {field}=excluded.{field} WHERE task_poses.{field} IS NULL",
                           (task_id, value))
            return db.execute("SELECT before, after FROM task_poses WHERE task=?", (task_id,)).fetchone()
    finally:
        db.close()


def start(task_id, before_pose):
    return _receipt(task_id, _row(task_id, "before", before_pose))


def finish(task_id, after_pose):
    return _receipt(task_id, _row(task_id, "after", after_pose))


def get(task_id):
    return _receipt(task_id, _row(task_id))


def unfinished():
    file = Path(os.environ.get("EDGE_FIELD_AUDIT_DIR","/audit")) / "task-poses.sqlite"
    if not file.is_file():
        return []
    with closing(sqlite3.connect(file, timeout=8)) as db:
        return [row[0] for row in db.execute("SELECT task FROM task_poses WHERE after IS NULL")]


def recent():
    file = Path(os.environ.get("EDGE_FIELD_AUDIT_DIR","/audit")) / "task-poses.sqlite"
    if not file.is_file():
        return None
    with closing(sqlite3.connect(file,timeout=8)) as db:
        row=db.execute("SELECT task FROM task_poses WHERE before IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()
        return row[0] if row else None
