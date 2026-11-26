"""Bounded image-space feedback shared by target-aware motion providers."""
import json
import math
import re


def observation(payload, frame, target):
    """Decode a source-stamped normalized image box, including explicit loss."""
    row = json.loads(payload)
    fields = {"version", "frame", "target", "stamp_ns", "provider", "box"}
    if (not isinstance(row, dict) or set(row) != fields or row["version"] != 1 or row["frame"] != frame
            or row.get("target") != target or type(row.get("stamp_ns")) is not int
            or row["stamp_ns"] <= 0 or not re.fullmatch("[a-f0-9]{64}", str(row.get("provider")))):
        raise ValueError("invalid_target_tracking_source")
    box = row.get("box")
    if box is not None and (not isinstance(box, list) or len(box) != 4
            or not all(type(x) in (float, int) and math.isfinite(x) for x in box)
            or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)):
        raise ValueError("invalid_target_tracking_box")
    return row


def ready(row, reception_age_s, odom_stamp_ns, expected_stamp_ns):
    return (row is not None and row["box"] is not None and 0 <= reception_age_s < .6
            and row["stamp_ns"] >= expected_stamp_ns
            and -100_000_000 <= odom_stamp_ns - row["stamp_ns"] <= 600_000_000)


def tracked_command(v, w, stage, box, yaw_limit):
    """Nudge a driving path away from a side edge without overriding the final heading."""
    left, top, right, bottom = box
    margin = min(left, top, 1-right, 1-bottom)
    if margin < .018:
        return 0., 0.
    if stage != "drive":
        return v, w
    # Maintain the planned camera angle while there is room. Near an edge, turn
    # toward the target and slow down so the bounded steering can take effect.
    correction = max(0., .095-left) - max(0., right-.905)
    w = max(-yaw_limit, min(yaw_limit, w + max(-.025, min(.025, correction*.45))))
    return v * min(1., margin/.062), w
