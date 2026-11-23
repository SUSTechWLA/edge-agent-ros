"""Compare the truth tracks of two centered-box runs on the same rover and course."""
import argparse
import json
import math
from pathlib import Path


def read(path):
    return json.loads(path.read_bytes())


def audit(directory):
    run = read(directory / "run.json")
    score = read(directory / "witness/score.json")
    mission = read(directory / "mission.json")
    with (directory / "witness/observations.jsonl").open(encoding="utf8") as stream:
        poses = [r for line in stream if (r := json.loads(line))["kind"] == "truth"]
    if not poses or not all(r["frame"] == "world" for r in poses):
        raise ValueError("missing world-frame trajectory")
    if any(a["stamp_ns"] >= b["stamp_ns"] for a, b in zip(poses, poses[1:])):
        raise ValueError("unordered truth trajectory")
    distances = [math.dist(a["xy"], b["xy"]) for a, b in zip(poses, poses[1:])]
    pivot_s = rolling_turn_s = yaw_travel = 0.
    for a, b, distance in zip(poses, poses[1:], distances):
        dt = (b["stamp_ns"]-a["stamp_ns"])*1e-9
        dyaw = abs(math.remainder(b["yaw"]-a["yaw"], math.tau))
        yaw_travel += dyaw
        if dyaw/dt > .18:
            if distance/dt < .03:
                pivot_s += dt
            else:
                rolling_turn_s += dt
    start, goal = run["start"], run["goal"]
    length = math.dist(start, goal)
    dx, dy = (goal[0]-start[0])/length, (goal[1]-start[1])/length
    lateral = [dx*(r["xy"][1]-start[1]) - dy*(r["xy"][0]-start[0]) for r in poses]
    peak = max(lateral, key=abs)
    arrival = math.dist(poses[-1]["xy"], goal)
    if not (math.isclose(sum(distances), score["path_length_m"], abs_tol=1e-6) and
            math.isclose(arrival, score["arrival_error_m"], abs_tol=1e-6)):
        raise ValueError("archived score differs from its raw trajectory")
    return run, {
        "task": mission["task"], "planner": run["planner"], "core_state": mission["state"],
        "truth_acceptance": score["passed"], "arrival_error_m": arrival,
        "travel_m": sum(distances), "signed_peak_lateral_m": peak,
        "lateral_min_m": min(lateral), "lateral_max_m": max(lateral),
        "lateral_span_m": max(lateral)-min(lateral),
        "absolute_yaw_travel_rad": yaw_travel, "pivot_time_s": pivot_s, "rolling_turn_time_s": rolling_turn_s,
        "peak_lateral_m": abs(peak), "surface_clearance_m": score["min_swept_clearance_m"],
        "box_clearances_by_part_m": score["box_clearances_by_part"]["swept_lower_bound_m"],
        "contact_pairs_non_ground": score["unexpected_contact_pairs"],
        "eligible_box_frames": score["visible_box_samples"], "measurement_failures": score["failure_samples"],
        "max_range_error_m": score["max_range_error_m"], "max_bearing_error_deg": score["max_bearing_error_deg"],
        "failed_checks": [name for name, result in score["checks"].items() if not result],
    }


def compare(baseline, candidate):
    old_run, old = audit(baseline)
    new_run, new = audit(candidate)
    same = ("vehicle", "localization", "world", "scenario", "start", "goal", "obstacle", "deployed_world_sha256")
    for key in same:
        if key not in old_run or old_run[key] != new_run.get(key):
            raise ValueError(f"incomparable test conditions: {key}")
    if old["planner"] == new["planner"] or new_run["scenario"] != "centered":
        raise ValueError("expected two different planners on the centered course")
    return {
        "conditions": {k: old_run[k] for k in same},
        "baseline": old, "candidate": new,
        "paired_pass": old["truth_acceptance"] and new["truth_acceptance"],
        "lateral_reduction_m": old["peak_lateral_m"] - new["peak_lateral_m"],
        "lateral_span_reduction_m": old["lateral_span_m"] - new["lateral_span_m"],
        "travel_reduction_m": old["travel_m"] - new["travel_m"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.baseline, args.candidate)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf8")
    if not result["paired_pass"] or result["lateral_reduction_m"] <= 0:
        raise SystemExit("comparison recorded; paired success and a smaller measured lateral excursion required")
