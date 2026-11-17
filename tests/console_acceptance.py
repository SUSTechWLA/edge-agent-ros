"""Run against an idle simulation via its public console URL. The robot will move."""
import argparse
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(base, out, verify_model=False):
    http = build_opener(ProxyHandler({}))
    def api(path, body=None):
        req=Request(base+path, data=json.dumps(body).encode() if body is not None else None,
                    headers={"Content-Type":"application/json"})
        with http.open(req,timeout=8) as reply:
            return json.load(reply)

    def wait(call, ok, seconds=65):
        until=time.monotonic()+seconds
        while True:
            item=call()
            if ok(item):
                return item
            if time.monotonic()>=until:
                raise TimeoutError(str(item)[:2500])
            time.sleep(.25)

    run_id="desk-"+uuid.uuid4().hex[:8]
    report={"run_id":run_id,"utc":datetime.now(timezone.utc).isoformat(),"tasks":[]}
    state=wait(lambda: api("/api/state"),lambda s: all(s["sources"][n]["fresh"] for n in
        ("scan","imu","pose","map","odom","truth","perception")))
    check(state["environment"] == "simulation","this test requires a Gazebo simulation")
    src=state["sources"]
    counts=src["perception"]["data"]
    check(counts["matched_scans"]>0 and counts["occupied_cells"]>0 and counts["free_cells"]>0,"no real map")
    report["sensors"]={"scan_beams":len(src["scan"]["data"]["ranges"]),"fusion":counts}
    cases=[("round","依次巡检 (0.8,0) 和 (0.8,0.7)，然后报告状态",(.8,.7),2),
           ("west","前往 (-0.5,0.4)",(-.5,.4),1),
           ("home","巡检 (0,0)",(0,0),1),
           ("pose","报告机器人当前位置",None,1)]
    if verify_model:
        cases.append(("model","麻烦把小车开到 (0.35,0.15) 做一次巡检，在那里停稳。",(.35,.15),1))
    for suffix,text,goal,steps in cases:
        key=f"{run_id}-{suffix}"
        req={"id":key,"text":text}
        api("/agent/tasks",req)
        api("/agent/tasks",req)
        task=wait(lambda: api("/agent/tasks?"+urlencode({"id":key})),
                  lambda t:t["state"] in ("SUCCEEDED","BLOCKED","RECOVERY_REQUIRED"))
        check(task["state"]=="SUCCEEDED" and len(task["steps"])==steps,str(task))
        row={"id":key,"text":text,"state":task["state"],"provider":task["provider"],"steps":task["steps"]}
        if suffix=="model":
            check(task["provider"]!="local","model case did not exercise the configured planner")
        if goal:
            truth=api("/api/state")["sources"]["truth"]
            actual=[truth["data"]["x"],truth["data"]["y"]]
            check(truth["fresh"] and math.dist(actual,goal)<.18, f"world differs: {actual}, {goal}")
            row["world"]=actual
            row["world_error_m"]=math.dist(actual,goal)
        # Each durable ledger has its own collector. A completed read step may
        # reach the console before the motion ledger's final transaction.
        def trace_complete(page):
            kinds=[e["kind"] for e in page["events"]]
            return kinds.count("agent.verified")==steps and (not goal or "mission.transition" in kinds)

        def timeline_pages():
            rows=[]
            after=0
            while True:
                page=api("/api/events?"+urlencode({"task_id":key,"after":after,"limit":100}))
                rows.extend(page["events"])
                if not page["has_more"]:
                    return {"events":rows}
                check(rows and rows[-1]["seq"]>after,"archive pagination made no progress")
                after=rows[-1]["seq"]

        timeline=wait(timeline_pages,trace_complete)
        kinds=[e["kind"] for e in timeline["events"]]
        if goal:
            check("step.verified" in kinds and "mission.transition" in kinds,"ROS action trace missing")
            e=next(e for e in timeline["events"] if e["kind"]=="step.verified")
            check(e["cause_seq"] is not None,"ledger predecessor missing")
            frame=api(f"/api/replay?at={e['seq']}")
            check(frame["mode"]=="replay" and frame["cursor"]==e["seq"],"wrong historical snapshot")
        row["events"]=kinds
        report["tasks"].append(row)
    with http.open(base+"/api/export?"+urlencode({"task_id":report["tasks"][0]["id"]}), timeout=8) as response:
        data=response.read()
    rows=[json.loads(line) for line in data.splitlines()]
    check(rows and all(a["seq"]<b["seq"] for a,b in zip(rows,rows[1:])),"export sequence invalid")
    out.mkdir(exist_ok=True,parents=True)
    (out/"sample-trace.ndjson").write_bytes(data)
    (out/"missions.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"run_id":run_id,"tasks":[(t["id"],t["state"]) for t in report["tasks"]],
                      "archived_events":len(rows)},ensure_ascii=False))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",default="http://127.0.0.1:8765")
    parser.add_argument("--out",type=Path,default=Path(".ci-results/console"))
    parser.add_argument("--model",action="store_true",help="also run a free-form navigation request with the configured planner")
    opts=parser.parse_args()
    run(opts.url,opts.out,opts.model)
