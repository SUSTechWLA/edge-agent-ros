"""SDD-007: trace a blocked task, a proposed tool, its release and a new ROS task via public APIs."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from urllib.parse import urlencode

from derived_tool_acceptance import DEFAULT, PUBLISHER, fixtures, write_json
from lifecycle_acceptance import TERMINAL, check, wait_for
from runtime_plugin_acceptance import mounted


ROOT = Path(__file__).resolve().parents[2]
MODEL = """
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user = json.loads(body["messages"][-1]["content"])
        if (self.path != "/api/chat" or user.get("request") != %r
                or user.get("reason_code") not in ("planner.not_configured","planner.capability_missing")):
            self.send_error(400)
            return
        with Path("/audit/calls").open("a") as audit:
            audit.write("author-draft\\n")
        answer = {"field":"speed","unit":"km/h","op":"read","threshold":0}
        wire = json.dumps({"message":{"content":json.dumps(answer)}}).encode()
        self.send_response(200)
        self.send_header("Content-Length",str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)
HTTPServer(("0.0.0.0",11434),Handler).serve_forever()
""" % DEFAULT["description"]
CLIENT = """
import json,sys
from urllib.request import Request,urlopen
from urllib.error import URLError
path,body = json.load(sys.stdin)
req=Request("http://console:8080"+path, data=json.dumps(body).encode() if body else None,
            headers={"Content-Type":"application/json"})
try:
    with urlopen(req,timeout=4) as response:
        print(json.dumps(json.load(response)))
except URLError as exc:
    print(json.dumps({"api_error":str(exc)}))
"""


def run(image, out):
    project = "edge-author-bus-" + uuid.uuid4().hex[:12]
    info = json.loads(subprocess.check_output(["docker","image","inspect",image]))[0]
    report = {"passed":False, "project":project, "image":image, "image_id":info["Id"],
              "revision":info["Config"]["Labels"].get("org.opencontainers.image.revision"),
              "started_at":datetime.now(timezone.utc).isoformat(),
              "fixture":"internal Docker network, deterministic Ollama protocol reply and ROS 2 odometry"}
    out.parent.mkdir(parents=True,exist_ok=True)
    env = {k:v for k,v in os.environ.items() if not k.startswith(("EDGE_","COMPOSE_"))}
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    with tempfile.TemporaryDirectory(prefix="edge-author-bus-") as temp:
        folder = Path(temp)
        plugins, proposals, audit = folder/"plugins", folder/"private", folder/"audit"
        plugins.mkdir()
        proposals.mkdir()
        audit.mkdir()
        defaults = {"image":image,"pull_policy":"never"}
        # Docker's internal network need not carry multicast. Use explicit DDS peers
        # for the two sensor participants and keep discovery inside this test project.
        def ros(peer):
            return {"ROS_DOMAIN_ID":"153","RMW_IMPLEMENTATION":"rmw_cyclonedds_cpp",
                    "CYCLONEDDS_URI":'<CycloneDDS><Domain><General><AllowMulticast>false</AllowMulticast></General>'
                    '<Discovery><ParticipantIndex>auto</ParticipantIndex><Peers><Peer Address="'+peer+
                    '"/></Peers></Discovery></Domain></CycloneDDS>'}
        services = {
            "agent":{**defaults,"command":["python3","/opt/edge/agent/service.py","--ros-args","-r","/odom:=/fixture/odom"],
                     "environment":{**ros("odometry"),"EDGE_OLLAMA_URL":"","EDGE_PLANNER_URL":"","EDGE_EXTRA_PLUGIN_DIR":""},
                     "volumes":["tasks:/var/lib/edge-tasks",mounted(plugins,"/etc/edge/plugins")]},
            "console":{**defaults,"command":["python3","/opt/edge/console/server/main.py"],"environment":ros("agent"),
                       "volumes":["tasks:/var/lib/edge-tasks:ro","history:/var/lib/edge-console",
                                  "author-events:/var/lib/edge-author-events:ro",mounted(plugins,"/etc/edge/plugins")]},
            "author":{**defaults,"entrypoint":["python3","/opt/edge/client/plugin_author_watch.py"],
                      "command":["--once","--url","http://console:8080","--ollama-url","http://fixture:11434",
                                 "--out-dir","/proposals"],"read_only":True,"tmpfs":["/tmp"],"cap_drop":["ALL"],
                      "environment":{"EDGE_AUTHOR_EVENT_DB":"/events/module-events.sqlite"},
                      "volumes":["author-events:/events",mounted(proposals,"/proposals",read_only=False)]},
            "publisher":{**defaults,"entrypoint":["python3","/opt/edge/agent/author_tool.py"],
                         "volumes":[mounted(plugins,"/plugins",read_only=False),mounted(proposals,"/in")]},
            "fixture":{**defaults,"entrypoint":["python3","-c",MODEL],"volumes":[mounted(audit,"/audit",read_only=False)]},
            "odometry":{**defaults,"command":["python3","-c",PUBLISHER],"environment":ros("agent")}}
        stack = folder/"compose.json"
        write_json(stack, {"services":services,"volumes":{"tasks":{},"history":{},"author-events":{}},
                           "networks":{"default":{"internal":True}}})

        def compose(*args, stdin=None):
            result = subprocess.run(["docker","compose","-p",project,"-f",str(stack),*args], input=stdin,
                                    env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=90)
            check(result.returncode==0, result.stderr[-1800:] or result.stdout[-1800:])
            return result.stdout.strip()

        def api(path, body=None):
            doc = json.loads(compose("exec","-T","fixture","python3","-c",CLIENT,stdin=json.dumps([path,body])))
            if "api_error" in doc:
                raise ConnectionError(doc["api_error"])
            return doc

        def task(key):
            return api("/agent/tasks?"+urlencode({"id":key,"limit":500}))

        def terminal(key):
            return wait_for(lambda: doc if (doc:=task(key))["state"] in TERMINAL else None, key)

        def archived(**query):
            return wait_for(lambda: api("/api/events?"+urlencode(query))["events"],str(query))[0]

        def author():
            return json.loads(compose("run","--no-deps","--rm","-T","author"))

        try:
            compose("up","-d","--no-build","agent","console","fixture","odometry")
            wait_for(lambda: api("/agent/capabilities")["capabilities"],"Agent ready",60)
            text = DEFAULT["description"]
            blocked = terminal(api("/agent/tasks",{"id":"missing-speed","text":text})["id"])
            check(blocked["state"]=="BLOCKED" and blocked["steps"]==[], "original task unexpectedly dispatched")
            diagnosis = json.loads(blocked["events"][-1]["payload"])
            blocked_fact = archived(event_id=diagnosis["trace"]["event_id"],source="mission_agent/tasks")
            trace = blocked["trace_id"]
            check(blocked_fact["kind"]=="agent.blocked", "incorrect source event")
            check(author()["proposed"]==1, "watcher did not produce a candidate")
            proposal = archived(module="plugin.author",kind="plugin.author.proposed",trace_id=trace)
            private = list((proposals/"decisions").glob("*.json"))
            check(len(private)==1,"expected one decision")
            raw = private[0].read_bytes()
            definition = json.loads(raw)["definition"]
            check(proposal["parent_event_id"]==blocked_fact["event_id"] and proposal["cause_seq"]==blocked_fact["seq"]
                  and proposal["source"]=="journal:plugin.author" and proposal["task_id"]==blocked["id"]
                  and proposal["data"]["receipt_sha256"]==hashlib.sha256(raw).hexdigest(),
                  "proposal source or private receipt digest differs")
            check(not any(c["id"]==definition["id"] for c in api("/agent/capabilities")["capabilities"]),
                  "author activated its own candidate")
            check(not any(author().values()) and (audit/"calls").read_text().splitlines()==["author-draft"],
                  "watcher restart repeated work or issued another inference")
            write_json(proposals/"definition.json",definition)
            write_json(proposals/"cases.json",fixtures("km/h"))
            result = json.loads(compose("run","--no-deps","--rm","-T","publisher","publish","/in/definition.json",
                                "--cases","/in/cases.json","--registry","/plugins/derived","--expect","absent",
                                "--trace-id",trace,"--parent-event-id",proposal["event_id"]))
            applied = archived(event_id=result["event_id"],module="plugin.publisher")
            requested = archived(event_id=applied["parent_event_id"],module="plugin.publisher")
            check(requested["source"]==applied["source"]=="journal:plugin.publisher" and
                  requested["kind"]=="plugin.release.requested" and applied["kind"]=="plugin.release.applied" and
                  requested["trace_id"]==applied["trace_id"]==trace and requested["parent_event_id"]==proposal["event_id"]
                  and requested["cause_seq"]==proposal["seq"] and applied["cause_seq"]==requested["seq"]
                  and applied["data"]["change_id"]==result["change_id"] and applied["data"]["case_count"]==3,
                  "release provenance or test evidence differs")
            version = result["version"]
            check(proposal["data"]["candidate_version"]==applied["data"]["observed_version"]==version,
                  "candidate and installed release differ")
            trigger = {"kind":"agent","source":"author-bus-acceptance","id":"accept-speed","parent_task_id":blocked["id"],
                       "parent_event_id":applied["event_id"]}
            accepted = terminal(api("/agent/tasks",{"id":"verify-speed","text":text,"trigger":trigger})["id"])
            check(accepted["state"]=="SUCCEEDED" and len(accepted["steps"])==1 and accepted["trace_id"]==trace,
                  "new task did not verify this capability: "+str(accepted["detail"]))
            step = accepted["steps"][0]
            e = step["evidence"]
            check(step["tool"]==definition["id"] and step["version"]==e["definition_version"]==version and
                  e["source"]=="/fixture/odom" and e["frame"]=="odom" and e["stamp_ns"]>0 and
                  (e["raw_value"],e["raw_unit"],e["value"],e["unit"])==(-0.5,"m/s",-1.8,"km/h"),
                  "new task lacks the expected versioned ROS receipt")
            accepted_event = archived(event_id=json.loads(accepted["events"][0]["payload"])["trace"]["event_id"])
            check(accepted_event["parent_event_id"]==applied["event_id"] and accepted_event["cause_seq"]==applied["seq"],
                  "new task did not preserve its release trigger")
            original = task(blocked["id"])
            check(original["state"]=="BLOCKED" and not original["steps"],"release rewrote original task")
            report.update(passed=True, trace_id=trace, blocked_task=blocked["id"], new_task=accepted["id"],
                          tool=definition["id"], version=version, case_count=3, model_calls=1,
                          events=[{k:r[k] for k in ("seq","event_id","kind","source","parent_event_id","cause_seq")}
                                  for r in (blocked_fact,proposal,requested,applied,accepted_event)], receipt=e)
        except Exception as exc:
            report["failure"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            try:
                compose("down","--volumes","--remove-orphans","--timeout","2")
            finally:
                write_json(out,report)
                print(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",default="edge-agent-ros:v0.1")
    parser.add_argument("--out",type=Path,default=ROOT/".ci-results/system/author-bus.json")
    args = parser.parse_args()
    run(args.image,args.out)
