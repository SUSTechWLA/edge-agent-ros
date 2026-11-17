"""Validate actual model → ROS read and two related triggers. Emits a content-free evidence summary."""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


def check(value, why):
    if not value:
        raise RuntimeError(why)


def run(base,out):
    http=build_opener(ProxyHandler({}))
    def get(path,body=None,ndjson=False):
        req=Request(base.rstrip("/")+path,data=json.dumps(body).encode() if body is not None else None,
                    headers={"Content-Type":"application/json"})
        with http.open(req,timeout=10) as reply:
            return [json.loads(line) for line in reply] if ndjson else json.load(reply)

    def done(key):
        until=time.monotonic()+90
        while time.monotonic()<until:
            task=get("/agent/tasks?"+urlencode({"id":key}))
            if task["state"] in ("SUCCEEDED","BLOCKED","RECOVERY_REQUIRED"):
                check(task["state"]=="SUCCEEDED",f"{key}: {task['state']} {task['detail']}")
                return task
            time.sleep(.22)
        raise TimeoutError(key)

    def traces(task):
        return [(row["kind"],json.loads(row["payload"])["trace"]) for row in task["events"]]

    def pair(rows,layer):
        a=next(v for k,v in rows if k==layer+".request")
        b=next(v for k,v in rows if k==layer+".response" and v["call_id"]==a["call_id"])
        return a,b

    key="watch-"+uuid.uuid4().hex[:12]
    request={"text":"麻烦只读取一下机器人的当前状态，将它的位置、朝向和速度返回给我。不要移动。",
             "trigger":{"kind":"watchdog","source":"trace-acceptance","id":key}}
    parent=get("/agent/tasks",request)
    replay=get("/agent/tasks",request)
    check(parent["id"]==replay["id"],"duplicate trigger created another task")
    parent=done(parent["id"])
    p=traces(parent)
    check(sum(k=="accepted" for k,_ in p)==1,"duplicate acceptance")
    check(parent["provider"].startswith("ollama/"),"no actual model call")
    model_in,model_out=pair(p,"model")
    tool_in,tool_out=pair(p,"tool")
    check(tool_in["module"]=="status","provider chose a different capability")
    body=model_in["artifact"]["content"]
    check({m["role"] for m in body["messages"]}=={"system","user"} and body["format"],"missing original prompts or schema")
    reply=json.loads(model_out["artifact"]["content"]["message"]["content"])
    check(any(s["tool"]=="status" for s in reply["steps"]),"no status in real model completion")
    evidence=parent["steps"][0]["evidence"]
    check(evidence["source"].endswith("odom") and evidence["stamp_ns"]>0 and "yaw" in evidence,"no live ROS evidence")
    end=p[-1][1]["event_id"]
    child=done(get("/agent/tasks",{"text":"报告当前位置","trigger":{"kind":"agent","id":key+"-child",
        "source":"remote-demo","parent_task_id":parent["id"],"parent_event_id":end}})["id"])
    check(child["trace_id"]==parent["trace_id"],"trace lost when delegated")
    until=time.monotonic()+15
    while True:
        exported=get("/api/export?"+urlencode({"trace_id":parent["trace_id"]}),ndjson=True)
        if any(r["task_id"]==child["id"] and r["kind"]=="agent.verified" for r in exported):
            break
        if time.monotonic()>until:
            raise TimeoutError("child was not present in trace export")
        time.sleep(.3)
    first=next(r for r in exported if r["task_id"]==child["id"] and r["kind"]=="agent.accepted")
    check(first["parent_event_id"]==end and first["cause_seq"] in {r["seq"] for r in exported if r["event_id"]==end},
          "archive lost child cause")
    check({r["task_id"] for r in exported}=={parent["id"],child["id"]},"unexpected tasks in export")
    report={"utc":datetime.now(timezone.utc).isoformat(),"scope":"Gazebo, actual Ollama and ROS odometry",
            "passed":True,"root_task":parent["id"],"trace_id":parent["trace_id"],"trigger":parent["trigger"],
            "duplicate_task":replay["id"],"child_task":child["id"],"child_trigger":child["trigger"],
            "provider":parent["provider"],"prompt_revision":model_in["prompt"],"profile_revision":model_in["profile"],
            "model_call_id":model_in["call_id"],"model_request_sha256":model_in["artifact"]["sha256"],
            "model_response_sha256":model_out["artifact"]["sha256"],"model_ms":model_out["duration_ms"],
            "model_selected_tools":[s["tool"] for s in reply["steps"]],
            "tool_call_id":tool_in["call_id"],"tool":tool_in["module"],"tool_ms":tool_out["duration_ms"],
            "ros_observation":{"topic":evidence["source"],"frame":evidence["frame"],"stamp":evidence["stamp"],
                               "x":evidence["x"],"y":evidence["y"],"yaw":evidence["yaw"],"speed":evidence["speed"]},
            "exported_event_count":len(exported),"indexed_events":[{k:r[k] for k in (
             "seq","event_id","parent_event_id","task_id","kind","code","call_id","trigger_kind")} for r in exported]}
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"parent":parent["id"],"child":child["id"],"model":parent["provider"],"count":len(exported)}))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",default="http://127.0.0.1:8765")
    parser.add_argument("--out",type=Path,default=Path(".ci-results/model-trace.json"))
    args=parser.parse_args()
    run(args.url,args.out)
