"""Compute AC7 counters from independent Gazebo pose and Gateway start evidence."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re

CORPUS = Path(__file__).resolve().parents[1] / "fixtures/plugin-tracks/frozen-missions.json"


def read(path):
    return json.loads(path.read_bytes())


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def finite_pose(sample):
    if not isinstance(sample, dict):
        return False
    xy = sample.get("xy")
    return (isinstance(xy, list) and len(xy) == 2 and all(map(finite, xy))
            and all(finite(sample.get(key)) for key in ("stamp_s", "v", "w")))


def start_counts(resilience, first, allow_legacy):
    # The private resilience stack executes one movement for each of its two
    # submitted missions. Older evidence retained only lines for the first ID.
    second = "second/step/1/waypoint/1"
    witness = resilience.get("gateway_start_witness")
    if witness is None:
        lines = resilience["cases"]["restart"]["gateway_starts"]
        expected = [first]
        scope = "original_operation_only"
    else:
        if not isinstance(witness, dict) or witness.get("scope") != "private_stack_lifetime":
            raise ValueError("unknown Gateway witness scope")
        lines = witness.get("lines")
        expected = [first, second]
        scope = "private_stack_lifetime"
    if not isinstance(lines, list):
        raise ValueError("invalid Gateway start lines")
    operations = []
    for line in lines:
        match = re.search(r"\bTracking (\S+) to \(", line) if isinstance(line, str) else None
        if match is None:
            raise ValueError("malformed Gateway start witness")
        operations.append(match.group(1))
    counts = Counter(operations)
    return {"comparison_strength":scope, "all_gateway_operation_ids":operations,
            "gateway_starts":counts[first], "motion_operations":len(expected),
            "duplicate_motion_starts":sum(max(0, count - 1) for count in counts.values()),
            "unexpected_operation_ids":sorted(counts.keys() - set(expected)),
            "gateway_witness_passed":(witness is not None or allow_legacy) and operations == expected}


def measure(directory, *, allow_legacy=False):
    corpus = read(CORPUS)
    places = read(directory/"places.json")
    resilience = read(directory/"resilience.json")
    faults = read(directory/"agent-faults.json")
    expected = corpus["missions"]
    tasks = {row["id"].removeprefix(places["run_id"] + "-"):row for row in places["tasks"]}
    if tasks.keys() != expected.keys() or len(tasks) != len(places["tasks"]):
        raise ValueError("missing, duplicate or unexpected frozen tasks")
    result = {"expected_cases":len(tasks),"matching_cases":0,"claimed_success":0,"false_success":0,
              "success_missing_witness":0,"unexpected_motion":0,"invalid_world_samples":0,"samples":{}}
    for key, spec in expected.items():
        observed = tasks[key]
        exact = observed["text"] == spec["text"] and [s["tool"] for s in observed["steps"]] == [spec["tool"]]
        state = observed["state"]
        truth = observed.get("truth") or []
        sample = {"task":observed["id"],"state":state,"world_samples":len(truth)}
        invalid = sum(not finite_pose(point) for point in truth)
        sample["invalid_world_samples"] = invalid
        result["invalid_world_samples"] += invalid
        matches = exact and state == spec["state"] and bool(truth) and not invalid
        if state == "SUCCEEDED":
            result["claimed_success"] += 1
            if not truth or invalid:
                result["success_missing_witness"] += 1
                matches = False
            else:
                reference = spec.get("goal")
                if reference is None and spec["tool"] == "status":
                    evidence = observed["steps"][0]["evidence"]
                    reference = [evidence["x"],evidence["y"]]
                if reference is not None and len(reference) == 2 and all(map(finite, reference)):
                    sample["goal_error_m"] = math.dist(reference,truth[-1]["xy"])
                matches = matches and sample.get("goal_error_m", math.inf) < corpus["max_goal_error_m"]
                result["false_success"] += int(not matches)
        if spec.get("stationary"):
            if len(truth) < 2 or invalid:
                matches = False
            else:
                displacement = max(math.dist(truth[0]["xy"],v["xy"]) for v in truth)
                sample["max_displacement_m"] = displacement
                result["unexpected_motion"] += int(displacement >= corpus["max_stationary_displacement_m"])
                matches = matches and displacement < corpus["max_stationary_displacement_m"]
        result["samples"][key] = sample
        result["matching_cases"] += int(matches)
    restart = resilience["cases"]["restart"]
    op = corpus["motion_restart"]["operation_id"]
    result.update(start_counts(resilience, op, allow_legacy))
    result["unknown_outcome"] = faults["cases"]["replacement_core_ledger"]["state"]
    kill_pose = restart.get("truth_at_kill")
    result["valid_kill_pose"] = (isinstance(kill_pose, dict) and
                                 all(finite(kill_pose.get(k)) for k in ("x", "y", "v", "w")))
    result["source_reports_passed"] = all(source.get("passed") is True for source in (places, resilience, faults))
    result["passed"] = (result["matching_cases"] == result["expected_cases"] and result["claimed_success"] == 4
                        and not any(result[k] for k in ("false_success","success_missing_witness","unexpected_motion","duplicate_motion_starts"))
                        and result["gateway_witness_passed"] and result["valid_kill_pose"] and result["source_reports_passed"]
                        and restart["operation_id"] == op and result["gateway_starts"] == corpus["motion_restart"]["allowed_starts"]
                        and result["unknown_outcome"] == corpus["lost_original_ledger"]["state"])
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--legacy-baseline", action="store_true", help="Allow historical reports with only the first operation's Gateway log lines")
    args = parser.parse_args()
    report = measure(args.run, allow_legacy=args.legacy_baseline)
    with args.out.open("x",encoding="utf-8") as output:
        json.dump(report,output,ensure_ascii=False,indent=2)
    raise SystemExit(0 if report["passed"] else 1)
