"""Run continuous read-only tasks and sensor outages in a fresh, isolated Gazebo deployment."""
import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = {"SUCCEEDED", "BLOCKED", "RECOVERY_REQUIRED", "CANCELED"}


def check(value, reason):
    if not value:
        raise AssertionError(reason)


def wait_for(call, label, seconds=40):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            value = call()
            if value:
                return value
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(.15)
    raise TimeoutError(label)


def exercise(image, count, out_root):
    tag = uuid.uuid4().hex[:12]
    directory = out_root.resolve() / tag
    directory.mkdir(parents=True)
    project = "edge-lifecycle-" + tag
    env = {k:v for k,v in os.environ.items() if not k.startswith(("EDGE_", "COMPOSE_"))}
    env.update(COMPOSE_DISABLE_ENV_FILE="1", EDGE_CONSOLE_PORT="0", EDGE_IMAGE=image,
               EDGE_ROS_DOMAIN_ID="143", EDGE_RETRY_LIMIT="3", PYTHONUTF8="1",
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    cmd = ["docker","compose","-p",project,"-f",str(ROOT/"compose.yaml")]
    info = json.loads(subprocess.check_output(["docker","image","inspect",image]))[0]
    report = {"passed":False, "started_at":datetime.now(timezone.utc).isoformat(),
              "project":project, "image_id":info["Id"], "revision":info["Config"]["Labels"]["org.opencontainers.image.revision"],
              "retry_limit":3, "tasks":[], "faults":{}, "model":"local deterministic planner"}
    console = build_opener(ProxyHandler({}))
    base = None
    watcher = None
    sink_path = directory/"events.db"
    original_requests = []
    world_samples = []
    start = time.monotonic()

    def compose(*args):
        result = subprocess.run([*cmd,*args],env=env,cwd=ROOT,capture_output=True,timeout=90,encoding="utf-8",errors="replace")
        with (directory/"compose.log").open("a",encoding="utf-8") as log:
            print(args,result.stdout,result.stderr,file=log)
        result.check_returncode()
        return result.stdout.strip()

    def api(path, body=None):
        req = Request(base+path, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type":"application/json"})
        with console.open(req,timeout=5) as response:
            return json.load(response)

    def submit(index, text):
        body = {"text":text, "trigger":{"kind":"user","source":"lifecycle-acceptance","id":tag+"-"+str(index)}}
        task_id = api("/agent/tasks",body)["id"]
        check(api("/agent/tasks",body)["id"]==task_id, "immediate replay created a new task")
        original_requests.append((body,task_id))
        return task_id

    def task(key):
        return api("/agent/tasks?"+urlencode({"id":key}))

    def done(key):
        def terminal():
            item = task(key)
            return item if item["state"] in TERMINAL else None
        return wait_for(terminal,"no terminal result for "+key)

    def verify(key, tool):
        item = done(key)
        check(item["state"]=="SUCCEEDED" and len(item["steps"])==1, "unexpected result "+str(item["detail"]))
        step = item["steps"][0]
        check(step["tool"]==tool and bool(step["evidence"]), "wrong or empty tool evidence")
        check(sum(row["kind"]=="verified" for row in item["events"])==1, "expected one verified event")
        if tool=="status":
            check(step["evidence"]["source"]=="/odom" and step["evidence"]["stamp_ns"]>0,"missing odometry")
        else:
            check(step["evidence"]["destination_id"]=="garden" and step["evidence"]["distance_m"]>0,"wrong route")
        with (directory/"tasks.ndjson").open("a",encoding="utf-8") as stream:
            stream.write(json.dumps(item,ensure_ascii=False)+"\n")
        return item

    def truth():
        state = api("/api/state")
        source = state["sources"]["truth"]
        if state["environment"]=="simulation" and source["fresh"]:
            world_samples.append((source["data"]["x"],source["data"]["y"]))
            return True
        return False

    def batch(first, end):
        pending = []
        for i in range(first,end):
            text,tool = (("报告当前位置","status") if i%2==0 else ("只规划到庭院的路线","preview_route"))
            pending.append((submit(i,text),tool))
        for key,tool in pending:
            item = verify(key,tool)
            report["tasks"].append({"id":key,"operation_id":item["steps"][0]["operation_id"],"tool":tool,"state":item["state"]})
            wait_for(truth,"independent world pose")

    def pause_input():
        compose("pause","bridge")
        wait_for(lambda: not api("/api/state")["sources"]["odom"]["fresh"],"console did not notice stale odometry")
        # The Agent accepts arrivals up to two seconds old, independently of the console TTL.
        time.sleep(2.1)

    def retry_event(key):
        return next((row for row in task(key)["events"] if row["kind"]=="retry"),None)

    def alert_for(key):
        if not sink_path.is_file():
            return None
        with sqlite3.connect(sink_path.resolve().as_uri()+"?mode=ro",uri=True) as conn:
            for row, in conn.execute("select event from events"):
                event = json.loads(row)
                if event["task_id"]==key:
                    return event
        return None

    try:
        compose("up","-d","--no-build","sim","bridge","witness","gateway","core","perception","scene","agent","console")
        base = "http://"+compose("port","console","8080")
        check(base.startswith("http://127.0.0.1:"),"expected private loopback port")
        wait_for(truth,"Gazebo world did not start")
        wait_for(lambda: api("/api/state")["sources"]["pose"]["fresh"],"waiting for fused pose")
        with (directory/"watcher.log").open("w",encoding="utf-8") as logfile:
            watcher = subprocess.Popen([sys.executable,str(ROOT/"client/event_sink.py"),"--db",str(sink_path),"--url",base,
                                       "--kind","agent.retry_exhausted"],env=env,stdout=logfile,stderr=logfile)
        batch(0,count//2)
        pause_input()
        short = submit("brief-outage","报告当前位置")
        first_retry = wait_for(lambda: retry_event(short),"expected transient retry")
        compose("unpause","bridge")
        recovered = verify(short,"status")
        op = recovered["steps"][0]["operation_id"]
        check(first_retry["operation_id"]==op and not recovered["can_retry"],"transient recovery changed identity")
        report["faults"]["brief_outage"] = {"id":short,"state":recovered["state"],"operation_id":op,
                                            "retry_events":sum(e["kind"]=="retry" for e in recovered["events"]),
                                            "operator_retry_calls":0}

        pause_input()
        long = submit("persistent-outage","报告当前位置")
        # An independent, always-running archive consumer observes the event without querying the task or requesting retry.
        alert = wait_for(lambda:alert_for(long),"missing archived escalation",45)
        blocked = done(long)
        last = next(row for row in blocked["events"] if row["kind"]=="retry_exhausted")
        diag = json.loads(last["payload"])["diagnostic"]
        check(blocked["state"]=="RECOVERY_REQUIRED" and blocked["can_retry"],"missing operator handoff")
        check(alert["code"]==diag["code"]=="tool.odom_stale" and diag["catalog"]["hint"],"escalation lacks diagnosis")
        op = blocked["steps"][0]["operation_id"]
        check(alert["operation_id"]==op and alert["trace_id"]==blocked["trace_id"],"lost escalation identity")
        compose("unpause","bridge")
        wait_for(lambda:api("/api/state")["sources"]["odom"]["fresh"],"sensor did not recover")
        compose("restart","agent")
        stable = wait_for(lambda:task(long),"agent restart")
        check(stable["state"]=="RECOVERY_REQUIRED" and stable["can_retry"],"exhausted task resumed without operator")
        api("/agent/retry",{"id":long})
        resumed = verify(long,"status")
        check(resumed["steps"][0]["operation_id"]==op,"operator retry created a new operation")
        check(sum(e["kind"]=="retry_requested" for e in resumed["events"])==1,"operator retry not recorded")
        try:
            api("/agent/retry",{"id":long})
        except HTTPError as exc:
            retry_http = exc.code
            check(retry_http==409,"expected a state conflict on duplicate operator retry")
        else:
            raise AssertionError("completed task accepted another retry")
        report["faults"]["persistent_outage"] = {
            "id":long,"trace_id":blocked["trace_id"],"operation_id":op,"cause":diag["code"],"operator_hint":diag["catalog"]["hint"],
            "paused_state":blocked["state"],"can_retry":blocked["can_retry"],"after_agent_restart":stable["state"],
            "observer_kind":alert["kind"],"observer_event_id":alert["event_id"],"observer_delivery":"local polling event sink",
            "recovery_request":"POST /agent/retry","resumed":resumed["state"],"second_retry_http":retry_http}
        batch(count//2,count)
        for body,key in original_requests:
            check(api("/agent/tasks",body)["id"]==key,"late replay changed task identity")
            check(sum(e["kind"]=="accepted" for e in task(key)["events"])==1,"duplicate acceptance")
        check(len({key for _,key in original_requests})==count+2,"distinct triggers collided")
        wait_for(truth,"end pose")
        report["world_samples"]=len(world_samples)
        report["max_observed_displacement_m"]=max(math.dist(world_samples[0],p) for p in world_samples)
        check(report["max_observed_displacement_m"]<.025,"read-only task displaced rover")
        check(watcher.poll() is None, "archive observer stopped")
        report["completed"]=len(original_requests)
        report["duplicate_trigger_submissions"]=2*len(original_requests)
        report["passed"]=True
    finally:
        if watcher:
            watcher.terminate()
            watcher.wait(timeout=10)
        try:
            if not report["passed"]:
                compose("logs","--no-color","--tail","50")
        finally:
            compose("down","--volumes","--remove-orphans","--timeout","3")
            report["elapsed_s"]=round(time.monotonic()-start,3)
            (directory/"summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            print(directory/"summary.json",flush=True)
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",default="edge-agent-ros:v0.1")
    parser.add_argument("--tasks",type=int,default=36)
    parser.add_argument("--out-root",type=Path,default=ROOT/".ci-results/system/lifecycle")
    args=parser.parse_args()
    if not 2<=args.tasks<=500:
        parser.error("--tasks must be between 2 and 500")
    exercise(args.image,args.tasks,args.out_root)
