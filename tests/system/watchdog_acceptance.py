"""SDD-015. Observe an independent Watchdog across real Agent and Console outages."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from threading import Thread
from urllib.parse import urlencode, urlsplit
import uuid

from lifecycle_acceptance import check, wait_for
from runtime_plugin_acceptance import HEARTBEAT, mounted, publish


class Witness(BaseHTTPRequestHandler):
    """Log each watchdog request independently, forward reads to the real service."""
    def log_message(self, *_):
        pass

    def handle_probe(self):
        target, port = self.server.upstream
        path = urlsplit(self.path).path
        with Path("/audit/watch.jsonl").open("a") as output:
            print(json.dumps([target, self.command, path]), file=output)
        if self.command != "GET":
            self.send_error(405)
            return
        upstream = HTTPConnection(target, port, timeout=3)
        try:
            upstream.request("GET", self.path)
            response = upstream.getresponse()
            status, data = response.status, response.read(4_000_001)
        except (HTTPException, OSError):
            status, data = 503, b"{}"
        finally:
            upstream.close()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_probe


def serve():
    servers = []
    for target, port in (("agent",8090),("console",8080)):
        server = ThreadingHTTPServer(("0.0.0.0",port), Witness)
        server.upstream = target, port
        servers.append(server)
    Thread(target=servers[0].serve_forever, daemon=True).start()
    servers[1].serve_forever()


def read_source(path):
    with sqlite3.connect(Path(path).absolute().as_uri()+"?mode=ro",uri=True) as db:
        rows = [json.loads(raw) for (raw,) in db.execute("SELECT event FROM module_bus_events ORDER BY seq")]
    print(json.dumps(rows))


def read_mirror(path):
    with sqlite3.connect(Path(path).absolute().as_uri()+"?mode=ro",uri=True) as db:
        db.row_factory = sqlite3.Row
        cursor = db.execute("SELECT archive_id,seq FROM watchdog_cursor WHERE singleton=1").fetchone()
        rows = db.execute("SELECT archive_id,seq,event_id,task_id,trace_id,code FROM watchdog_events ORDER BY seq")
        print(json.dumps({"cursor":dict(cursor) if cursor else None,"events":[dict(row) for row in rows]}))


HTTP_CLIENT = """
import json,sys
from urllib.request import Request,urlopen
from urllib.error import URLError
url,body=json.load(sys.stdin)
req=Request(url,data=json.dumps(body).encode() if body else None,headers={'Content-Type':'application/json'})
try:
    with urlopen(req,timeout=3) as rsp: print(json.dumps(json.load(rsp)))
except URLError as exc: print(json.dumps({'probe_error':str(exc)}))
"""


def run(image, out):
    project = "edge-watchdog-" + uuid.uuid4().hex[:12]
    env = {k:v for k,v in os.environ.items() if not k.startswith(("EDGE_", "COMPOSE_"))}
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    report = {"passed":False, "project":project, "utc":datetime.now(timezone.utc).isoformat(),
              "scope":"private Docker network, ROS Agent, Console, independent Watchdog, read Tool"}
    with tempfile.TemporaryDirectory(prefix=project) as temp:
        root = Path(temp)
        registry, audit = root/"plugins", root/"audit"
        registry.mkdir()
        audit.mkdir()
        publish(registry/"heartbeat.json", {"id":"test_heartbeat","version":"1","effect":"read","parameters":{},
                "description": HEARTBEAT, "endpoint":"http://tool:8096/heartbeat"})
        common = {"image":image, "pull_policy":"never", "cap_drop":["ALL"],
                  "security_opt":["no-new-privileges:true"]}
        test = "/opt/edge/tests/system/watchdog_acceptance.py"
        db = "/state/watchdog/module-events.sqlite"
        services = {
            "agent": {**common,"command":["python3","/opt/edge/agent/service.py"],"environment":{
                "EDGE_EXTRA_PLUGIN_DIR":"","EDGE_OLLAMA_URL":"","EDGE_PLANNER_URL":"","ROS_LOCALHOST_ONLY":"1"},
                "volumes":[mounted(registry,"/etc/edge/plugins"), "tasks:/var/lib/edge-tasks"]},
            "console": {**common,"command":["python3","/opt/edge/console/server/main.py"],
                "environment":{"ROS_LOCALHOST_ONLY":"1"}, "volumes":["archive:/var/lib/edge-console",
                    "watch:/var/lib/edge-module-sources/watchdog:ro"]},
            "watchdog": {**common,"read_only":True,"entrypoint":["python3","-B","/opt/edge/client/watchdog.py"],
                "command":["--agent-url","http://witness:8090","--console-url","http://witness:8080",
                           "--journal",db,"--threshold","2","--interval","1","--timeout","1",
                           "--grace","1","--attempts","2","--recheck","5"],
                "volumes":["watch:/state/watchdog"]},
            "tool": {**common,"entrypoint":["python3","/opt/edge/tests/system/runtime_plugin_acceptance.py",
                     "--serve-fixture"],"volumes":[mounted(audit,"/audit",read_only=False)]},
            "witness": {**common,"entrypoint":["python3","-B",test,"--serve-witness"],"read_only":True,
                "volumes":[mounted(audit,"/audit",read_only=False),"watch:/state/watchdog:ro"]},
        }
        config = root/"stack.json"
        publish(config, {"services":services, "volumes":{v:{} for v in ("tasks","watch","archive")},
                         "networks":{"default":{"internal":True}}})
        def compose(*args, stdin=None):
            result = subprocess.run(["docker","compose","-p",project,"-f",str(config),*args],input=stdin,
                                    capture_output=True,encoding="utf-8",errors="replace",env=env,timeout=90)
            if result.returncode:
                raise RuntimeError(result.stderr[-1400:] or result.stdout[-800:])
            return result.stdout.strip()

        def api(service, path, body=None):
            port = 8090 if service == "agent" else 8080
            data = json.loads(compose("exec","-T","witness","python3","-c",HTTP_CLIENT,
                                      stdin=json.dumps([f"http://{service}:{port}"+path,body])))
            if "probe_error" in data:
                raise ConnectionError(data["probe_error"])
            return data

        def source():
            return json.loads(compose("exec","-T","witness","python3",test,"--read-source",db))

        def mirror():
            return json.loads(compose("exec","-T","witness","python3",test,"--read-mirror",db))

        def state(rows, target, kind):
            return [r for r in rows if r.get("data",{}).get("target") == target and r["kind"] == "watchdog."+kind]

        def wait_state(target, kind):
            return wait_for(lambda:state(source(), target, kind), f"{target}/{kind}", 75)

        def archive():
            return api("console","/api/events?"+urlencode({"module":"watchdog","limit":200,"after":0}))["events"]

        def task(key):
            return api("agent","/agent/tasks?"+urlencode({"id":key}))

        def run_control(key):
            api("agent","/agent/tasks",{"text":HEARTBEAT,"id":key})
            return wait_for(lambda: (t if (t:=task(key))["state"]=="SUCCEEDED" else None), key)

        def witness_reads():
            path = audit/"watch.jsonl"
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

        try:
            info = json.loads(subprocess.check_output(["docker","image","inspect",image]))[0]
            report.update(image_id=info["Id"], revision=(info["Config"].get("Labels") or {}).get("org.opencontainers.image.revision"))
            compose("up","-d","agent","console","tool","witness")
            wait_for(lambda:api("agent","/agent/capabilities"), "Agent startup")
            wait_for(lambda:api("console","/api/event-stats"), "Console startup")
            first = run_control("control-before")
            compose("up","-d","watchdog")
            wd = compose("ps","-q","watchdog")
            conf = json.loads(subprocess.check_output(["docker","inspect",wd]))[0]
            mounts = [m["Destination"] for m in conf["Mounts"]]
            check(mounts == ["/state/watchdog"] and conf["HostConfig"]["ReadonlyRootfs"]
                  and not conf["HostConfig"]["Privileged"] and "ALL" in conf["HostConfig"]["CapDrop"],
                  "unexpected Watchdog privileges")
            wait_for(lambda:len(witness_reads()) >= 8,"normal Watchdog probe cycles")
            check(not state(source(),"agent","detected") and not state(source(),"console","detected"), "false initial incident")
            initial_cursor = wait_for(lambda:mirror()["cursor"],"initial archive checkpoint")
            compose("stop","-t","1","console")
            wait_state("console","detected")
            middle = run_control("control-without-console")
            offline_id = "unserved-during-console-outage"
            api("agent","/agent/tasks",{"id":offline_id,"text":"绘制当前区域的磁场强度分布"})
            blocked = wait_for(lambda:(t if (t:=task(offline_id))["state"]=="BLOCKED" else None),offline_id)
            check(not blocked["steps"],"unserved software request dispatched an action")
            compose("stop","-t","1","agent")
            wait_state("agent","detected")
            for target in ("agent","console"):
                wait_state(target,"intervention_required")
            before = source()
            compose("restart","-t","1","watchdog")
            baseline = len(witness_reads())
            wait_for(lambda:len(witness_reads()) >= baseline+6,"persisted incident after Watchdog restart")
            for target in ("agent","console"):
                for kind in ("detected","action_requested","intervention_required"):
                    check(len(state(before,target,kind)) == len(state(source(),target,kind)) == 1,
                          f"duplicate or missing {target}/{kind} across restart")
            compose("start","agent")
            wait_state("agent","recovered")
            check(not state(source(),"console","recovered"),"Console recovered while its process was stopped")
            compose("stop","-t","2","watchdog")
            compose("start","console")
            def imported_fault():
                page = api("console","/api/events?"+urlencode({"task_id":offline_id,"kind":"agent.blocked"}))
                return next((r for r in page["events"] if r["source"]=="mission_agent/tasks"), None)
            archived_fault = wait_for(imported_fault, "Console did not import the missed task failure")
            check(not any(row["task_id"]==offline_id for row in mirror()["events"]), "stopped monitor mirrored the event")
            compose("start","watchdog")
            wait_state("console","recovered")
            def repaired_mirror():
                snapshot = mirror()
                return snapshot if any(row["event_id"]==archived_fault["event_id"] for row in snapshot["events"]) else None
            repaired = wait_for(repaired_mirror,"Watchdog did not repair its missing local evidence")
            copies = [r for r in repaired["events"] if r["event_id"]==archived_fault["event_id"]]
            check(len(copies)==1 and copies[0]["task_id"]==offline_id and copies[0]["seq"]==archived_fault["seq"]
                  and copies[0]["trace_id"]==blocked["trace_id"], "local evidence differs from authoritative fault")
            check(initial_cursor["archive_id"]==copies[0]["archive_id"]==repaired["cursor"]["archive_id"]
                  and initial_cursor["seq"]<copies[0]["seq"]<=repaired["cursor"]["seq"], "checkpoint did not cover outage")
            repair = state(source(),"console","action_confirmed")[0]["data"]["repair"]
            check(repair["archive_id"]==copies[0]["archive_id"] and repair["rows_copied"]>=1 and
                  repair["from_seq"]<copies[0]["seq"]<=repair["through_seq"],
                  "recovery receipt omitted the repaired source interval")
            report["mirror_repair"]={"before":initial_cursor,"after":repaired["cursor"],"repaired_source_event":copies[0],
                                     "receipt":repair,"missing_before_restart":True,
                                     "proof_source":"Console public archive and Watchdog SQLite"}
            final = source()
            ids = {row["event_id"] for row in final}
            def complete_archive():
                records = archive()
                return records if ids <= {row["event_id"] for row in records} else None
            archived = wait_for(complete_archive,"offline source facts did not reach Console",60)
            report["incidents"] = {}
            for target in ("console","agent"):
                trace = state(final,target,"detected")[0]["trace_id"]
                rows = [row for row in archived if row["trace_id"] == trace]
                kinds = Counter(row["kind"] for row in rows)
                for name in ("detected","action_requested","action_confirmed","intervention_required","recovered"):
                    check(kinds["watchdog."+name] == 1, f"expected exactly one {target}/{name}")
                operations = {r["operation_id"] for r in rows if r.get("operation_id")}
                check(len(operations)==1,"expected one logical reconciliation per incident")
                check(all(r["source"]=="journal:watchdog" for r in rows),"wrong producer")
                for row in rows:
                    if row.get("parent_event_id"):
                        check(any(r["event_id"] == row["parent_event_id"] and r["seq"] < row["seq"] for r in archived),
                              "archived cause cannot be located")
                action = state(final,target,"intervention_required")[0]["data"]
                check(action.get("reason") and action.get("steps"), "operator instructions missing")
                report["incidents"][target] = {"trace_id":trace,"operation_id":next(iter(operations)),
                    "counts":dict(kinds),"human_reason":action["reason"],"steps":action["steps"],
                    "events":[{k:r.get(k) for k in ("seq","kind","event_id","parent_event_id","source_seq")} for r in rows]}
            check(report["incidents"]["agent"]["trace_id"]!=report["incidents"]["console"]["trace_id"], "target traces merged")
            compose("rm","--stop","--force","watchdog")
            check(task(first["id"])["steps"]==first["steps"] and task(middle["id"])["steps"]==middle["steps"],
                  "Watchdog changed a completed task")
            last = run_control("control-after-unload")
            calls = [json.loads(line) for line in (audit/"requests.jsonl").read_text().splitlines()]
            check([row["operation_id"] for row in calls] == [t["id"]+"/step/1" for t in (first,middle,last)],
                  "unexpected controlled tool re-execution")
            reads = Counter(tuple(r) for r in witness_reads())
            allowed = {("agent","GET","/agent/capabilities"),("agent","GET","/agent/plugins"),
                       ("console","GET","/api/event-stats"),("console","GET","/api/history"),
                       ("console","GET","/api/events")}
            check(set(reads) == allowed, f"unexpected Watchdog requests: {reads}")
            report.update(passed=True, independent_tool_calls=calls, readonly_mount_root=True,
                          request_counts={"/".join(k):v for k,v in reads.items()}, archived_source_rows=len(ids))
        except Exception as exc:
            report.update(error=str(exc), service_logs=compose("logs","--no-color","--tail","10"))
            raise
        finally:
            try:
                compose("down","--volumes","--remove-orphans","--timeout","2")
            finally:
                out.parent.mkdir(parents=True,exist_ok=True)
                out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-witness",action="store_true")
    parser.add_argument("--read-source")
    parser.add_argument("--read-mirror")
    parser.add_argument("--image",default="edge-agent-ros:v0.1")
    parser.add_argument("--out",type=Path)
    options = parser.parse_args()
    if options.serve_witness:
        serve()
    elif options.read_source:
        read_source(options.read_source)
    elif options.read_mirror:
        read_mirror(options.read_mirror)
    elif options.out:
        run(options.image, options.out)
    else:
        parser.error("--out is required")
