"""DSH read-only planning tool and single-snapshot local-path service."""
import argparse
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import build_opener, ProxyHandler

try:
    from .planner import finite, pose, project, stamp, wrap
    from .evaluator import EvaluationError, SERVICE, SnapshotEvaluator, VERSION
except ImportError:
    from planner import finite, pose, project, stamp, wrap
    from evaluator import EvaluationError, SERVICE, SnapshotEvaluator, VERSION

TOOL = "preview_rover_obstacle_path"


def canonical(data):
    return json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()


def reject_constant(value):
    raise ValueError("non-finite number in JSON request")


def pose_at(snap):
    obs=snap.get("odom")
    if not isinstance(obs,dict):
        return None
    p=pose(obs,snap["base_frame"],stamp(snap["scan_stamp_ns"]))
    if not p:
        return None
    if obs.get("valid") is False:
        return None
    for k in ("source_topic","source_id","epoch","position_m","orientation_xyzw","planar_covariance","seq",
              "wall_received_ns","z_m","linear_mps","angular_rps","map_from_odom"):
        if k in obs:
            p[k]=obs[k]
    return p


def finish_evidence(report,observations):
    start,end=observations[0],observations[-1]
    poses=[pose_at(s) for s in observations]
    initial,last=poses[0],poses[-1]
    record={"verified":False,"displacement_m":None,"yaw_change_rad":None,"sample_count":0}
    if initial and last and initial["frame_id"]==last["frame_id"] and initial.get("epoch")==last.get("epoch"):
        record.update(displacement_m=math.hypot(initial["x_m"]-last["x_m"],initial["y_m"]-last["y_m"]),
                      yaw_change_rad=wrap(last["yaw_rad"]-initial["yaw_rad"]))
    body=report["base_frame"]
    times=[]
    valid=True
    for obs,p in zip(observations,poses):
        if (not p or not initial or p["frame_id"]!=initial["frame_id"] or p["child_frame_id"]!=body
                or p.get("epoch")!=initial.get("epoch")):
            valid=False
            continue
        t=stamp(p["stamp_ns"])
        if times and t<times[-1]:
            valid=False
        if not times or t!=times[-1]:
            times.append(t)
        raw=obs["odom"]
        v=raw.get("linear_mps")
        w=raw.get("angular_radps",raw.get("angular_rps"))
        if (not finite(v) or not finite(w) or abs(v)>.04 or abs(w)>.08 or
                math.hypot(initial["x_m"]-p["x_m"],initial["y_m"]-p["y_m"])>.025 or
                abs(wrap(p["yaw_rad"]-initial["yaw_rad"]))>.045):
            valid=False
    record.update(sample_count=len(times),
                  verified=bool(valid and len(times)>=3 and times[-1]-times[0]>=350_000_000 and
                                max(b-a for a,b in zip(times,times[1:]))<=350_000_000))
    report.update(initial_pose=initial,end_observed_pose=last,stationary=record,end_scan_stamp_ns=str(end["scan_stamp_ns"]),
                  scan_sha256="sha256:"+hashlib.sha256(canonical(start)).hexdigest(),
                  map_anchor_at_start=start.get("map_anchor") or (start.get("odom") or {}).get("map_from_odom"),
                  map_anchor_at_end=end.get("map_anchor") or (end.get("odom") or {}).get("map_from_odom"))
    report["map_anchor"]=report["map_anchor_at_start"]
    if initial and report["path_body_m"]:
        report["path_odom_m"]=project(report["path_body_m"],initial)
        x,y=report["path_odom_m"][-1]
        report["planned_endpoint"]={"frame_id":initial["frame_id"],"x_m":x,"y_m":y,"yaw_rad":initial["yaw_rad"]}
    if report["status"]=="planned" and not record["verified"]:
        report.update(status="partial",reason="pose_window_unverified")
    return report


def read_snapshot(url):
    with build_opener(ProxyHandler({})).open(url,timeout=2) as response:
        data=response.read(4*1024*1024+1)
    if len(data)>4*1024*1024:
        raise ValueError("snapshot exceeds bound")
    snap=json.loads(data)
    now=time.time_ns()
    if (not isinstance(snap,dict) or not all(isinstance(snap.get(k),str) and snap[k]
           for k in ("base_frame","source_id","calibration_ref")) or
           not -50_000_000<=now-stamp(snap["scan_stamp_ns"])<=2_000_000_000):
        raise ValueError("unavailable, stale or unidentified sensor snapshot")
    return snap


class Provider:
    version = VERSION
    def __init__(self,state,profile,url):
        state.mkdir(exist_ok=True,parents=True)
        self.path=state/"operations.sqlite"
        self.profile=profile
        self.evaluator=SnapshotEvaluator(profile) if profile else None
        self.url=url.rstrip("/")+"/snapshot"
        self.lock=threading.Lock()
        self.pool=ThreadPoolExecutor(max_workers=1)
        self.active={}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS operations (op TEXT PRIMARY KEY,phase TEXT NOT NULL, result TEXT,"
                       "version TEXT NOT NULL DEFAULT '1.0.0')")
            if not any(r[1]=="version" for r in db.execute("PRAGMA table_info(operations)")):
                db.execute("ALTER TABLE operations ADD COLUMN version TEXT NOT NULL DEFAULT '1.0.0'")
            db.execute("UPDATE operations SET phase='interrupted' WHERE result IS NULL")

    def update(self,op,phase,result=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE operations SET phase=?,result=? WHERE op=?",
                       (phase,canonical(result).decode() if result else None,op))

    def run(self,op):
        try:
            start=read_snapshot(self.url)
            self.update(op,"planning")
            # Sample while computing, so odometry can bracket sensor geometry even for a dense scan.
            observations=[start]
            failures=[]
            done=threading.Event()
            def sample_poses():
                try:
                    while not done.wait(.1):
                        observations.append(read_snapshot(self.url))
                except (OSError,HTTPException,ValueError,KeyError,TypeError) as exc:
                    failures.append(str(exc))
            watcher=threading.Thread(target=sample_poses,daemon=True)
            watcher.start()
            t0=time.monotonic()
            try:
                report=self.evaluator.archived_task_plan(start)
                self.update(op,"end_pose")
                time.sleep(max(0.,.65-(time.monotonic()-t0)))
            finally:
                done.set()
                watcher.join(3.)
            if watcher.is_alive() or failures:
                raise ValueError("pose acquisition interrupted")
            observations.append(read_snapshot(self.url))
            if (any(s["base_frame"]!=start["base_frame"] or s["source_id"]!=start["source_id"] or
                    s["calibration_ref"]!=start["calibration_ref"] for s in observations) or
                    stamp(observations[-1]["scan_stamp_ns"])<=stamp(start["scan_stamp_ns"])):
                raise ValueError("sensor identity changed or acquisition did not advance")
            report=finish_evidence(report,observations)
            report.update(provider_version=self.version,planning_scope="static_local_path",
                          observation_scope="planning_window_start")
            receipt={"operation_id":op,"state":"SUCCEEDED","evidence":report}
        except EvaluationError as exc:
            receipt={"operation_id":op,"state":"FAILED","error":{"code":exc.code,"message":str(exc)}}
        except (OSError,HTTPException,ValueError,KeyError,TypeError,OverflowError) as exc:
            receipt={"operation_id":op,"state":"FAILED","error":{"code":"sensing.observation_unavailable","message":str(exc)[:240]}}
        self.update(op,"complete",receipt)
        return receipt

    def run_admitted(self,op):
        try:
            return self.run(op)
        finally:
            # The worker owns this slot even after every HTTP waiter has gone.
            # Admission publishes the future under the same lock before release.
            with self.lock:
                self.active.pop(op,None)

    def invoke(self,request):
        op=request.get("operation_id")
        if (set(request)-{"operation_id","arguments","version","trace"} or not isinstance(op,str)
                or not 0<len(op)<=256 or request.get("arguments")!={}):
            raise ValueError("expected stable operation_id and empty arguments")
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            row=db.execute("SELECT phase,result,version FROM operations WHERE op=?",(op,)).fetchone()
            if "version" in request and request["version"]!=(row[2] if row else self.version):
                return {"operation_id":op,"state":"FAILED","error":{"code":"planning.binding_mismatch",
                        "message":"provider version differs from the bound operation"}}
            if row and row[1]:
                return json.loads(row[1])
            future=self.active.get(op)
            if row and row[0]=="interrupted" and not future:
                return {"operation_id":op,"state":"FAILED","error":{"code":"sensing.interrupted","message":"original observation window ended"}}
            if not future:
                if self.active:
                    denied={"operation_id":op,"state":"FAILED","error":{"code":"planning.busy",
                            "message":"provider capacity is occupied; no observation was started"}}
                    db.execute("INSERT INTO operations(op,phase,result,version) VALUES (?,'complete',?,?)",
                               (op,canonical(denied).decode(),self.version))
                    db.commit()
                    return denied
                db.execute("INSERT INTO operations(op,phase,version) VALUES (?,'capture',?)",(op,self.version))
                db.commit()
                future=self.pool.submit(self.run_admitted,op)
                self.active[op]=future
        try:
            return future.result(timeout=3.5)
        except FutureTimeout:
            return {"operation_id":op,"state":"RUNNING"}

    def status(self,op):
        with closing(sqlite3.connect(self.path)) as db:
            row=db.execute("SELECT phase,result FROM operations WHERE op=?",(op,)).fetchone()
        return None if not row else {"operation_id":op,"phase":row[0],"receipt":json.loads(row[1]) if row[1] else None}


class Handler(BaseHTTPRequestHandler):
    def send_json(self,status,obj):
        data=canonical(obj)
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)

    def do_GET(self):
        parts=urlsplit(self.path)
        if parts.path=="/health":
            return self.send_json(200,{"ready":True,"plugin_id":"planning.rover_preview","plugin_version":VERSION,
                                       "tool":TOOL,"services":[SERVICE]})
        if parts.path=="/receipt":
            op=parse_qs(parts.query).get("operation_id",[""])[0]
            record=self.server.provider.status(op)
            return self.send_json(200 if record else 404,record or {})
        self.send_json(404,{})

    def do_POST(self):
        fast=self.path=="/evaluate" and self.server.provider.evaluator is not None
        if self.path!="/invoke" and not fast:
            return self.send_json(404,{})
        try:
            self.connection.settimeout(3)
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length",[]))!=1:
                raise ValueError("expected a single content length")
            length=int(self.headers.get("Content-Length","0"))
            if not 0<length<=(1024*1024 if fast else 32768):
                raise ValueError("invalid request size")
            request=json.loads(self.rfile.read(length),parse_constant=reject_constant)
            if not isinstance(request,dict):
                raise ValueError("expected object")
            result=self.server.provider.evaluator.evaluate(request) if fast else self.server.provider.invoke(request)
        except EvaluationError as exc:
            return self.send_json(exc.status,exc.response())
        except (TypeError,ValueError,RecursionError,TimeoutError) as exc:
            return self.send_json(400,EvaluationError(400,"planning.invalid",str(exc)).response() if fast else {"detail":str(exc)})
        self.send_json(200,result)


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--facts",required=True)
    cli.add_argument("--profile",type=Path,default=Path(__file__).with_name("lingao.json"))
    cli.add_argument("--state",type=Path,default=Path("/state"))
    cli.add_argument("--bind",default="127.0.0.1")
    cli.add_argument("--port",type=int,default=8149)
    args=cli.parse_args()
    server=ThreadingHTTPServer((args.bind,args.port),Handler)
    server.provider=Provider(args.state,json.loads(args.profile.read_bytes()),args.facts)
    server.serve_forever()
