"""Exercise restart, resource contention and plugin retries in a private Compose stack.

Run from a Docker host with Python 3: python tests/console_resilience.py
The only published port is an ephemeral loopback port. The default deployment and
its mounted plugin directory are never modified.
"""
import argparse
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


def serve_fixture():
    """A read-only, instrumented adapter that fails the first call for each ID."""
    calls = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status, data):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            op = request["operation_id"]
            with lock:
                status = 200 if any(row["id"] == op for row in calls) else 503
                calls.append({"id":op,"http":status,"utc":datetime.now(timezone.utc).isoformat()})
            self.send_json(status,{"operation_id":op,"state":"SUCCEEDED","evidence":{"value":17}}
                           if status == 200 else {"detail":"injected temporary read failure"})

        def do_GET(self):
            self.send_json(200,calls)

    ThreadingHTTPServer(("0.0.0.0",8123),Handler).serve_forever()


def wait_for(f, seconds, message):
    deadline=time.monotonic()+seconds
    last=None
    while time.monotonic() < deadline:
        try:
            result=f()
            if result:
                return result
        except (HTTPError,URLError,TimeoutError,ConnectionError) as error:
            last=error
        time.sleep(0.2)
    raise RuntimeError(f"{message}; last transport error={last}")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run():
    repo=Path(__file__).resolve().parents[1]
    tag=uuid.uuid4().hex[:10]
    project="edge-console-resilience-"+tag
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    env={**os.environ,"EDGE_CONSOLE_PORT":str(port),"EDGE_ROS_DOMAIN_ID":"113","EDGE_OLLAMA_URL":"",
         "EDGE_PLANNER_URL":"","EDGE_RETRY_LIMIT":"7"}
    image=env.get("EDGE_IMAGE") or "edge-agent-ros:v0.1"
    url=f"http://127.0.0.1:{port}"
    # Proxy settings on a developer machine must not capture loopback requests.
    http=build_opener(ProxyHandler({}))
    report={"project":project,"port":port,"image":image,"utc":datetime.now(timezone.utc).isoformat(),
            "passed":False,"cases":{}}
    output=Path(os.getenv("EDGE_RESILIENCE_REPORT") or repo/".ci-results/resilience.json")
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        directory=Path(temp)
        plugin_dir=directory/"plugins"
        plugin_dir.mkdir()
        override=directory/"override.yaml"
        override.write_text(json.dumps({"services":{
            "agent":{"volumes":[{"type":"bind","source":str(plugin_dir),"target":"/etc/edge/plugins","read_only":True}]},
            "plugin-fixture":{"image":image,"pull_policy":"never","command":["python3","/fixture.py","--serve-fixture"],
                              "volumes":[{"type":"bind","source":str(Path(__file__).resolve()),
                                          "target":"/fixture.py","read_only":True}]}}}),encoding="utf-8")
        base=["docker","compose","-p",project,"-f",str(repo/"compose.yaml"),"-f",str(override)]

        def compose(*args, timeout=60):
            result=subprocess.run([*base,*args],cwd=repo,env=env,capture_output=True,
                                  timeout=timeout,encoding="utf-8",errors="replace")
            if result.returncode:
                raise RuntimeError(f"compose {args}: {result.stderr[-1500:]}")
            return result.stdout.strip()

        def api(path, body=None):
            data=None if body is None else json.dumps(body).encode()
            request=Request(url+path,data=data,headers={"Content-Type":"application/json"} if data else {})
            with http.open(request,timeout=4) as response:
                return json.load(response)

        def task(key):
            return api("/agent/tasks?"+urlencode({"id":key}))

        def submit(key,text):
            item=api("/agent/tasks",{"id":key,"text":text})
            require(item["id"]==key,f"failed admission {key}")

        def terminal(key,seconds=65):
            def check():
                item=task(key)
                return item if item["state"] in ("SUCCEEDED","BLOCKED","RECOVERY_REQUIRED") else None
            return wait_for(check,seconds,"missing terminal state for "+key)

        def events(key):
            return api("/api/events?"+urlencode({"task_id":key,"after":0,"limit":400}))["events"]

        def moving():
            state=api("/api/state")
            truth=state["sources"]["truth"]
            return state if truth["fresh"] and abs(truth["data"]["v"])>0.12 and any(
                m["mission_id"]=="first/step/1" and m["state"]=="RUNNING" for m in state["missions"]) else None

        try:
            compose("up","-d","--no-build","sim","bridge","witness","core","gateway","agent","console","plugin-fixture")
            wait_for(lambda: api("/api/state")["sources"]["truth"]["fresh"] and api("/agent/capabilities"),
                     60,"initial ROS or agent readiness")
            submit("first","inspect (2.6,0)")
            in_motion=wait_for(moving,30,"no independent Gazebo evidence of active first task")
            submit("second","inspect (1.4,0)")
            wait_for(lambda:any(e["kind"]=="retry" and "occupied" in e["detail"]
                                for e in task("second")["events"]),14,"did not exercise resource contention")
            state=moving()
            require(state,"robot already stopped before agent kill")
            before_id=compose("ps","-q","agent")
            started_before=subprocess.check_output(["docker","inspect","--format","{{.State.StartedAt}}",before_id],text=True).strip()
            compose("kill","-s","SIGKILL","agent")
            compose("start","agent")
            started_after=subprocess.check_output(["docker","inspect","--format","{{.State.StartedAt}}",before_id],text=True).strip()
            require(started_before!=started_after,"agent restart not observed")
            first,second=terminal("first"),terminal("second")
            require(first["state"]=="SUCCEEDED" and second["state"]=="SUCCEEDED",f"tasks {first['state']}, {second['state']}")
            wait_for(lambda: {"agent.verified","mission.transition"} <= {e["kind"] for e in events("second")},
                     10,"complete archived trace")
            first_rows, second_rows=events("first"),events("second")
            action="first/step/1/waypoint/1"
            gateway=compose("logs","--no-color","gateway")
            starts=[line for line in gateway.splitlines() if f"Tracking {action} to " in line]
            for kind in ("agent.dispatch","mission.created","step.dispatch","step.verified","agent.verified"):
                require(sum(e["kind"]==kind for e in first_rows)==1,"incorrect dispatch/verification count: "+kind)
            require(len(starts)==1,"gateway executed a duplicate goal after recovery")
            evidence=first["steps"][0]["evidence"]
            require(evidence["verified_count"]==1,"missing recovered step evidence")
            retries=[e for e in second_rows if e["kind"]=="agent.retry" and "occupied" in e["detail"]]
            require(retries and all(e["operation_id"]=="second/step/1" for e in retries),"new operation ID in retry")
            first_end=next(e for e in first_rows if e["kind"]=="mission.transition" and e["data"]["state"]=="SUCCEEDED")
            second_start=next(e for e in second_rows if e["kind"]=="mission.created")
            require(first_end["source_seq"] < second_start["source_seq"],"shared base ownership overlapped")
            report["cases"]["restart"]={"passed":True,"operation_id":action,"gateway_starts":starts,
                 "agent_started_before":started_before,"agent_started_after":started_after,
                 "truth_at_kill":state["sources"]["truth"]["data"],"result":evidence,"events":first_rows}
            report["cases"]["contention"]={"passed":True,"owner_release_seq":first_end["source_seq"],
                "next_owner_seq":second_start["source_seq"],"retries":len(retries),"events":second_rows,
                "first_observed_world_pose":in_motion["sources"]["truth"]["data"]}
            manifest={"id":"read_fixture","version":"1","effect":"read","description":"read fixture",
                      "endpoint":"http://plugin-fixture:8123/read","parameters":{}}
            target=plugin_dir/"fixture.json"
            staging=plugin_dir/"fixture.tmp"
            staging.write_text(json.dumps(manifest),encoding="utf-8")
            staging.replace(target)
            catalog=lambda: [c["id"] for c in api("/agent/capabilities")["capabilities"]]
            wait_for(lambda: "read_fixture" in catalog(),10,"manifest failed to mount")
            submit("plugin","read_fixture")
            plugin=terminal("plugin")
            require(plugin["state"]=="SUCCEEDED" and plugin["steps"][0]["evidence"]=={"value":17},"wrong plugin evidence")
            calls=json.loads(compose("exec","-T","plugin-fixture","python3","-c",
                         "import urllib.request;print(urllib.request.urlopen('http://localhost:8123/calls').read().decode())"))
            require([c["http"] for c in calls]==[503,200] and {c["id"] for c in calls}=={"plugin/step/1"},
                    "adapter retries lost identity")
            require(any(e["kind"]=="retry" and "503" in e["detail"] for e in plugin["events"]),"missing HTTP 503 cause")
            # Exercise manual continuation after exhausting a deliberately short budget.
            env["EDGE_RETRY_LIMIT"]="0"
            compose("up","-d","--no-deps","--no-build","agent")
            wait_for(lambda:api("/agent/capabilities"),20,"agent configuration was not reloaded")
            submit("repair","read_fixture")
            paused=terminal("repair")
            require(paused["can_retry"] and paused["state"]=="RECOVERY_REQUIRED","failure not retained for repair")
            api("/agent/retry",{"id":"repair"})
            resumed=terminal("repair")
            require(resumed["state"]=="SUCCEEDED" and resumed["steps"][0]["operation_id"]=="repair/step/1",
                    "manual retry lost original operation")
            report["cases"]["operator_resume"]={"passed":True,"paused_state":paused["state"],
                "events":resumed["events"],"state":resumed["state"]}
            target.unlink()
            wait_for(lambda:"read_fixture" not in catalog(),10,"removed plugin still advertised")
            submit("unmounted","read_fixture")
            detached=terminal("unmounted",12)
            require(detached["state"]=="BLOCKED","unavailable local capability dispatched")
            report["cases"]["hotplug"]={"passed":True,"manifest":manifest,"calls":calls,"events":plugin["events"],
                                       "unmounted_state":detached["state"],"unmounted_catalog":catalog()}
            report["passed"]=True
        except Exception as ex:
            report["error"]=f"{type(ex).__name__}: {ex}"
            raise
        finally:
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            compose("down","--volumes","--remove-orphans","--timeout","3")
    print(f"PASS: restart, contention, hotplug, stable retry operation IDs. {output}")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--serve-fixture",action="store_true")
    if parser.parse_args().serve_fixture:
        serve_fixture()
    else:
        run()
