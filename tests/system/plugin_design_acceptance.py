"""Exercise an actual blocked task, archived author decision and optional real Ollama design."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
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


class Fixture(DeclineOdometry):
    """Independent witnesses for the simulator-only device and restricted design-model calls."""

    def reply(self, data):
        wire = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)

    def do_GET(self):
        if self.path != "/counts":
            return self.send_error(404)
        with self.server.guard:
            self.reply(dict(self.server.counts))

    def do_POST(self):
        if self.path == "/author/api/chat":
            with self.server.guard:
                self.server.counts["author_calls"] += 1
            return super().do_POST()
        if self.path == "/device":
            with self.server.guard:
                self.server.counts["device_calls"] += 1
            return self.send_error(409, "unexpected fixture device call")
        if self.path != "/design/api/chat":
            return self.send_error(404)
        wire = self.rfile.read(int(self.headers["Content-Length"]))
        payload = json.loads(json.loads(wire)["messages"][-1]["content"])
        with self.server.guard:
            self.server.counts["design_calls"] += 1
            self.server.counts["model_contracts_sha256"] = snapshot_digest(payload["contracts"])
        try:
            req = Request("http://model:11434/api/chat", data=wire, headers={"Content-Type":"application/json"})
            with urlopen(req, timeout=100) as res:
                self.reply(json.load(res))
        except OSError:
            self.send_error(502)


def snapshot_digest(value):
    wire = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",",":")).encode()
    return hashlib.sha256(wire).hexdigest()


def run(args):
    project = "edge-design-" + uuid.uuid4().hex[:12]
    env = {k:v for k,v in os.environ.items() if not k.startswith(("COMPOSE_", "EDGE_"))}
    env.update(COMPOSE_DISABLE_ENV_FILE="1", PYTHONUTF8="1",
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    report = {"passed":False, "project":project, "goal":GOAL, "model":args.model if args.cache else None,
              "fixture":"One operator task; Docker Agent, Console, live author and design watcher, independent call counts."}

    def command(*cmd):
        result = subprocess.run(cmd,capture_output=True,cwd=ROOT,env=env,timeout=160,encoding="utf-8",errors="replace")
        if result.returncode:
            raise RuntimeError(result.stderr[-1200:] or result.stdout[-1200:])
        return result.stdout.strip()

    def read(url, body=None):
        req = Request(url,data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type":"application/json"})
        with urlopen(req,timeout=5) as res:
            return json.load(res)

    with tempfile.TemporaryDirectory(prefix=project) as temp:
        root = Path(temp)
        public = root/"events"
        private = root/"private-design"
        author = root/"private-author"
        registry = root/"registry"
        for folder in (public/"plugin.design",public/"plugin.author",private,author,registry):
            folder.mkdir(parents=True)
        sentinel = {"id":"test.device_witness","description":"Read the hardware fixture counter",
                    "version":"1","parameters":{},"effect":"read","endpoint":"http://fixture:8131/device"}
        (registry/"device.json").write_text(json.dumps(sentinel),encoding="utf-8")
        original_registry = {p.name:p.read_bytes() for p in registry.iterdir()}
        common = {"image":args.image,"pull_policy":"never"}
        ros = {"ROS_LOCALHOST_ONLY":"1","EDGE_OLLAMA_URL":"","EDGE_PLANNER_URL":"","EDGE_EXTRA_PLUGIN_DIR":""}
        watch_cmd = ["--url","http://console:8080","--decisions","/author/decisions","--state","/private",
                     "--journal","/events/module-events.sqlite","--contracts-dir","/contracts","--interval",".2"]
        services = {
            "agent":{**common,"command":["python3","/opt/edge/agent/service.py"],"environment":ros,
                     "volumes":["tasks:/var/lib/edge-tasks",mounted(registry,"/etc/edge/plugins")]},
            "console":{**common,"command":["python3","/opt/edge/console/server/main.py"],
                       "environment":{**ros,"EDGE_CONSOLE_DIR":"/tmp/fixture-history"},"ports":["127.0.0.1::8080"],
                       "volumes":[mounted(public,"/var/lib/edge-module-sources")]},
            "fixture":{**common,"entrypoint":["python3","/opt/edge/tests/system/plugin_design_acceptance.py","--serve-fixture"],
                       "ports":["127.0.0.1::8131"]},
            "author":{**common,"entrypoint":["python3","/opt/edge/client/plugin_author_watch.py"],
                      "environment":{"EDGE_AUTHOR_EVENT_DB":"/events/module-events.sqlite"},
                      "command":["--url","http://console:8080","--ollama-url","http://fixture:8131/author",
                                 "--model","odometry-fixture","--out-dir","/author"],
                      "volumes":[mounted(public/"plugin.author","/events",read_only=False),mounted(author,"/author",read_only=False)]},
            "design":{**common,"entrypoint":["python3","/opt/edge/client/plugin_design_watch.py"],"command":watch_cmd,
                      "read_only":True,"cap_drop":["ALL"],"tmpfs":["/tmp"],
                      "volumes":[mounted(public/"plugin.design","/events",read_only=False),mounted(author,"/author"),
                                 mounted(private,"/private",read_only=False),mounted(ROOT/"contracts/design","/contracts")]}}
        volumes = {"tasks":{}}
        if args.cache:
            watch_cmd.extend(["--ollama-url","http://fixture:8131/design","--model",args.model])
            services["model"] = {"image":MODEL_IMAGE,"pull_policy":"never","ports":["127.0.0.1::11434"],
                                 "volumes":["cache:/cache:ro","model-home:/root/.ollama"]}
            volumes.update(cache={"external":True,"name":args.cache}, **{"model-home":{}})
        stack = root/"compose.json"
        stack.write_text(json.dumps({"services":services,"volumes":volumes}),encoding="utf-8")
        compose = ["docker","compose","-p",project,"-f",str(stack)]
        def invoke(*cmd):
            return command(*compose,*cmd)
        def url(service,port):
            return "http://"+invoke("port",service,port)
        def archived(kind=None, source=None):
            query = {"task_id":"png-dimensions","limit":100}
            if kind:
                query["kind"] = kind
            if source:
                query["source"] = source
            return read(base+"/api/events?"+urlencode(query))
        def one(kind,source):
            rows=archived(kind,source)["events"]
            check(len(rows)<=1,"duplicate "+kind+" before CLI control")
            return rows[0] if rows else None
        def counts():
            return read(fixture_url+"/counts")
        source = "journal:plugin.design"
        try:
            if args.cache:
                invoke("run","--no-deps","--rm","-T","--entrypoint","/bin/sh","model","-ec",
                       "test -d /cache/models/blobs && mkdir -p /root/.ollama/models && cp -a /cache/models/. /root/.ollama/models/")
            invoke("up","--no-build","-d")
            base,fixture_url = url("console","8080"),url("fixture","8131")
            if args.cache:
                ollama_url=url("model","11434")
                tags=wait_for(lambda:read(ollama_url+"/api/tags"),"Ollama tags")
                report["model_digest"]=next(t["digest"] for t in tags["models"] if t["name"]==args.model)
            wait_for(lambda:read(base+"/agent/capabilities").get("capabilities"),"Agent/Console")
            read(base+"/agent/tasks",{"id":"png-dimensions","text":GOAL})
            completed=wait_for(lambda:one("plugin.design_watch.completed",source),"automatic design delivery",140)
            blocked=one("agent.blocked","mission_agent/tasks")
            event=one("plugin.author.declined","journal:plugin.author")
            finished=one("plugin.design.drafted",source)
            requested=next(r for r in archived("plugin.design.requested",source)["events"]
                           if r["event_id"]==finished["parent_event_id"])
            chain=[blocked,event,requested,finished,completed]
            check(all(child["parent_event_id"]==parent["event_id"] and child["trace_id"]==parent["trace_id"]
                      for parent,child in zip(chain,chain[1:])), "causal link missing")
            draft_path=private/"drafts"/(finished["data"]["design_id"]+".json")
            draft=json.loads(draft_path.read_bytes())
            digest=hashlib.sha256(draft_path.read_bytes()).hexdigest()
            contract_sha=snapshot_digest(draft["interfaces"])
            for path in (ROOT/"contracts/design").glob("*.json"):
                supplied=json.loads(path.read_bytes())
                check(draft["interfaces"].get(supplied["id"])==supplied,"wrong public interface "+path.name)
            check(draft["goal"]==GOAL and digest==finished["data"]["draft_sha256"]==completed["data"]["draft_sha256"],
                  "draft source or bytes mismatch")
            check(contract_sha==draft["contracts_sha256"]==finished["data"]["contracts_sha256"] and
                  len(draft["interfaces"])==finished["data"]["interface_count"],"contract snapshot mismatch")
            initial=counts()
            attempts=completed["data"]["attempt"]
            check(1<=attempts<=3 and initial["device_calls"]==0 and initial["author_calls"]==1,
                  "unexpected work budget, author or device call counts")
            if args.cache:
                check(1<=initial["design_calls"]<=attempts and initial["model_contracts_sha256"]==contract_sha,
                      "model budget or witnessed contracts mismatch")
            else:
                check(initial["design_calls"]==0,"offline design contacted a model")
            check(GOAL not in json.dumps(archived(source=source)["events"]),"private goal in published events")
            # Lose only the disposable Console archive. The Agent, author facts and design work survive.
            first_archive=archived()["archive_id"]
            invoke("stop","-t","2","author","design")
            invoke("up","--no-deps","--force-recreate","-d","console")
            base=url("console","8080")
            restored=wait_for(lambda:one("plugin.author.declined","journal:plugin.author"),"reimported refusal")
            second_archive=archived()["archive_id"]
            check(restored["event_id"]==event["event_id"] and first_archive!=second_archive,"archive replay missing")
            invoke("start","design")
            def replayed():
                db=sqlite3.connect((private/"intake.sqlite").as_uri()+"?mode=ro",uri=True,timeout=2)
                try:
                    row=db.execute("SELECT max(cursor) FROM checkpoints WHERE archive_id=?",(second_archive,)).fetchone()
                    if not row or row[0] is None or row[0]<restored["seq"]:
                        return False
                    return db.execute("SELECT count(*),min(state),sum(attempts) FROM design_jobs").fetchone()
                finally:
                    db.close()
            job=wait_for(replayed,"design watcher durable checkpoint after replay")
            invoke("stop","-t","2","design")
            # Drain once after the observed checkpoint so delayed intake also has an opportunity to run.
            invoke("run","--rm","--no-deps","-T","design",*watch_cmd,"--once")
            replay=wait_for(lambda:one("plugin.design_watch.completed",source),"reimported completion")
            check(job==(1,"completed",attempts) and replayed()==job and
                  replay["event_id"]==completed["event_id"] and counts()==initial
                  and len(list((private/"drafts").glob("*.json")))==1,"redelivery repeated work")
            original=read(base+"/agent/tasks?id=png-dimensions")
            check(original["state"]=="BLOCKED" and original["steps"]==[] and
                  original_registry=={p.name:p.read_bytes() for p in registry.iterdir()},"unexpected dispatch or release")
            # Exercise the existing explicit SDD-013 CLI independently after ending the autonomous experiment.
            event_path=root/"event.json"
            event_path.write_text(json.dumps(event),encoding="utf-8")
            cli=[sys.executable,str(ROOT/"client/plugin_design.py"),"--event",str(event_path),"--decision",
                 str(author/"decisions"/(event["data"]["receipt_id"]+".json")),"--out-dir",str(root/"cli-designs"),
                 "--journal",str(public/"plugin.design"/"module-events.sqlite")]
            if args.cache:
                cli.extend(["--ollama-url",ollama_url,"--model",args.model])
            result=json.loads(command(*cli))
            cli_path=Path(result["path"])
            cli_done=wait_for(lambda:next((r for r in archived("plugin.design.drafted",source)["events"]
                                         if r["event_id"]==result["event_id"]),None),"CLI control archived")
            check(json.loads(cli_path.read_bytes())["goal"]==GOAL and
                  cli_done["data"]["draft_sha256"]==hashlib.sha256(cli_path.read_bytes()).hexdigest(),"CLI draft mismatch")
            report.update(passed=True,source_sha256=hashlib.sha256((ROOT/"client/plugin_design.py").read_bytes()).hexdigest(),
                          image_id=command("docker","image","inspect","--format","{{.Id}}",args.image),state=original["state"],
                          steps=len(original["steps"]),synthetic_test_draft=draft,
                          automatic={"requests":1,"before_archive":first_archive,"after_archive":second_archive,
                                     "delivered_event":completed["event_id"],"redelivered_source_event":restored["event_id"],
                                     "model_and_device_witness":initial,"jobs":job[0],"attempts":job[2],
                                     "contract_count":len(draft["interfaces"]),"contracts_sha256":contract_sha},
                          cli_control={"event_id":result["event_id"],"draft_sha256":result["draft_sha256"]},
                          events=[{k:row[k] for k in ("seq","event_id","parent_event_id","trace_id","kind","source","data")}
                                  for row in chain])
        except Exception as exc:
            report["error"]=str(exc)
            report["service_logs"]=invoke("logs","--tail","12","design","fixture","author")[-2600:]
            raise
        finally:
            try:
                invoke("down","--volumes","--remove-orphans","--timeout","2")
            finally:
                args.out.parent.mkdir(parents=True,exist_ok=True)
                args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",default="edge-agent-ros:v0.1")
    parser.add_argument("--cache",help="existing read-only Ollama cache for a real model proposal")
    parser.add_argument("--model",default="qwen2.5:1.5b")
    parser.add_argument("--out",type=Path)
    parser.add_argument("--serve-fixture",action="store_true")
    args=parser.parse_args()
    if args.serve_fixture:
        with ThreadingHTTPServer(("0.0.0.0",8131),Fixture) as server:
            server.guard=threading.Lock()
            server.counts={"author_calls":0,"design_calls":0,"device_calls":0}
            server.serve_forever()
    elif args.out:
        run(args)
    else:
        parser.error("provide --out to run the experiment")
