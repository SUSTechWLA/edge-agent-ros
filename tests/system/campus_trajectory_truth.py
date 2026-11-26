"""Measure the shape of the executed trajectory using Gazebo world poses alone."""
import argparse
import gzip
import json
import math
from pathlib import Path


SPACING_M = .06
MOVING_MPS = .04


def equal_distance_points(track):
    """Interpolate along measured XY, keeping constant spacing across sample boundaries."""
    spaced = [track[0]]
    remaining = SPACING_M
    for start, end in zip(track, track[1:]):
        length = math.dist(start, end)
        while length >= remaining:
            t = remaining / length
            start = [a + t * (b - a) for a, b in zip(start, end)]
            spaced.append(start)
            length = math.dist(start, end)
            remaining = SPACING_M
        remaining -= length
    return spaced


def score_turns(track):
    points = equal_distance_points(track)
    headings = [math.atan2(b[1] - a[1], b[0] - a[0]) for a, b in zip(points, points[1:])]
    turns = []
    group = []
    for a, b in zip(headings, headings[1:]):
        delta = math.degrees(math.remainder(b - a, 2 * math.pi))
        if group and (abs(delta) < .15 or delta * group[-1] < 0):
            turns.append(group)
            group = []
        if abs(delta) >= .15:
            group.append(delta)
    if group:
        turns.append(group)
    result = []
    for group in turns:
        angle = abs(sum(group))
        largest = max(map(abs, group))
        result.append({"turn_deg": round(angle, 3), "span_m": round((len(group) + 1) * SPACING_M, 3),
                       "largest_increment_deg": round(largest, 3),
                       "distributed": len(group) >= 3 and angle >= 12 and largest < .65 * angle})
    return result


def measure(rows):
    samples = []
    nearest = None
    nearest_moving = None
    seen_start = False
    for row in rows:
        if row.get("type") == "gateway" and row.get("kind") == "action.started" and not seen_start:
            # The witness arms before browser submission. Keep the last
            # pre-dispatch pose, not the entire stationary setup interval.
            samples = samples[-1:]
            seen_start = True
        elif row.get("type") == "sample":
            samples.append(row["rover"])
            if seen_start and row.get("gaps_m"):
                actor, gap = min(row["gaps_m"].items(), key=lambda entry: entry[1])
                observation = {"gap_m": gap, "actor": actor, "rover_speed_mps": row["rover"]["linear_mps"],
                               "stamp_ns": row["rover"]["stamp_ns"], "command": row.get("command")}
                if nearest is None or gap < nearest["gap_m"]:
                    nearest = observation
                if (row["rover"]["linear_mps"] > MOVING_MPS
                        and (nearest_moving is None or gap < nearest_moving["gap_m"])):
                    nearest_moving = observation
    if len(samples) < 2 or any(s["frame"] != "world" for s in samples):
        raise ValueError("expected multiple Gazebo world poses")
    moving, turns, distance, spin = [], [], 0., 0.
    gaps = 0
    for a, b in zip(samples, samples[1:]):
        dt = (b["stamp_ns"] - a["stamp_ns"]) / 1e9
        if dt <= 0:
            raise ValueError("Gazebo world clock did not advance")
        if dt <= .35:
            distance += math.dist(a["xy"], b["xy"])
            if max(a["linear_mps"], b["linear_mps"]) <= MOVING_MPS:
                spin += abs(math.remainder(b["yaw"] - a["yaw"], 2 * math.pi))
        else:
            gaps += 1
        if dt <= .35 and min(a["linear_mps"], b["linear_mps"]) > MOVING_MPS:
            if not moving:
                moving.append(a["xy"])
            moving.append(b["xy"])
        elif moving:
            turns.extend(score_turns(moving))
            moving = []
    if moving:
        turns.extend(score_turns(moving))
    best = max((t for t in turns if t["distributed"]), key=lambda t: t["turn_deg"], default=None)
    return {"source": "Gazebo world odometry XY", "samples": len(samples), "discontinuities_over_350ms": gaps,
            "distance_m": round(distance, 4), "spacing_m": SPACING_M, "moving_above_mps": MOVING_MPS,
            "start": samples[0], "stop": samples[-1], "low_speed_body_rotation_deg": round(math.degrees(spin), 2),
            "closest_actor": nearest, "closest_actor_while_moving": nearest_moving,
            "largest_continuous_turn": best, "distributed_moving_turn": best is not None,
            "turns_over_5deg": [t for t in turns if t["turn_deg"] >= 5]}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("timeline", type=Path)
    cli.add_argument("output", type=Path)
    args = cli.parse_args()
    open_timeline = gzip.open if args.timeline.suffix == ".gz" else open
    with open_timeline(args.timeline, "rt", encoding="utf-8-sig") as capture:
        result = measure(json.loads(line) for line in capture)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("samples", "distance_m", "largest_continuous_turn")}))
