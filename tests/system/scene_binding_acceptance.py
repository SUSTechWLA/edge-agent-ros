"""Check that an existing, different ROS mission cannot complete a named-place task."""
import argparse
import json
import math
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


def run(project, out):
    if not project.startswith("edge-test-") and not project.startswith("edge-proof-"):
        raise ValueError("select the dedicated system-test Compose project")
    compose = ["docker", "compose", "-p", project]
    address = subprocess.check_output([*compose, "port", "console", "8080"], text=True).strip()
    if not address.startswith("127.0.0.1:"):
        raise ValueError("expected an isolated loopback simulation")
    http = build_opener(ProxyHandler({}))

    def api(path, body=None):
        req = Request("http://" + address + path, data=json.dumps(body).encode() if body else None,
                      headers={"Content-Type": "application/json"})
        with http.open(req, timeout=5) as response:
            return json.load(response)

    def position():
        state = api("/api/state")
        if state["environment"] != "simulation" or not state["sources"]["truth"]["fresh"]:
            raise ValueError("fresh Gazebo world pose required")
        pose = state["sources"]["truth"]["data"]
        return [pose["x"], pose["y"]]

    key = "collision-" + uuid.uuid4().hex[:12]
    op = key + "/step/1"
    start = position()
    preview_id = key + "-preview"
    api("/agent/tasks", {"id": preview_id, "text": "只规划到庭院的路线"})
    preview = None
    for _ in range(80):
        query = api("/agent/tasks?" + urlencode({"id": preview_id}))
        if query["state"] == "SUCCEEDED":
            preview = query["steps"][0]["evidence"]
            break
        time.sleep(.2)
    if not preview or preview["destination_id"] != "garden" or preview["distance_m"] <= 0:
        raise AssertionError("a preview must resolve the actual destination and return a route")
    if math.dist(position(), start) >= .025 or query["steps"][0]["tool"] != "preview_route":
        raise AssertionError("a route preview must stay read-only")
    # This valid, already completed ROS mission has the ID the new task will use,
    # but its only waypoint is the starting position. It never visited the garden.
    target = ",".join(str(value) for value in start)
    subprocess.run([*compose, "exec", "-T", "core", "/usr/local/bin/edge-entrypoint", "python3",
                    "/opt/edge/client/mission.py", "run", op, target, "--wait"], check=True, timeout=45)
    api("/agent/tasks", {"id": key, "text": "去庭院巡检"})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        task = api("/agent/tasks?" + urlencode({"id": key}))
        if task["state"] in ("SUCCEEDED", "BLOCKED", "RECOVERY_REQUIRED", "FAILED"):
            break
        time.sleep(.2)
    final = position()
    terminal = json.loads(task["events"][-1]["payload"])["trace"]["code"]
    record = {"case": "different_goal_same_ros_id", "task_id": key, "operation_id": op,
              "state": task["state"], "code": terminal, "start_xy_m": start, "end_xy_m": final,
              "preview": {"tool":"preview_route", "task_id":preview_id, **preview},
              "claimed_evidence": task["steps"][0].get("evidence"),
              "distance_from_garden_m": math.dist(final, [3.3, 1.35]),
              "passed": task["state"] == "BLOCKED" and terminal == "core.id_conflict"
                        and math.dist(start, final) < .025 and math.dist(final, [3.3, 1.35]) > 1}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(record, ensure_ascii=True))
    if not record["passed"]:
        raise AssertionError("another mission must not complete a named-place task")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    run(args.project, args.out)
