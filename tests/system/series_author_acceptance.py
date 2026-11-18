"""A real Ollama author, managed publication and a software-only Agent task in isolated Docker."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler, Request
import uuid

from lifecycle_acceptance import TERMINAL, check, wait_for
from runtime_plugin_acceptance import mounted

ROOT = Path(__file__).resolve().parents[2]
MODEL_IMAGE = "ollama/ollama@sha256:910d0d8107b538fb0894e63163eb195de0cc7aefcbc666dc62d3b18f71124592"
TEXT = "计算相邻采样间隔的最大值 [100,103,111,115]"
TOOL = "derived.sample_gap"


def run(args):
    project = "edge-series-" + uuid.uuid4().hex[:12]
    opener = build_opener(ProxyHandler({}))
    env = dict(os.environ, COMPOSE_DISABLE_ENV_FILE="1", PYTHONUTF8="1",
               NO_PROXY="localhost,127.0.0.1", no_proxy="localhost,127.0.0.1")
    report = {"passed": False, "project": project, "utc": datetime.now(timezone.utc).isoformat(),
              "scope": "software-only ROS Agent, real model author, isolated task and registry",
              "image": args.image, "model_image": MODEL_IMAGE, "model": args.model}

    def command(*cmd):
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, timeout=160,
                              encoding="utf-8", errors="replace")
        if proc.returncode:
            raise RuntimeError(proc.stderr[-1500:] or proc.stdout[-1000:])
        return proc.stdout.strip()

    def read(url, data=None):
        req = Request(url, data=None if data is None else json.dumps(data).encode(),
                      headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=5) as res:
            return json.load(res)

    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    with tempfile.TemporaryDirectory(prefix=project) as name:
        root = Path(name)
        plugins = root / "plugins"
        plugins.mkdir()
        ports = lambda n: [{"target": n, "host_ip": "127.0.0.1"}]
        config = {
            "services": {
                "agent": {"image": args.image, "pull_policy": "never", "command": ["python3", "/opt/edge/agent/service.py"],
                          "environment": {"ROS_LOCALHOST_ONLY": "1", "EDGE_EXTRA_PLUGIN_DIR": "",
                                          "EDGE_OLLAMA_URL": "", "EDGE_PLANNER_URL": ""},
                          "ports": ports(8090), "volumes": [mounted(ROOT/"agent", "/opt/edge/agent"),
                                                              mounted(plugins, "/etc/edge/plugins")]},
                "model": {"image": MODEL_IMAGE, "pull_policy": "never", "ports": ports(11434),
                          "entrypoint": ["/bin/sh","-ec"],
                          "command": ["mkdir -p /root/.ollama/models && cp -a /cache/models/. /root/.ollama/models/ && exec ollama serve"],
                          "volumes": ["models:/cache:ro"]}},
            "volumes": {"models": {"external": True, "name": args.cache}}}
        compose = ["docker", "compose", "-p", project, "-f", str(write(root/"compose.json", config))]
        try:
            report["image_id"] = command("docker", "image", "inspect", "--format", "{{.Id}}", args.image)
            report["source_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                       for p in sorted((ROOT/"agent").glob("*.py"))}
            command(*compose, "up", "-d")
            agent = "http://" + command(*compose, "port", "agent", "8090")
            model = "http://" + command(*compose, "port", "model", "11434")
            catalog = lambda: [row["id"] for row in read(agent+"/agent/capabilities")["capabilities"]]
            wait_for(catalog, "Agent capabilities")
            tags = wait_for(lambda: read(model+"/api/tags"), "Ollama model directory")
            report["model_digest"] = next(row["digest"] for row in tags["models"] if row["name"] == args.model)
            def execute(key, text=TEXT):
                created = read(agent+"/agent/tasks", {"id": key, "text": text})
                task_url = agent+"/agent/tasks?" + urlencode({"id": created["id"], "limit": 128})
                return wait_for(lambda: (t if (t := read(task_url))["state"] in TERMINAL else None), key)

            original = execute("missing")
            check(original["state"] == "BLOCKED" and original["steps"] == [], "unexpected pre-existing capability")
            report["before"] = original["state"]
            record = root/"author.json"
            candidate = json.loads(command(sys.executable, str(ROOT/"agent/author_tool.py"), "draft", "--from-task",
                str(write(root/"task.json", original)), "--ollama-url", model, "--model", args.model,
                "--record", str(record), "--id", TOOL))
            report["author"] = json.loads(record.read_text(encoding="utf-8"))
            report["definition"] = candidate
            cases = [{"sample":{"series":"[8,11,14]"}, "expected":{"value":3}},
                     {"sample":{"series":"[100,75,90]"}, "expected":{"value":15}},
                     {"sample":{"series":"[10,8,3]"}, "expected":{"value":-2}}]
            manager = [sys.executable, str(ROOT/"agent/plugin_manager.py")]
            params = ["--kind", "derived", "--root", str(plugins), "--trace-id", original["trace_id"],
                      "--parent-event-id", original["events"][-1]["event_id"]]
            proposal = write(root/"candidate.json", candidate)
            released = json.loads(command(*manager, "publish", str(proposal), "--cases", str(write(root/"cases.json", cases)),
                                          "--expect", "absent", *params))
            report["publish"] = released
            check(TOOL in catalog(), "approved tool absent")
            task = execute("active")
            step = task["steps"][0]
            check(task["state"] == "SUCCEEDED" and step["tool"] == TOOL and step["evidence"]["value"] == 8,
                  "expected maximum adjacent interval 8")
            check(step["evidence"]["input_sha256"] == hashlib.sha256(b"[100,103,111,115]").hexdigest(), "input drift")
            report["execution"] = {"task_id": task["id"], "trace_id": task["trace_id"], "step": step,
                                   "events": [{k: row[k] for k in ("event_id","operation_id","kind")} for row in task["events"]]}
            bad = execute("bad", "计算相邻采样间隔的最大值 [7]")
            check(bad["state"] == "BLOCKED", "single sample incorrectly reports a gap")
            report["invalid"] = {"state": bad["state"], "detail": bad["detail"]}
            report["remove"] = json.loads(command(*manager, "remove", TOOL, "--expect", released["version"], *params))
            report["after"] = execute("after")["state"]
            check(TOOL not in catalog() and report["after"] == "BLOCKED", "removed tool still usable")
            check(execute("active")["steps"] == task["steps"], "replay changed original completed task")
            report["passed"] = True
        except Exception as exc:
            report["error"] = str(exc)
            record = root / "author.json"
            if record.exists():
                report["author"] = json.loads(record.read_text(encoding="utf-8"))
            raise
        finally:
            try:
                command(*compose, "down", "--volumes", "--remove-orphans", "--timeout", "2")
            finally:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="edge-agent-ros:v0.1")
    parser.add_argument("--cache", required=True, help="read-only existing Ollama model volume")
    parser.add_argument("--model", default="qwen2.5:1.5b")
    parser.add_argument("--out", required=True, type=Path)
    run(parser.parse_args())
