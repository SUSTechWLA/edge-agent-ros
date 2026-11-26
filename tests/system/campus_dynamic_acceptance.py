"""Audit campus runs against frozen task identity, world poses and Gateway receipts."""
import argparse
import hashlib
import json
from pathlib import Path

from campus_trajectory_truth import measure
from campus_dynamic_witness import still, write
from campus_truth_geometry import FOOTPRINT, clearance, rectangle


def read(path):
    return json.loads(path.read_bytes())


def score(root, trial, runs=None):
    selected = runs or {"crossing": trial, "blocked": trial}
    cases = {}
    for case, attempt in selected.items():
        folder = root / f"{case}-{attempt}"
        world = (read(folder / "world.json") if (folder / "world.json").exists()
                 else read(root / "worlds.json")["scenarios"][case])
        result = read(folder / "replayed.json")
        before, after = (read(folder / f"gateway-{stage}.json") for stage in ("before", "after"))
        checks = result["checks"]
        checks["same_running_gateway_from_submission_through_terminal"] = (
            before == after and before["running"] and "trajectory_tracker:=regulated_pursuit" in before["gateway_command"])
        checks["correct_campus_and_semantic_task"] = (
            world["layout_id"] == "campus_large" and
            result["task_id"] == f"campus-dynamic-{case}-{attempt}" and
            read(folder / "terminal.json")["mission"]["target"] == "教学楼门外北侧等候点")
        digest = hashlib.sha256()
        close = []
        track = []
        with (folder / "timeline.jsonl").open("rb") as stream:
            for line in stream:
                digest.update(line)
                frame = json.loads(line)
                track.append(frame)
                if frame["type"] == "sample" and min(frame["gaps_m"].values()) < .55:
                    close.append(frame)
        checks["stationary_if_actor_enters_clearance_margin"] = all(still(frame["rover"]) for frame in close)
        trajectory = measure(track)
        if case in ("doorway", "crossing"):
            checks["measured_distributed_turn"] = trajectory["distributed_moving_turn"]
        if runs:
            checks["matched_continuous_core_gateway"] = before.get("core_image") == before["image_id"]
            walls = [rectangle(*f["xy"], 0., *(f["size"][0] / 2,) * 2, *(f["size"][1] / 2,) * 2)
                     for f in world["portal"]]
            gaps = [min(clearance(rectangle(*r["rover"]["xy"], r["rover"]["yaw"], *FOOTPRINT), wall)
                        for wall in walls) for r in track if r["type"] == "sample"] if walls else []
            result["minimum_door_frame_clearance_m"] = min(gaps, default=None)
            checks["door_frame_meets_existing_margin"] = len(walls) == 2 and bool(gaps) and min(gaps) >= .55
        result["encounters_within_055m"] = {
            "samples": len(close),
            "highest_measured_speed_mps": max((frame["rover"]["linear_mps"] for frame in close), default=None),
            "highest_measured_turn_rate_radps": max((abs(frame["rover"]["angular_radps"]) for frame in close),
                                                     default=None),
        }
        result.update(passed=all(checks.values()), timeline_sha256=digest.hexdigest(), gateway=before,
                      campus=world, trajectory=trajectory)
        cases[case] = result
    first = next(iter(cases.values()))
    common = {"same_map": all(c["map_ref"] == first["map_ref"] for c in cases.values()),
              "same_controller_image": all(c["gateway"]["image_id"] == first["gateway"]["image_id"] for c in cases.values()),
              "same_base_campus": all(c["campus"]["base_sha256"] == first["campus"]["base_sha256"] for c in cases.values())}
    return dict(trial=trial, source="Gazebo ground truth, actuator output, ROS Gateway events and frozen mission receipts",
                passed=all(common.values()) and all(r["passed"] for r in cases.values()),
                required_clearance_m=.55, checks=common, cases=cases)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--trial", help="Previous two-case recovery suite (same attempt for both)")
    selectors.add_argument("--runs", nargs=3, metavar=("DOORWAY", "CROSSING", "BLOCKED"),
                           help="Attempt number for each site of the continuous-driving suite")
    args = parser.parse_args()
    attempts = dict(zip(("doorway","crossing","blocked"), args.runs)) if args.runs else None
    report = score(args.samples, args.trial, attempts)
    write(args.output, report)
    print(json.dumps({"trial": args.trial, "passed": report["passed"]}))
    raise SystemExit(int(not report["passed"]))
