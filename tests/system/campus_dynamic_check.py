"""Monitor campus traffic fixtures using independent Gazebo poses and contact samples."""
import argparse
import json
from pathlib import Path

from campus_dynamic_fixture import CASES, DEPLOYMENT
from campus_dynamic_witness import record, summarize, write


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("crossing", "blocked", "doorway"), required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=480)
    parser.add_argument("--rescore", action="store_true", help="Re-evaluate saved timeline and terminal.json")
    opts = parser.parse_args()
    if opts.rescore:
        armed = json.loads((opts.output / "armed.json").read_bytes())
        if armed["case"] != opts.case or armed["task_ids"] != [opts.task_id]:
            raise ValueError("archived witness belongs to another case or task")
        events = [json.loads(line) for line in (opts.output / "timeline.jsonl").read_bytes().splitlines()]
        mission = json.loads((opts.output / "terminal.json").read_bytes())["mission"]
        result = summarize(opts.case, opts.task_id, events, mission, cases=CASES,
                           map_id=armed["map_id"], map_digest=armed["map_digest"])
        write(opts.output / "replayed.json", result)
        raise SystemExit(int(not result["passed"]))
    from campus_dynamic_map import publication
    imported = publication()
    ref = imported["map_ref"]
    target = next(place for place in imported["semantic"] if place["entity_id"] == "teaching-exit-north-wait")
    gx, gy, _ = target["entrance_pose"]
    raise SystemExit(record(opts.case, opts.task_id, opts.output, opts.timeout, cases=CASES,
                            deployment=DEPLOYMENT, prefix="campus-dynamic-",
                            map_id=ref["id"], map_digest=ref["digest"], lane_slope=gy/gx, save_scans=True))
