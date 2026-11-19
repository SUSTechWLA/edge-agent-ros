"""SDD-042. Film one natural-language round trip with two recoverable faults in private Gazebo."""
import argparse
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import uuid
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from long_mission_world import await_recording, finalize, make_world, mark, recording_services, rows_at

ROOT = Path(__file__).resolve().parents[2]
POINTS = [[1.1,0], [2.4,0], [3.3,1.35], [3.65,.25], [2.3,.15], [1.1,.1], [0,0]]
REQUEST = "请依次巡检" + "、".join(f"({x},{y})" for x,y in POINTS) + "，然后报告状态"


def run(out, image):
    out=out.resolve()
    out.mkdir(parents=True,exist_ok=False)
    run_id=uuid.uuid4().hex[:10]
    project="film-route-"+run_id
    key="patrol-"+run_id
    op=key+"/step/1"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    domain=40+int(run_id[:2],16)%40
    partition="film_"+run_id
    http=build_opener(ProxyHandler({}))
    def api(path,body=None):
        req=Request(f"http://127.0.0.1:{port}"+path,
                    data=json.dumps(body,ensure_ascii=False).encode() if body else None,
                    headers={"Content-Type":"application/json"} if body else {})
        with http.open(req,timeout=4) as response:
            return json.load(response)
    def marker(phase,**fields):
        mark(out,phase,key,**fields)
        print(phase,flush=True)
    def wait(fn,timeout=30):
        limit=time.monotonic()+timeout
        while time.monotonic()<limit:
            try:
                obj=fn()
                if obj: return obj
            except (URLError,OSError):
                pass
            time.sleep(.2)
        raise TimeoutError("timed out waiting for "+fn.__name__)
    def save(name,obj):
        (out/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n","utf8")
    env={k:v for k,v in os.environ.items() if not k.startswith(("COMPOSE_","EDGE_"))}
    env.update(COMPOSE_DISABLE_ENV_FILE="1",EDGE_IMAGE=image,EDGE_CONSOLE_PORT=str(port),
               EDGE_ROS_DOMAIN_ID=str(domain),EDGE_OLLAMA_URL="",EDGE_PLANNER_URL="",EDGE_TRACE_CONTENT="full")
    proof=dict(passed=False, task_id=key, operation_id=op, project=project, image=image, request=REQUEST,
               planner="local explicit-coordinate grammar",goals=POINTS, submissions=0)
    proof["image_id"]=subprocess.check_output(["docker","image","inspect",image,"--format","{{.Id}}"],text=True).strip()
    with tempfile.TemporaryDirectory(prefix=project) as temp:
        path=Path(temp)
        plugins=path/"plugins"
        plugins.mkdir()
        env["EDGE_PLUGINS_DIR"]=str(plugins)
        world=make_world(ROOT,out/"world.sdf")
        services=recording_services(image,domain,partition,world,out)
        services.update({name:{"environment":{"GZ_PARTITION":partition}} for name in ("agent","console","core","gateway")})
        override=path/"record.json"
        override.write_text(json.dumps({"services":services}),"utf8")
        base=["docker","compose","-p",project,"-f",str(ROOT/"compose.yaml"),"-f",str(override)]
        def dc(*args,check=True):
            p=subprocess.run(base+list(args),cwd=ROOT,env=env,capture_output=True,encoding="utf8",errors="replace",timeout=80)
            if check and p.returncode: raise RuntimeError(p.stderr[-2000:] or p.stdout[-2000:])
            return p.stdout.strip()
        paused=False
        try:
            dc("up","-d","--no-build",*services)
            await_recording(out,65)
            wait(lambda:api("/api/state")["environment"]=="simulation" and api("/agent/capabilities"))
            dc("exec","-T","core","/usr/local/bin/edge-entrypoint","python3","/opt/edge/client/ready.py")
            cid=dc("ps","-q","agent")
            def identity():
                obj=json.loads(subprocess.check_output(["docker","inspect",cid]))[0]
                return {"started":obj["State"]["StartedAt"],"restarts":obj["RestartCount"],"state":obj["State"]["Status"]}
            before=identity()
            dc("pause","core")
            paused=True
            marker("fault_core",detail="验收脚本暂停 Core；此次查询将短时收不到回执")
            admission=api("/agent/tasks",{"id":key,"text":REQUEST})
            proof["submissions"]+=1
            assert admission["id"]==key
            trace=admission["trace_id"]
            marker("submitted",text=REQUEST,goals=POINTS,trace_id=trace)
            def task():
                return api("/agent/tasks?id="+key)
            first=wait(lambda:t if (t:=task())["steps"] else None)
            assert [s["tool"] for s in first["steps"]]==["inspect","status"],first["detail"]
            marker("planned",tools=["inspect","status"],provider=first["provider"])
            retry=wait(lambda:next((e for e in task()["events"] if e["kind"]=="retry"),None),22)
            assert retry["operation_id"]==op,retry
            marker("retry",detail=retry["detail"],operation_id=op)
            dc("unpause","core")
            paused=False
            marker("core_restored",detail="测试脚本解除 Core 暂停；Agent 依退避策略重试")
            def moving():
                s=api("/api/state")
                truth=s["sources"]["truth"]
                return truth["data"] if truth["fresh"] and abs(truth["data"]["v"])>.12 and any(
                    m["mission_id"]==op and m["state"]=="RUNNING" for m in s["missions"]) else None
            pose=wait(moving,45)
            marker("moving",operation_id=op,truth=pose)
            # Crash the application process. Docker's existing restart policy handles recovery.
            # Scope to the already-verified isolated service, without a second submit/start.
            kill_app="""import os,signal
from pathlib import Path
ids=[int(p.parent.name) for p in Path('/proc').glob('[0-9]*/cmdline')
     if p.read_bytes().split(bytes(1))[:2]==[b'python3',b'/opt/edge/agent/service.py']]
assert len(ids)==1,ids
os.kill(ids[0],signal.SIGKILL)"""
            marker("fault_agent",operation_id=op,detail="注入 Agent 进程退出；已受理的巡检由 Core 和 Gateway 持续执行")
            dc("exec","-T","agent","python3","-c",kill_app,check=False)
            after=wait(lambda:v if (v:=identity())["restarts"]>before["restarts"] and v["state"]=="running" else None)
            resumed=wait(task)
            assert resumed["trace_id"]==trace and resumed["steps"][0]["operation_id"]==op
            marker("agent_restored",operation_id=op,restart_count=after["restarts"],state=resumed["state"])
            receipt_ids=set()
            dispatched=set()
            deadline=time.monotonic()+270
            while time.monotonic()<deadline:
                state=api("/api/state")
                mission=next((m for m in state["missions"] if m["mission_id"]==op),None)
                for i,step in enumerate(mission["steps"] if mission else [],1):
                    if step.get("operation_id") and i not in dispatched:
                        dispatched.add(i)
                        marker("goal",index=i,total=len(POINTS),xy=POINTS[i-1],operation_id=step["operation_id"])
                    if step.get("odom_stamp_ns") and i not in receipt_ids:
                        receipt_ids.add(i)
                        marker("waypoint",index=i,total=len(POINTS),xy=[step["evidence_x"],step["evidence_y"]],
                               operation_id=step["operation_id"],odom_stamp_ns=step["odom_stamp_ns"])
                row=task()
                if row["state"] in ("BLOCKED","RECOVERY_REQUIRED","CANCELED","FAILED"):
                    raise RuntimeError(str((row["state"],row["detail"])))
                if row["state"]=="SUCCEEDED" and len(receipt_ids)==len(POINTS): break
                time.sleep(.27)
            else: raise TimeoutError("missing ordered waypoints or terminal success")
            save("task.json",row)
            s1,s2=row["steps"]
            obs=s2["evidence"]
            assert row["trace_id"]==trace and s1["operation_id"]==op and s2["operation_id"]==key+"/step/2"
            assert s1["evidence"]["verified_count"]==len(POINTS)
            assert all(math.dist(a,b)<.12 for a,b in zip(POINTS,s1["evidence"]["points"],strict=True))
            assert math.hypot(obs["x"],obs["y"])<.12 and abs(obs["speed"])<.08
            marker("verified",x=obs["x"],y=obs["y"],speed=obs["speed"])
            logs=dc("logs","--no-color","gateway").splitlines()
            starts={str(i):sum(f"Tracking {op}/waypoint/{i} to " in line for line in logs) for i in range(1,len(POINTS)+1)}
            assert set(starts.values())=={1},starts
            marker("finished",state=row["state"],verified_count=len(POINTS),gateway_starts=sum(starts.values()))
            media=finalize(out,18)
            track=[x for x in rows_at(out) if x["kind"]=="world_pose"]
            assert media["measured_world_path_m"]>7. and math.dist(track[-1]["xy"],POINTS[-1])<.15
            at_rest=[x for x in track if x["stamp_ns"]>=track[-1]["stamp_ns"]-800000000]
            drift=max(math.dist(x["xy"],track[-1]["xy"]) for x in at_rest)
            assert len(at_rest)>2 and drift<.02
            assert max(x["xy"][0] for x in track)>3.45
            proof.update(passed=True,trace_id=trace,provider=row["provider"],terminal=row["state"],retry=retry["detail"],
                         restore={"before":before,"after":after},gateway_starts=starts,media=media,
                         final_status=obs,waypoint_evidence=s1["evidence"],stop_drift_m=drift)
        except Exception as exc:
            proof["error"]=str(exc)
            (out/"service.log").write_text(dc("logs","--no-color","--tail","40",check=False),"utf8")
            raise
        finally:
            if paused: dc("unpause","core",check=False)
            save("acceptance.json",proof)
            dc("down","--volumes","--remove-orphans","--timeout","4",check=False)


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--out",required=True,type=Path)
    cli.add_argument("--image",required=True)
    args=cli.parse_args()
    run(args.out,args.image)
