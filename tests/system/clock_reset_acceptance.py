"""Reset the clock of an isolated Gazebo project; verify live, replay and recovery via HTTP."""
import argparse
import json
import subprocess
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def run(project):
    if project == "edge-agent-ros":
        raise ValueError("this test requires an isolated Gazebo project")
    compose = ["docker", "compose", "-p", project]
    port = subprocess.check_output([*compose, "port", "console", "8080"], text=True).strip()
    if not port.startswith("127.0.0.1:"):
        raise ValueError("use a test console bound to loopback")
    http = build_opener(ProxyHandler({}))
    def get(path):
        with http.open(f"http://{port}/api/{path}", timeout=4) as r:
            return json.load(r)

    def wait(read, description):
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            value = read()
            if value:
                return value
            time.sleep(.2)
        raise RuntimeError(description)

    def mapped():
        state = get("state")
        if state["environment"] != "simulation":
            raise ValueError("expected a simulation")
        grid = state["sources"]["map"]
        if grid["fresh"] and 100 in grid["data"]["cells"]:
            return grid["stamp_s"]

    old = wait(lambda: (s if (s := mapped()) and s > 4 else None), "initial map")
    start = get("state")["cursor"]
    reply = subprocess.check_output([*compose, "exec", "-T", "sim", "/usr/local/bin/edge-entrypoint",
        "gz", "service", "-s", "/world/edge_lab/control", "--reqtype", "gz.msgs.WorldControl",
        "--reptype", "gz.msgs.Boolean", "--timeout", "6000", "--req", "reset: {all: true}"], text=True)
    if "data: true" not in reply:
        raise RuntimeError("Gazebo did not acknowledge the reset")
    events = wait(lambda: get(f"events?after={start}&kind=sensor.clock_reset")["events"], "clock reset event")
    reset = events[0]
    at_reset = get(f"replay?at={reset['seq']}")
    if at_reset["sources"]["map"]["available"] or at_reset["trail"]:
        raise AssertionError("old map/trail survived the clock reset")
    fresh = wait(lambda: (s if (s := mapped()) and s < old else None), "new occupied map in reset epoch")
    return {"passed":True, "project":project,"old_map_stamp_s":old, "new_map_stamp_s":fresh,
            "reset_seq":reset["seq"],"clock_jump":reset["data"],
            "at_reset_map_available":at_reset["sources"]["map"]["available"], "at_reset_trail_count":len(at_reset["trail"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    raw = json.dumps(run(args.project), indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(raw, encoding="utf-8")
    print(raw)
