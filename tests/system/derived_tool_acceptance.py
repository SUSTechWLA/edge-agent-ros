"""Publish, replace and retire a read-only tool while a real ROS Agent stays online."""
import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from lifecycle_acceptance import TERMINAL, check, wait_for


ROOT = Path(__file__).resolve().parents[2]
TOOL = "derived.speed_kmh"
DEFAULT = {"schema_version": 1, "id": TOOL, "description": "用千米每小时报告本机前向速度",
           "field": "speed", "unit": "km/h"}
# Only the Agent observes this signed speed. The Gazebo rover remains stationary.
PUBLISHER = """
import rclpy
from nav_msgs.msg import Odometry
from rclpy.qos import qos_profile_sensor_data
rclpy.init()
node = rclpy.create_node("derived_tool_fixture")
publisher = node.create_publisher(Odometry, "/fixture/odom", qos_profile_sensor_data)
def emit():
    msg = Odometry()
    msg.header.frame_id = "odom"
    msg.header.stamp = node.get_clock().now().to_msg()
    msg.pose.pose.orientation.w = 1.0
    msg.twist.twist.linear.x = -0.5
    publisher.publish(msg)
node.create_timer(0.1, emit)
rclpy.spin(node)
"""


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fixtures(unit):
    cases = []
    for speed, expected in ((-0.5, -1.8), (1.25, 4.5), (0.0, 0.0)):
        sample = {"source": "/fixture/odom", "frame": "odom", "stamp_ns": 1,
                  "x": 0.0, "y": 0.0, "yaw": 0.0, "speed": speed}
        cases.append({"sample": sample, "expected": {"value": expected if unit == "km/h" else speed, "unit": unit}})
    return cases


def watch_once(project, image, console_name, state, model_network):
    """Give the watcher a disposable root, the test console, and only its own writable files."""
    def docker(*args):
        result = subprocess.run(["docker", *args], capture_output=True, encoding="utf-8", errors="replace", timeout=180)
        check(result.returncode == 0, result.stderr[-1600:] or result.stdout[-1600:] or str(args))
        return result.stdout.strip()

    name = project + "-author-" + uuid.uuid4().hex[:8]
    created = False
    try:
        docker("create", "--name", name, "--network", project + "_default", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
               "--mount", f"type=bind,source={state},target=/author", "--entrypoint", "python3",
               image, "/opt/edge/client/plugin_author_watch.py", "--once", "--url", f"http://{console_name}:8080",
               "--ollama-url", "http://model:11434", "--out-dir", "/author")
        created = True
        if model_network:
            docker("network", "connect", model_network, name)
        config = json.loads(docker("inspect", name))[0]
        check(config["HostConfig"]["ReadonlyRootfs"] and
              [m["Destination"] for m in config["Mounts"] if m["Type"] != "tmpfs"] == ["/author"],
              "author unexpectedly has access to another mount or writable root")
        output = docker("start", "--attach", name)
        check(docker("inspect", "--format", "{{.State.ExitCode}}", name) == "0", output[-1500:])
        return json.loads(output)
    finally:
        if created:
            docker("rm", "-f", name)


def watch_proposal(directory, project, image, console_name, model_network, report, missing):
    state = directory / "author"
    state.mkdir()
    first = watch_once(project, image, console_name, state, model_network)
    check(first["proposed"] == 1 and first["deferred"] == first["declined"] == 0,
          f"unexpected decisions on first scan: {first}")
    proposals = list((state / "decisions").glob("*.json"))
    check(len(proposals) == 1, "expected exactly one decision for the blocked task")
    path = proposals[0]
    decision = json.loads(path.read_text(encoding="utf-8"))
    check(decision["status"] == "proposed" and all(decision["source"].get(k) == v
          for k, v in report["blocked_task"].items()), "candidate does not identify the archived failure")
    check(decision["source"]["archive_id"] and decision["source"]["seq"] > 0, "candidate lacks its archive position")
    model = decision["model"]
    cause = {key: report["blocked_task"][key] for key in ("task_id", "trace_id", "reason_code")}
    check(model["source_task"] == cause and model["request"] == missing["text"], "watcher lost original trace or request")
    check(model["author_protocol"] == "ollama.chat" and model["reply"]["field"] == "speed" and
          model["reply"]["unit"] == "km/h" and model["reply"]["op"] == "read", "unexpected model response")

    saved = path.read_bytes()
    timestamp = path.stat().st_mtime_ns
    # Start a new process on the test console alone. It cannot regenerate a candidate with the model.
    resumed = watch_once(project, image, console_name, state, None)
    check(all(resumed[k] == 0 for k in ("proposed", "declined", "deferred", "recovered")),
          f"unexpected work after restart: {resumed}")
    check(list((state / "decisions").glob("*.json")) == [path] and path.read_bytes() == saved
          and path.stat().st_mtime_ns == timestamp, "restarting the watcher duplicated or rewrote the proposal")
    report["watcher"] = {"first": first, "restart_without_model": resumed, "source": decision["source"],
                         "artifact": path.relative_to(state).as_posix(), "sha256": hashlib.sha256(saved).hexdigest(),
                         "mounts": ["/author"], "root_readonly": True}
    report["model"] = model
    return decision["definition"]


def run(image, out, draft, model_network=None, watch_with_model=False):
    definition = json.loads(draft.read_text(encoding="utf-8-sig")) if draft else dict(DEFAULT)
    check(definition == DEFAULT, "this experiment requires the signed speed conversion defined in DEFAULT")
    project = "edge-derived-" + uuid.uuid4().hex[:12]
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    draft_input = "external" if draft else "test fixture"
    if model_network:
        draft_input = "watcher with Ollama" if watch_with_model else "ollama from blocked task"
    report = {"passed": False, "started_at": datetime.now(timezone.utc).isoformat(), "project": project,
              "revision": info["Config"]["Labels"]["org.opencontainers.image.revision"], "image_id": info["Id"],
              "draft_input": draft_input,
              "definition": definition,
              "fixture": {"topic": "/fixture/odom", "signed_speed_mps": -0.5}, "releases": []}
    out.parent.mkdir(parents=True, exist_ok=True)
    opener = build_opener(ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix="edge-derived-") as temp:
        directory = Path(temp)
        plugins = directory / "plugins"
        plugins.mkdir()
        registry = str(plugins / "derived")
        overlay = directory / "compose.json"
        write_json(overlay, {"services": {
            "agent": {"command": ["python3", "/opt/edge/agent/service.py", "--ros-args", "-r", "/odom:=/fixture/odom"]},
            "test-odom": {"image": image, "pull_policy": "never", "command": ["python3", "-c", PUBLISHER],
                          "environment": {"ROS_DOMAIN_ID": "148", "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}}}})
        env = {k:v for k,v in os.environ.items() if not k.startswith(("EDGE_", "COMPOSE_"))}
        env.update(COMPOSE_DISABLE_ENV_FILE="1", EDGE_CONSOLE_PORT="0", EDGE_ROS_DOMAIN_ID="148",
                   EDGE_IMAGE=image, EDGE_PLUGINS_DIR=str(plugins), EDGE_RETRY_LIMIT="3", PYTHONUTF8="1")

        def compose(*args):
            cmd = ["docker", "compose", "-p", project, "-f", str(ROOT/"compose.yaml"), "-f", str(overlay), *args]
            result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, timeout=120,
                                    encoding="utf-8", errors="replace")
            if result.returncode:
                raise RuntimeError(f"{args}: {result.stderr[-2000:]}")
            return result.stdout.strip()

        def release(*args, expect_success=True):
            cmd = [sys.executable, str(ROOT/"agent/author_tool.py"), *args, "--registry", registry]
            result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, encoding="utf-8", timeout=20)
            report["releases"].append({"action": args[0], "returncode": result.returncode})
            check((result.returncode == 0) == expect_success, result.stderr[-1500:] or str(args))

        def publish(tool, expected, cases, expect_success=True):
            path, tests = directory/"definition.json", directory/"cases.json"
            write_json(path, tool)
            write_json(tests, cases)
            release("publish", str(path), "--cases", str(tests), "--expect", expected, expect_success=expect_success)

        def started():
            cid = compose("ps", "-q", "agent")
            return subprocess.check_output(["docker", "inspect", "--format", "{{.Id}} {{.State.StartedAt}}", cid],
                                           text=True).strip()

        def api(path, body=None):
            req = Request(base+path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json"})
            with opener.open(req, timeout=5) as reply:
                return json.load(reply)

        def tool():
            return next((c for c in api("/agent/capabilities")["capabilities"] if c["id"] == definition["id"]), None)

        def submit(text, trigger=None):
            body = {"text":text}
            if trigger:
                body["trigger"] = trigger
            return api("/agent/tasks", body)["id"]

        def task(key):
            return api("/agent/tasks?" + urlencode({"id": key}))

        def terminal(key):
            return wait_for(lambda: (t if (t := task(key))["state"] in TERMINAL else None), key)

        def verify(key, version, unit, expected):
            item = terminal(key)
            check(item["state"] == "SUCCEEDED" and len(item["steps"]) == 1, str(item["detail"]))
            step = item["steps"][0]
            check(step["tool"] == definition["id"] and step["version"] == version,
                  "the saved plan used another tool or release")
            e = step["evidence"]
            check(e["definition_version"] == version and e["unit"] == unit and math.isclose(e["value"], expected),
                  "wrong release or odometry conversion")
            check(e["source"] == "/fixture/odom" and e["frame"] == "odom" and e["stamp_ns"] > 0,
                  "insufficient ROS sample evidence")
            check(e["field"] == "speed" and e["raw_unit"] == "m/s" and e["raw_value"] == -0.5,
                  "missing original signed measurement")
            return step

        def position():
            state = api("/api/state")
            source = state["sources"]["truth"]
            if state["environment"] == "simulation" and source["fresh"]:
                return [source["data"]["x"], source["data"]["y"]]

        paused = False
        try:
            compose("up", "-d", "--no-build", "sim","witness","bridge","core","gateway","agent","console","test-odom")
            address = compose("port", "console", "8080")
            check(address.startswith("127.0.0.1:"), "isolated loopback port required")
            base = "http://" + address
            origin = wait_for(position, "fresh independent Gazebo pose", 60)
            wait_for(lambda: api("/agent/capabilities")["capabilities"], "Agent startup")
            initial = started()
            check(tool() is None, "tool present before publish")
            missing = terminal(submit(definition["description"]))
            check(missing["state"] == "BLOCKED" and not missing["steps"], "missing conversion was reported as available")
            blocked = json.loads(missing["events"][-1]["payload"])
            cause = {"task_id": missing["id"], "trace_id": missing["trace_id"],
                     "reason_code": blocked["diagnostic"]["code"]}
            report["blocked_task"] = {**cause, "event_id": blocked["trace"]["event_id"]}
            if watch_with_model:
                event_id = report["blocked_task"]["event_id"]
                wait_for(lambda: api("/api/events?" + urlencode({"event_id": event_id}))["events"],
                         "archived BLOCKED event")
                console_id = compose("ps", "-q", "console")
                console_name = subprocess.check_output(
                    ["docker", "inspect", "--format", "{{.Name}}", console_id], text=True).strip().lstrip("/")
                definition = watch_proposal(directory, project, image, console_name, model_network, report, missing)
                check({**definition, "id": TOOL} == DEFAULT, "watcher did not author the exact signed speed conversion")
                check(tool() is None and not list(Path(registry).glob("*.json")), "watcher activated its own draft")
                unchanged = task(missing["id"])
                check(unchanged["state"] == "BLOCKED" and not unchanged["steps"],
                      "watcher resumed or dispatched the original task")
                report["definition"] = definition
            elif model_network:
                # Only the author contacts the chosen model network. It has the query result
                # and an empty record directory; the live plugin registry belongs to the publisher.
                author_dir = directory / "author"
                author_dir.mkdir()
                write_json(author_dir / "task.json", missing)
                name = project + "-author"
                cmd = ["docker", "run", "--rm", "--name", name, "--network", model_network,
                       "--entrypoint", "python3", "-e", "EDGE_OLLAMA_URL=http://model:11434",
                       "-v", f"{author_dir}:/author", image, "/opt/edge/agent/author_tool.py", "draft",
                       "--id", TOOL, "--from-task", "/author/task.json", "--record", "/author/model.json"]
                try:
                    authored = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
                except subprocess.TimeoutExpired:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
                    raise
                model_record = author_dir / "model.json"
                if model_record.exists():
                    report["model"] = json.loads(model_record.read_text(encoding="utf-8"))
                check(authored.returncode == 0, authored.stderr[-1500:] or "no proposal returned")
                check(report["model"]["source_task"] == cause and report["model"]["request"] == missing["text"],
                      "model draft lost the failing task or trace")
                definition = json.loads(authored.stdout)
                report["definition"] = definition
                check(definition == DEFAULT, "model did not author the requested signed speed conversion")

            replacement = {**definition, "unit": "m/s", "description": "用米每秒报告本机前向速度"}
            publish(definition, "absent", fixtures("km/h"))
            first = wait_for(tool, "published tool missing")["version"]
            trigger = {"kind":"agent", "source":"derived-tool-acceptance", "id":project+"-available",
                       "parent_task_id":missing["id"], "parent_event_id":report["blocked_task"]["event_id"]}
            original = submit(definition["description"], trigger)
            saved = verify(original, first, "km/h", -1.8)
            executed = task(original)
            check(executed["trace_id"] == cause["trace_id"] and executed["trigger"] == trigger,
                  "execution after publication lost its original task")
            report["first"] = {"task_id": original, "trace_id": executed["trace_id"], "trigger": trigger, "step": saved}
            bad = fixtures("m/s")
            bad[0]["expected"]["value"] = 123
            publish(replacement, first, bad, expect_success=False)
            check(tool()["version"] == first, "failed candidate replaced the active version")

            compose("pause", "test-odom")
            paused = True
            time.sleep(2.3)  # Agent status rejects samples older than two seconds.
            pending = submit(definition["description"])
            wait_for(lambda: any(e["kind"] == "retry" for e in task(pending)["events"]), "sample did not become stale")
            publish(replacement, first, fixtures("m/s"))
            second = wait_for(lambda: (c if (c := tool()) and c["version"] != first else None), "no new version")["version"]
            compose("unpause", "test-odom")
            paused = False
            held = terminal(pending)
            step = held["steps"][0]
            check(held["state"] == "RECOVERY_REQUIRED" and held["can_retry"] and step["version"] == first
                  and not step.get("evidence"), "in-flight step used replacement or failed to record uncertainty")
            diagnostic = next(e for e in held["events"] if e["kind"] == "retry_exhausted")
            check(json.loads(diagnostic["payload"])["diagnostic"]["code"] == "tool.binding_unavailable",
                  "incorrect attribution for missing pinned definition")
            report["second"] = verify(submit(replacement["description"]), second, "m/s", -0.5)

            release("remove", definition["id"], "--expect", first, expect_success=False)
            check(tool()["version"] == second, "stale retirement withdrew the new release")
            publish(definition, second, fixtures("km/h"))
            wait_for(lambda: tool() and tool()["version"] == first, "original definition did not regain its identity")
            api("/agent/retry", {"id": pending})
            resumed = verify(pending, first, "km/h", -1.8)
            check(resumed["operation_id"] == step["operation_id"], "restored step received a new operation ID")
            report["in_flight"] = {"task_id": pending, "operation_id": resumed["operation_id"],
                                   "conflict": "tool.binding_unavailable", "resumed_version": first}

            release("remove", definition["id"], "--expect", first)
            wait_for(lambda: tool() is None, "retired tool remains visible")
            after = terminal(submit(definition["description"]))
            check(after["state"] == "BLOCKED" and not after["steps"], "retired tool still admits new work")
            check(task(original)["steps"][0] == saved, "old tool receipt changed")
            original_blocked = task(missing["id"])
            check(original_blocked["state"] == "BLOCKED" and not original_blocked["steps"],
                  "published capability rewrote the history of the original blocked task")
            check(started() == initial, "Agent restarted during hot replacement")
            report["agent_identity"] = initial
            report["gazebo_displacement_m"] = math.dist(origin, wait_for(position, "final independent world pose"))
            check(report["gazebo_displacement_m"] < .025, "read-only tool displaced the simulated rover")
            report["passed"] = True
        except Exception as exc:
            report["failure"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            try:
                try:
                    if paused:
                        compose("unpause", "test-odom")
                finally:
                    compose("down", "--volumes", "--remove-orphans", "--timeout", "3")
            finally:
                write_json(out, report)
                print(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="edge-agent-ros:v0.1")
    parser.add_argument("--out", type=Path, default=ROOT/".ci-results/system/derived-tool.json")
    author = parser.add_mutually_exclusive_group()
    author.add_argument("--draft", type=Path, help="externally authored JSON for the same speed conversion experiment")
    author.add_argument("--model-network", help="Docker network where model:11434 serves Ollama; draft from the blocked task")
    parser.add_argument("--watch-with-model", action="store_true",
                        help="subscribe to the live console from an isolated author container; requires --model-network")
    args = parser.parse_args()
    if args.watch_with_model and not args.model_network:
        parser.error("--watch-with-model requires --model-network")
    run(args.image, args.out, args.draft, args.model_network, args.watch_with_model)
