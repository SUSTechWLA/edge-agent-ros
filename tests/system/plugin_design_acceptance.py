"""Exercise an actual blocked task, archived author decision and optional real Ollama design."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

from lifecycle_acceptance import check, wait_for
from runtime_plugin_acceptance import mounted

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client"))
from plugin_author_watch import Watch

MODEL_IMAGE = "ollama/ollama@sha256:910d0d8107b538fb0894e63163eb195de0cc7aefcbc666dc62d3b18f71124592"
GOAL = "Return the width and height of a PNG artifact in pixels."


class DeclineOdometry(BaseHTTPRequestHandler):
    """Repeatable unsupported odometry reply. The independent design model runs in Docker."""
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        wire = json.dumps({"message": {"content": json.dumps({"field": "unsupported", "unit": "none",
                                                              "op": "read", "threshold": 0})}}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(wire)


def run(args):
    project = "edge-design-" + uuid.uuid4().hex[:12]
    env = dict(os.environ, COMPOSE_DISABLE_ENV_FILE="1", PYTHONUTF8="1",
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    report = {"passed": False, "project": project, "goal": GOAL, "model": args.model if args.cache else None,
              "fixture": "Isolated single software task, deterministic odometry decline, real Console archive."}
    def command(*cmd):
        result = subprocess.run(cmd, capture_output=True, cwd=ROOT, env=env, timeout=145, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(result.stderr[-1200:] or result.stdout[-1200:])
        return result.stdout.strip()

    def read(url, body=None):
        req = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type":"application/json"})
        with urlopen(req, timeout=5) as res:
            return json.load(res)

    with tempfile.TemporaryDirectory(prefix=project) as temp:
        root = Path(temp)
        public = root/"events"
        for folder in (public/"plugin.design", public/"plugin.author", root/"registry"):
            folder.mkdir(parents=True)
        def ros(service):
            return {"image":args.image, "pull_policy":"never", "command":["python3", "/opt/edge/"+service],
                    "environment":{"ROS_LOCALHOST_ONLY":"1", "EDGE_OLLAMA_URL":"", "EDGE_PLANNER_URL":"",
                                   "EDGE_EXTRA_PLUGIN_DIR":""}}
        services = {"agent":dict(ros("agent/service.py"), volumes=["tasks:/var/lib/edge-tasks",
                                                                   mounted(root/"registry","/etc/edge/plugins")]),
                    "console":dict(ros("console/server/main.py"), ports=["127.0.0.1::8080"],
                                   volumes=["tasks:/var/lib/edge-tasks:ro",mounted(public, "/var/lib/edge-module-sources"),
                                            mounted(public/"plugin.author","/var/lib/edge-author-events")])}
        volumes = {"tasks":{}}
        if args.cache:
            services["model"] = {"image": MODEL_IMAGE, "pull_policy": "never", "ports": ["127.0.0.1::11434"],
                                 "volumes": ["cache:/cache:ro", "model-home:/root/.ollama"]}
            volumes["cache"] = {"external":True, "name":args.cache}
            volumes["model-home"] = {}
        stack = root/"compose.json"
        stack.write_text(json.dumps({"services":services, "volumes":volumes}), encoding="utf-8")
        compose = ["docker", "compose", "-p", project, "-f", str(stack)]
        stub = ThreadingHTTPServer(("127.0.0.1",0), DeclineOdometry)
        thread = threading.Thread(target=stub.serve_forever)
        thread.start()
        try:
            if args.cache:
                command(*compose,"run","--no-deps","--rm","-T","--entrypoint","/bin/sh","model","-ec",
                        "test -d /cache/models/blobs && mkdir -p /root/.ollama/models && cp -a /cache/models/. /root/.ollama/models/")
            command(*compose, "up", "-d")
            base = "http://" + command(*compose, "port", "console", "8080")
            def archived(kind, source):
                return read(base + "/api/events?" + urlencode({"kind":kind, "source":source,
                                                              "task_id":"png-dimensions"}))["events"]
            wait_for(lambda: read(base+"/agent/capabilities").get("capabilities"), "Agent/Console")
            read(base+"/agent/tasks", {"id":"png-dimensions", "text":GOAL})
            blocked = wait_for(lambda: archived("agent.blocked","mission_agent/tasks"), "archived planner failure")[0]
            previous = os.environ.get("EDGE_AUTHOR_EVENT_DB")
            os.environ["EDGE_AUTHOR_EVENT_DB"] = str(public/"plugin.author"/"module-events.sqlite")
            try:
                watch = Watch(base, "http://127.0.0.1:"+str(stub.server_port), "odometry-fixture", root/"private-author")
                try:
                    check(watch.poll()[0]["declined"]==1, "expected one author decline")
                finally:
                    watch.close()
            finally:
                if previous is None:
                    os.environ.pop("EDGE_AUTHOR_EVENT_DB")
                else:
                    os.environ["EDGE_AUTHOR_EVENT_DB"] = previous
            event = wait_for(lambda: archived("plugin.author.declined","journal:plugin.author"), "archived author decline")[0]
            event_path = root/"event.json"
            event_path.write_text(json.dumps(event),encoding="utf-8")
            decision = root/"private-author"/"decisions"/(event["data"]["receipt_id"]+".json")
            cli = [sys.executable, str(ROOT/"client/plugin_design.py"), "--event", str(event_path), "--decision",
                   str(decision), "--out-dir", str(root/"private-design"), "--journal",
                   str(public/"plugin.design"/"module-events.sqlite")]
            if args.cache:
                url = "http://" + command(*compose, "port", "model", "11434")
                tags = wait_for(lambda: read(url+"/api/tags"), "Ollama models")
                report["model_digest"] = next(t["digest"] for t in tags["models"] if t["name"]==args.model)
                cli.extend(["--ollama-url", url, "--model", args.model])
            result = json.loads(command(*cli))
            path = Path(result["path"])
            draft = json.loads(path.read_bytes())
            requested = wait_for(lambda: archived("plugin.design.requested","journal:plugin.design"), "design request")[0]
            finished = wait_for(lambda: archived("plugin.design.drafted","journal:plugin.design"), "archived design")[0]
            chain = [blocked,event,requested,finished]
            check(all(child["parent_event_id"]==parent["event_id"] and child["trace_id"]==parent["trace_id"]
                      for parent,child in zip(chain, chain[1:])), "causal link missing")
            check(draft["goal"]==GOAL and finished["data"]["draft_sha256"]==hashlib.sha256(path.read_bytes()).hexdigest(),
                  "draft differs from source or archive")
            original = read(base+"/agent/tasks?id=png-dimensions")
            check(original["state"]=="BLOCKED" and original["steps"]==[] and not list((root/"registry").glob("*.json")),
                  "draft should leave the software task and registry unchanged")
            report.update(passed=True, source_sha256=hashlib.sha256((ROOT/"client/plugin_design.py").read_bytes()).hexdigest(),
                          image_id=command("docker","image","inspect","--format","{{.Id}}",args.image),
                          state=original["state"], steps=len(original["steps"]), synthetic_test_draft=draft,
                          events=[{k:row[k] for k in ("seq","event_id","parent_event_id","trace_id","kind","source","data")}
                                  for row in chain])
        except Exception as exc:
            report["error"] = str(exc)
            raise
        finally:
            stub.shutdown()
            thread.join()
            stub.server_close()
            try:
                command(*compose,"down","--volumes","--remove-orphans","--timeout","2")
            finally:
                args.out.parent.mkdir(parents=True,exist_ok=True)
                args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="edge-agent-ros:v0.1")
    parser.add_argument("--cache", help="existing read-only Ollama cache volume for a real model proposal")
    parser.add_argument("--model", default="qwen2.5:1.5b")
    parser.add_argument("--out", type=Path, required=True)
    run(parser.parse_args())
