"""Verify the timestamp provider directly or through an isolated, running language Agent."""
import argparse
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import time
import uuid
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "examples/timestamp-audit"


def check(condition, reason):
    if not condition:
        raise AssertionError(reason)


def verify_agent(image, out):
    run_id = uuid.uuid4().hex[:12]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "EDGE_"))}
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    report = {"passed": False, "started_at": datetime.now(timezone.utc).isoformat(),
              "image": image, "image_id": info["Id"], "revision": info["Config"]["Labels"].get("org.opencontainers.image.revision"),
              "project": "edge-sample-" + run_id, "model": "local", "scope": "internal Docker network, Agent and read-only HTTP provider"}

    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        plugins = temp / "plugins"
        plugins.mkdir()
        config = {"services": {
            "agent": {"image": image, "pull_policy": "never",
                      "command": ["python3", "/opt/edge/agent/service.py"],
                      "environment": {"ROS_DOMAIN_ID": "147", "EDGE_RETRY_LIMIT": "1",
                                      "EDGE_PLUGIN_DIR": "/sample-plugins", "EDGE_EXTRA_PLUGIN_DIR": "",
                                      "EDGE_OLLAMA_URL": "", "EDGE_PLANNER_URL": ""},
                      "volumes": [{"type": "bind", "source": str(plugins), "target": "/sample-plugins", "read_only": True}]},
            "timestamp-audit": {"image": image, "pull_policy": "never", "read_only": True,
                                "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                                "entrypoint": ["python3", "-B", "/candidate/provider.py"],
                                "volumes": [{"type": "bind", "source": str(SAMPLE), "target": "/candidate", "read_only": True}]}},
            "networks": {"default": {"internal": True}}}
        compose_file = temp / "compose.json"
        compose_file.write_text(json.dumps(config), encoding="utf-8")

        def compose(*args, stdin=None):
            cmd = ["docker", "compose", "-p", report["project"], "-f", str(compose_file), *args]
            return subprocess.run(cmd, input=stdin, capture_output=True, check=True, env=env,
                                  encoding="utf-8", errors="replace", timeout=90).stdout.strip()

        # Use the public HTTP endpoint from loopback inside the Agent container. No host ports or robot network.
        client = ("import json,sys; from urllib.request import Request,urlopen; path,body=json.load(sys.stdin); "
                  "req=Request('http://127.0.0.1:8090'+path,data=None if body is None else json.dumps(body).encode(),"
                  "headers={'Content-Type':'application/json'}); print(json.dumps(json.load(urlopen(req,timeout=5))))")

        def api(path, body=None):
            return json.loads(compose("exec", "-T", "agent", "python3", "-c", client, stdin=json.dumps([path, body])))

        def catalog():
            return [v["id"] for v in api("/agent/capabilities")["capabilities"]]

        def done(key):
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                task = api("/agent/tasks?" + urlencode({"id": key}))
                if task["state"] in ("SUCCEEDED", "BLOCKED", "RECOVERY_REQUIRED", "CANCELED"):
                    return task
                time.sleep(.12)
            raise TimeoutError("task did not finish: " + key)

        text = "检查这组采样时间戳的重复与回拨：[100,110,110,95,110,140]"
        def submit(trigger):
            return api("/agent/tasks", {"text": text, "trigger": {"kind": "user", "source": "audit-probe", "id": trigger}})["id"]

        try:
            compose("up", "-d", "--no-build")
            deadline = time.monotonic() + 35
            while True:
                try:
                    before = catalog()
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.2)
            container = compose("ps", "-q", "agent")
            def started_at():
                return subprocess.check_output(["docker", "inspect", "-f", "{{.State.StartedAt}}", container], text=True).strip()
            started = started_at()
            check("timestamp_audit" not in before, "tool present before publication")
            unavailable = done(submit("before"))
            check(unavailable["state"] == "BLOCKED" and not unavailable["steps"], "absent tool produced a plan")
            staging = plugins / "timestamp.tmp"
            live = plugins / "timestamp_audit.json"
            shutil.copyfile(SAMPLE / "tool.json", staging)
            staging.replace(live)
            check("timestamp_audit" in catalog(), "tool missing after atomic publication")
            key = submit("active")
            task = done(key)
            expected = {"sample_count": 6, "duplicate_count": 2, "backward_count": 1, "max_forward_gap_ms": 30}
            check(task["state"] == "SUCCEEDED" and len(task["steps"]) == 1, "timestamp task failed")
            step = task["steps"][0]
            check(step["tool"] == "timestamp_audit" and step["evidence"] == expected and step["operation_id"] == key+"/step/1",
                  "tool, operation or timestamp evidence differs")
            check(submit("active") == key, "trigger replay changed task identity")
            live.unlink()
            check("timestamp_audit" not in catalog(), "tool still advertised after removal")
            after = done(submit("after"))
            check(after["state"] == "BLOCKED" and not after["steps"], "unpublished tool produced a plan")
            original = done(key)
            kinds = [e["kind"] for e in original["events"]]
            check(original["steps"] == task["steps"] and original["state"] == "SUCCEEDED", "original task changed after removal")
            check(all(kinds.count(k) == 1 for k in ("adapter.request", "adapter.response", "verified")),
                  "expected one provider request, response and verification")
            check(container == compose("ps", "-q", "agent") and started == started_at(), "agent restarted during publication")
            report.update(passed=True, text=text, before=unavailable["state"], after=after["state"], task_id=key,
                          operation_id=step["operation_id"], evidence=step["evidence"], version=step["version"],
                          trace_id=task["trace_id"], event_kinds=kinds, replay_same_task=True,
                          original_query_after_remove=original["state"], agent_started_at=started, agent_restarted=False)
        except Exception as exc:
            report["failure"] = str(exc)
            report["service_logs"] = compose("logs", "--no-color", "--tail", "25")
            raise
        finally:
            try:
                compose("down", "--volumes", "--remove-orphans", "--timeout", "3")
            except (OSError, subprocess.SubprocessError) as exc:
                report.update(passed=False, cleanup_failure=str(exc))
                raise
            finally:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--image", help="exercise the public Agent API in two private Docker services")
    args = parser.parse_args()
    if args.image:
        verify_agent(args.image, args.out)
        sys.exit(0)
    # This path imports the fixed repository example. Docker mode keeps its code inside the provider container.
    sys.path.insert(0, str(SAMPLE))
    from provider import Handler
    with ThreadingHTTPServer(("127.0.0.1",0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run([sys.executable, str(ROOT/"tests/system/provider_contract.py"),
                            "--manifest", str(SAMPLE/"tool.json"), "--cases", str(SAMPLE/"cases.json"),
                            "--url", f"http://127.0.0.1:{server.server_port}/invoke", "--out", str(args.out)], check=True)
        finally:
            server.shutdown()
            thread.join()
