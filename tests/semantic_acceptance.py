"""Drive an idle campus Gazebo simulation and verify the independent world trajectory."""
import argparse
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


def require(check, reason):
    if not check:
        raise AssertionError(reason)


def run(base, out):
    http=build_opener(ProxyHandler({}))
    def api(path, body=None):
        req=Request(base.rstrip("/")+path,data=json.dumps(body).encode() if body else None,
                    headers={"Content-Type":"application/json"})
        with http.open(req,timeout=10) as response:
            return json.load(response)

    def truth():
        state=api("/api/state")
        require(state["environment"]=="simulation","only a Gazebo simulation is permitted")
        src=state["sources"]["truth"]
        if not src["fresh"]:
            return None
        d=src["data"]
        return {"stamp_s":src["stamp_s"],"xy":[d["x"],d["y"]],"v":d["v"],"w":d["w"]}

    run_id="campus-"+uuid.uuid4().hex[:10]
    report={"run_id":run_id,"utc":datetime.now(timezone.utc).isoformat(),"passed":False,"tasks":[]}

    def mission(suffix, text, success=True):
        nonlocal report
        key=f"{run_id}-{suffix}"
        row={"id":key,"text":text,"truth":[]}
        report["tasks"].append(row)
        start=truth()
        require(start is not None,"missing fresh world pose")
        row["truth"].append(start)
        req={"id":key,"text":text,"trigger":{"kind":"user","source":"semantic-acceptance","id":key}}
        api("/agent/tasks",req)
        api("/agent/tasks",req)
        until=time.monotonic()+100
        while time.monotonic()<until:
            pose=truth()
            require(pose is not None,"independent world-pose samples became stale")
            if pose["stamp_s"]!=row["truth"][-1]["stamp_s"]:
                row["truth"].append(pose)
            task=api("/agent/tasks?"+urlencode({"id":key,"limit":1}))
            if task["state"] in ("SUCCEEDED","BLOCKED","FAILED","CANCELED","RECOVERY_REQUIRED"):
                break
            time.sleep(.11)
        else:
            raise TimeoutError(key+" did not terminate")
        row.update(state=task["state"],detail=task["detail"],trace_id=task["trace_id"],
                   provider=task["provider"],steps=task["steps"])
        last=json.loads(task["events"][-1]["payload"])["trace"]
        row["terminal_code"]=last["code"]
        expected="SUCCEEDED" if success else "BLOCKED"
        require(row["state"]==expected,f"{text}: expected {expected}, got {row['state']}: {row['detail']}")
        require(row["provider"]=="local" and row["trace_id"]==key,"wrong planner or trace identity")
        return row

    def arrival(row, goal, crossing):
        trail=row["truth"]
        row["world_error_m"]=math.dist(trail[-1]["xy"],goal)
        require(row["world_error_m"]<.18,f"world goal error {row['world_error_m']}")
        if crossing:
            gate=[s["xy"] for s in trail if abs(s["xy"][0]-1.8)<.2]
            require(gate and max(abs(y) for _,y in gate)<.55,"did not traverse the door opening")
            row["gate_samples"]=len(gate)
            row["gate_max_abs_y"]=max(abs(y) for _,y in gate)
            evidence=row["steps"][0]["evidence"]
            require(evidence["verified_count"]==len(evidence["waypoints"]) and evidence["digest"],"missing waypoint evidence")

    def stationary(row, seconds=.55):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            p=truth()
            require(p is not None,"no fresh stop evidence")
            if p["stamp_s"]!=row["truth"][-1]["stamp_s"]:
                row["truth"].append(p)
            time.sleep(.09)
        tail=row["truth"][-3:]
        require(len(tail)==3 and max(abs(p["v"]) for p in tail)<.05 and
                max(abs(p["w"]) for p in tail)<.10,"terminal task still moving")

    try:
        until=time.monotonic()+55
        while time.monotonic()<until:
            ready=truth()
            if ready and any(c["id"]=="navigate_place" for c in api("/agent/capabilities")["capabilities"]):
                break
            time.sleep(.25)
        else:
            raise TimeoutError("campus simulator or named-place plugin unavailable")
        require(math.dist(ready["xy"],[0,0])<.18,"start at the campus desk")
        arrival(mission("garden","去庭院巡检"),[3.3,1.35],True)
        arrival(mission("desk","回到工位"),[0,0],True)
        for suffix,request,code in (("unknown","去火星巡检","scene.place_unknown"),
                                    ("closed","去维修区巡检","scene.place_inaccessible")):
            row=mission(suffix,request,False)
            stationary(row)
            require(row["terminal_code"]==code,"unexpected error: "+row["terminal_code"])
            require(max(math.dist(row["truth"][0]["xy"],p["xy"]) for p in row["truth"])<.025 and
                    max(abs(p["v"]) for p in row["truth"])<.05,"rejected destination produced motion")
        wall=mission("wall","巡检 (2.2,2.1)",False)
        require(wall["terminal_code"]=="navigation.path_blocked","wall did not trigger laser stop: "+wall["terminal_code"])
        stationary(wall)
        require(.4<wall["truth"][-1]["xy"][0]<1.71 and max(p["xy"][0] for p in wall["truth"])<1.71,
                "world evidence does not show a stop in front of the wall")
        arrival(mission("recover","回到工位"),[0,0],False)
        report["passed"]=True
    except Exception as exc:
        report["error"]=str(exc)
        raise
    finally:
        out.parent.mkdir(exist_ok=True,parents=True)
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"passed":report["passed"],"cases":[[r["id"],r.get("state")] for r in report["tasks"]]},
                         ensure_ascii=True))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",default="http://127.0.0.1:8765")
    parser.add_argument("--out",type=Path,default=Path(".ci-results/semantic.json"))
    args=parser.parse_args()
    run(args.url,args.out)
