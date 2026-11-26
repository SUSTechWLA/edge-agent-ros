"""Verify exported Jetson task receipts against recorder events, archived map and CameraInfo bytes."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from edge_contracts.navigation import digest, map_reference, validate_route
from edge_contracts.occupancy import Grid, clearance_mask


def check(ok, what):
    if not ok:
        raise ValueError(what)


def read(root, file):
    return json.loads((root / file).read_bytes())


def recording(root, folder, tool):
    summary = read(root, folder + "/recording.json")
    task = read(root, summary["task_id"] + ".json")
    events = [json.loads(row) for row in (root / folder / "media/mission/timeline.jsonl").read_bytes().splitlines()]
    check(summary["status"] == "FINISHED" and summary["task_status"] == task["state"] == "SUCCEEDED"
          and summary["missing_sources"] == [], "recording was incomplete")
    check(len(task["steps"]) == 1 and task["steps"][0]["tool"] == tool, "unexpected plan")
    op = task["id"] + "/step/1"
    check(any(e.get("operation_id") == op and e.get("code") == "agent.verified" for e in events),
          "missing verification for original operation")
    check(sum(e.get("code") == "agent.tool.response" and e.get("outcome") == "SUCCEEDED"
              and e.get("module") == tool and e.get("operation_id") == op for e in events) == 1,
          "expected one successful tool receipt")
    return task["steps"][0]["evidence"], {"task_id": task["id"], "counts": summary["counts"]}


def verify(root):
    plan, plan_record = recording(root, "recording", "nav_plan")
    batch, sensor_record = recording(root, "sensor-recording", "read_spatial_observations")
    route, pose = plan["route"], plan["localization"]
    site = read(root, "settings.json")["navigation.maps"]["catalog"]
    binding = read(root, "odometry.json")
    actor = site["actors"][binding["actor_ref"]]
    record = site["maps"][actor["map_key"]]
    profile = site["profiles"][actor["profile_ref"]]
    validate_route(route, actor_ref=binding["actor_ref"], map_ref=map_reference(record), frame_id=binding["frame_id"],
                   localization_epoch=binding["localization_epoch"], profile_ref=actor["profile_ref"])
    check(route["profile_digest"] == digest(profile) and route["map_ref"] == binding["map_ref"], "map/profile mismatch")
    check(pose["basis"] == "published" and pose["position_m"] == route["waypoints"][0]
          and pose["stamp_ns"] == route["localization_stamp_ns"] and pose["map_ref"] == route["map_ref"]
          and int(pose["stamp_ns"]) <= int(pose["as_of_ns"]) <= int(pose["valid_until_ns"]), "invalid starting pose")
    check(any(p["position_m"] == route["waypoints"][-1] and p["map_key"] == actor["map_key"]
              for p in site["destinations"][route["destination_ref"]]), "wrong route endpoint")
    grid = Grid(record["geometry"])
    safe, _ = clearance_mask(grid, profile["clearance_m"])
    for start, end in zip(route["waypoints"], route["waypoints"][1:]):
        n = max(1, math.ceil(math.dist(start, end) / (grid.resolution / 4)))
        for j in range(n + 1):
            cell = grid.index([a + (b - a) * j / n for a, b in zip(start, end)])
            check(cell >= 0 and safe[cell], "route intersects an excluded cell")

    samples = {s["source_id"]: s for s in batch["samples"]}
    check(set(samples) == {"wheel.odometry", "d435.color.info"} and batch["basis"] == "published"
          and not batch.get("source_gaps") and batch["actor_ref"] == binding["actor_ref"], "sensor coverage mismatch")
    ages = {k: (int(batch["as_of_ns"]) - int(s["stamp_ns"])) / 1e6 for k, s in samples.items()}
    check(all(0 <= age <= 1000 for age in ages.values()), "sensor observation expired")
    camera = samples["d435.color.info"]
    raw = (root / "camera-info.json").read_bytes()
    check(camera["artifact_ref"] == "sha256:" + hashlib.sha256(raw).hexdigest(), "wrong calibration artifact")
    calibration = json.loads(raw)
    check(calibration["frame_id"] == camera["frame_id"] and calibration["type"] == "sensor_msgs/msg/CameraInfo",
          "wrong camera binding")
    containers = read(root, "final-containers.json")
    check(all(not c["privileged"] and not c["devices"] and "ALL" in c["cap_drop"] for c in containers),
          "unexpected device authority")
    return {"environment": "Jetson Orin NX; real published odometry and camera calibration; synthetic planar grid",
            "plan": {**plan_record, "length_m": route["length_m"], "waypoints": len(route["waypoints"]),
                     "age_ms": (int(pose["as_of_ns"]) - int(pose["stamp_ns"])) / 1e6, "route_ref": route["route_ref"]},
            "observation": {**sensor_record, "age_ms": ages, "calibration_ref": camera["artifact_ref"],
                            "width": calibration["width"], "height": calibration["height"]},
            "images": sorted({c["image"] for c in containers})}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("export", type=Path)
    cli.add_argument("--out", type=Path)
    args = cli.parse_args()
    report = json.dumps(verify(args.export), ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n", encoding="utf8")
    print(report)
