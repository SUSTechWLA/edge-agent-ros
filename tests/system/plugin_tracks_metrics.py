"""Compute AC7 counters from independent Gazebo pose and Gateway start evidence."""
import argparse
import json
import math
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / "fixtures/plugin-tracks/frozen-missions.json"


def read(path):
    return json.loads(path.read_bytes())


def measure(directory):
    corpus = read(CORPUS)
    places = read(directory/"places.json")
    expected = corpus["missions"]
    tasks = {row["id"].removeprefix(places["run_id"] + "-"):row for row in places["tasks"]}
    if tasks.keys() != expected.keys() or len(tasks) != len(places["tasks"]):
        raise ValueError("missing, duplicate or unexpected frozen tasks")
    result = {"expected_cases":len(tasks),"matching_cases":0,"claimed_success":0,"false_success":0,
              "success_missing_witness":0,"unexpected_motion":0,"samples":{}}
    for key, spec in expected.items():
        observed = tasks[key]
        exact = observed["text"] == spec["text"] and [s["tool"] for s in observed["steps"]] == [spec["tool"]]
        state = observed["state"]
        truth = observed.get("truth") or []
        sample = {"task":observed["id"],"state":state,"world_samples":len(truth)}
        matches = exact and state == spec["state"]
        if state == "SUCCEEDED":
            result["claimed_success"] += 1
            if not truth:
                result["success_missing_witness"] += 1
                matches = False
            else:
                reference = spec.get("goal")
                if reference is None and spec["tool"] == "status":
                    evidence = observed["steps"][0]["evidence"]
                    reference = [evidence["x"],evidence["y"]]
                if reference is not None:
                    sample["goal_error_m"] = math.dist(reference,truth[-1]["xy"])
                matches = matches and sample.get("goal_error_m", math.inf) < corpus["max_goal_error_m"]
                result["false_success"] += int(not matches)
        if spec.get("stationary"):
            if len(truth) < 2:
                matches = False
            else:
                displacement = max(math.dist(truth[0]["xy"],v["xy"]) for v in truth)
                sample["max_displacement_m"] = displacement
                result["unexpected_motion"] += int(displacement >= corpus["max_stationary_displacement_m"])
        result["samples"][key] = sample
        result["matching_cases"] += int(matches)
    restart = read(directory/"resilience.json")["cases"]["restart"]
    op = corpus["motion_restart"]["operation_id"]
    starts = [line for line in restart["gateway_starts"] if "Tracking " + op + " to " in line]
    result.update(motion_operations=1, gateway_starts=len(starts),
                  duplicate_motion_starts=max(0,len(starts)-corpus["motion_restart"]["allowed_starts"]),
                  unknown_outcome=read(directory/"agent-faults.json")["cases"]["replacement_core_ledger"]["state"])
    result["passed"] = (result["matching_cases"] == result["expected_cases"] and result["claimed_success"] == 4
                        and not any(result[k] for k in ("false_success","success_missing_witness","unexpected_motion","duplicate_motion_starts"))
                        and restart["operation_id"] == op and len(starts) == 1 and bool(restart["truth_at_kill"])
                        and result["unknown_outcome"] == corpus["lost_original_ledger"]["state"])
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args = parser.parse_args()
    report = measure(args.run)
    with args.out.open("x",encoding="utf-8") as output:
        json.dump(report,output,ensure_ascii=False,indent=2)
    raise SystemExit(0 if report["passed"] else 1)
