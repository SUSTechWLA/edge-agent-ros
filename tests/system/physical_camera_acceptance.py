"""SDD-045 P2 isolated ROS Image + source witness, recovery and operator handoff."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from soft_runtime_acceptance import ROOT, check, command, until, write


def run(image, output):
    if output.exists():
        raise FileExistsError(f"use a new evidence path: {output}")
    output.parent.mkdir(parents=True,exist_ok=True)
    project = "edge-test-physical-" + uuid.uuid4().hex[:10]
    report = {"passed":False,"scope":"private ROS2 Image publisher; no physical device attached",
              "project":project,"image":image,"stages":{}}
    stages = report["stages"]
    with tempfile.TemporaryDirectory(prefix=project) as temp:
        work = Path(temp)
        site,second = work/"site",work/"site2"
        base = ["docker","compose","-p",project,"-f","compose.yaml","-f","compose.physical-camera.yaml",
                "-f","compose.physical-camera-test.yaml"]
        env = {k:v for k,v in os.environ.items() if not k.startswith(("EDGE_","COMPOSE_"))}
        started = False
        try:
            info = json.loads(command(["docker","image","inspect",image]))[0]
            platform = info["Os"] + "/" + info["Architecture"]
            selected = json.loads(command(["docker","image","inspect","--platform",platform,image]))[0]
            digest = selected["Descriptor"]["digest"]
            revision = (info["Config"].get("Labels") or {})["org.opencontainers.image.revision"]
            commit = revision.removesuffix("-dirty")
            report.update(platform=platform,platform_digest=digest,source_revision=revision)
            prep = [sys.executable,"-B","scripts/prepare_physical_camera.py","--source-commit",commit]
            command([*prep,"--site",str(site),"--image",image],timeout=300)
            command([*prep,"--site",str(second),"--version","2.0.0","--image-manifest",str(site/"trust/image.json"),
                     "--image-digest",digest])
            env.update(COMPOSE_DISABLE_ENV_FILE="1", EDGE_IMAGE=image,EDGE_PHYSICAL_IMAGE=info["Id"],
                       EDGE_PHYSICAL_PLATFORM_DIGEST=digest,EDGE_PHYSICAL_SITE=str(site),
                       EDGE_CONSOLE_PORT="0",EDGE_PHYSICAL_DOMAIN_ID="161",EDGE_EXTRA_PLUGIN_DIR="",
                       EDGE_PLUGINS_DIR=str(work/"plugins"))
            (work/"plugins").mkdir()

            def dc(*args):
                return command([*base,*args],env=env)

            def admin(action,*args):
                return json.loads(dc("run","--rm","-T","--no-deps","physical-admin",action,*args))

            def rejected(action,expected,*,deployment=site):
                result = subprocess.run([*base,"run","--rm","-T","--no-deps","physical-admin",action],
                                        cwd=ROOT,env={**env,"EDGE_PHYSICAL_SITE":str(deployment)},
                                        capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=30)
                check(result.returncode!=0 and expected in result.stderr+result.stdout,
                      f"expected {expected} from {action}: {(result.stderr + result.stdout)[-900:]}")
                return expected

            def ledger(op=None):
                args = ["--operation",op] if op else []
                return json.loads(dc("exec","-T","physical-controller","python3","-B",
                                     "/opt/edge/agent/physical_controller.py","status",*args))

            def local_rows(service,path,sql):
                # Query independent read-only artifacts. Requests cannot mutate the controller or issue captures.
                code = ("import json,sqlite3,sys; db=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); "
                        "print(json.dumps(db.execute(sys.argv[2]).fetchall()))")
                return json.loads(dc("exec","-T",service,"python3","-Bc",code,path,sql))

            started = True
            stages["installation"] = admin("install")
            dc("up","-d","--no-build","agent","console","physical-controller","physical-camera","physical-verifier","physical-camera-source")
            port = dc("port","console","8080").splitlines()[0]
            check(port.startswith("127.0.0.1:"),"expected loopback-only Console")
            opener = build_opener(ProxyHandler({}))

            def api(path, body=None):
                request = Request("http://" + port + path,data=None if body is None else json.dumps(body).encode(),
                                  headers={"Content-Type":"application/json"})
                with opener.open(request,timeout=6) as response:
                    return json.load(response)

            def capability():
                caps = api("/agent/capabilities")["capabilities"]
                return next((c for c in caps if c["id"]=="capture_camera_frame"),None)

            cap = until(capability,"native physical export")
            def submit(name):
                key = "camera-ac-" + name
                api("/agent/tasks",{"id":key,"text":cap["description"]})
                return key

            def task(key,state):
                def match():
                    row = api("/agent/tasks?" + urlencode({"id":key}))
                    if row["state"] in ("RECOVERY_REQUIRED","SUCCEEDED","BLOCKED","FAILED") and row["state"]!=state:
                        raise AssertionError(f"{key}: {row['state']}: {row['detail']}")
                    return row if row["state"]==state else None
                result = until(match,key + " " + state,seconds=65)
                return {k:result[k] for k in ("id","state","detail","steps")}

            def device_op(op,phase):
                def match():
                    try:
                        status = ledger(op)
                    except RuntimeError as error:
                        if "unknown physical operation" in str(error):
                            return None
                        raise
                    return status if status["state"] == phase else None
                return until(match,op + " " + phase,seconds=35)

            first = submit("healthy")
            stages["healthy_task"] = task(first,"SUCCEEDED")
            original = ledger(first+"/step/1")
            check(original["proof"]["verdict"] == "pass","no original independent source proof")
            stages["healthy_evidence"] = original
            capture = original["candidate"]["artifact_sha256"]
            observations = local_rows("physical-camera-source","/witness/source.sqlite","SELECT digest FROM frames")
            check([capture] in observations, "sensor-side source never emitted this capture")
            stages["source_witness_matches"] = True
            api("/agent/tasks",{"id":first,"text":cap["description"]})
            check(task(first,"SUCCEEDED")["steps"]==stages["healthy_task"]["steps"],"repeated task regenerated plan")

            dc("stop","physical-verifier")
            second_task = submit("verifier-offline")
            second_op = second_task + "/step/1"
            stages["unverified_candidate"] = device_op(second_op,"CANDIDATE")
            check(api("/agent/tasks?"+urlencode({"id":second_task}))["state"]!="SUCCEEDED","false success without verifier")
            stages["blocked_upgrade"] = rejected("install","plugin.physical_hot_reload_forbidden",deployment=second)
            stages["early_release"] = rejected("release","plugin.stop_unconfirmed")
            dc("start","physical-verifier")
            stages["verified_after_return"] = task(second_task,"SUCCEEDED")
            # Ensure the test actually supported a second operation under the same deployment fence.
            check(ledger(second_op)["lease"]["epoch"] == original["lease"]["epoch"],"unexpected in-place deployment change")

            dc("stop","physical-camera-source")
            broken = submit("interrupted")
            broken_op = broken+"/step/1"
            before = device_op(broken_op,"CLAIMED")
            def armed():
                return any(r == [broken_op,"armed"] for r in local_rows("physical-camera","/captures/device.sqlite",
                           "SELECT op,state FROM captures"))
            until(armed,"durable device-side claim")
            dc("kill","-s","SIGKILL","physical-camera")
            stages["killed_operation"] = device_op(broken_op,"RECOVERY_REQUIRED")
            check(stages["killed_operation"]["lease"] == before["lease"],"lost operation silently rebound")
            stages["unknown_task"] = task(broken,"RECOVERY_REQUIRED")
            dc("start","physical-camera-source","physical-camera")
            def recovered_outbox():
                rows = local_rows("physical-camera","/captures/device.sqlite","SELECT op,state FROM captures")
                return rows if [broken_op,"unknown_reported"] in rows else None
            stages["device_records"] = until(recovered_outbox,"device reports original unknown")
            device_stats = dict((op,state) for op,state in stages["device_records"])
            check(len(device_stats)==3 and sum(s=="candidate_reported" for s in device_stats.values())==2,
                  "a task generated excess captures or replayed after a crash")
            stages["observed_source_readers"] = local_rows("physical-camera-source","/witness/source.sqlite",
                        "SELECT gid,offered_frames FROM readers ORDER BY first_stamp")
            check(stages["observed_source_readers"],"missing independent raw subscription observations")

            stages["drain"] = admin("drain")
            def quiet():
                row = ledger()
                return row if row["phase"]=="QUIESCENT" else None
            stages["quiet"] = until(quiet,"independent source-live quiescence",seconds=30)
            stages["release"] = admin("release")
            old_camera = dc("ps","-q","physical-camera")
            dc("stop","physical-camera")
            inspect = json.loads(command(["docker","inspect",old_camera]))
            proof_path = work / "old-camera.json"
            write(proof_path,inspect)
            stopped = dc("run","--rm","-T","--no-deps","-v",str(proof_path)+":/inspection.json:ro",
                         "physical-admin","process-stopped","--inspection-file","/inspection.json")
            stages["exited"] = json.loads(stopped)
            dc("stop","physical-controller")
            env["EDGE_PHYSICAL_SITE"] = str(second)
            stages["installed_v2"] = admin("install")
            dc("up","-d","--no-build","--force-recreate","physical-controller","physical-camera","physical-verifier")
            until(capability,"new deployment ready")
            old = ledger(broken_op)
            check(old["state"]=="RECOVERY_REQUIRED" and old["lease"]==before["lease"],"upgrade erased unresolved evidence")
            stale = "\n".join(["import sys,json","sys.path.insert(0,'/opt/edge/perception/camera')",
                 "from physical_io import request,ControlError","try: request('camera','check',lease=json.loads(sys.argv[1]))",
                 "except ControlError as error: print(error.code)","else: raise SystemExit('stale fence accepted')"])
            stages["stale_fence"] = dc("exec","-T","physical-camera","python3","-Bc",stale,json.dumps(before["lease"]))
            check(stages["stale_fence"] in ("plugin.fence_rejected","tool.binding_unavailable"),"unexpected stale fence result")
            new_task = submit("version-two")
            stages["v2_task"] = task(new_task,"SUCCEEDED")
            check(ledger(new_task+"/step/1")["pin"]["version"]=="2.0.0","new task bound to old camera")
            by_digest = dict(local_rows("physical-camera-source","/witness/source.sqlite","SELECT digest,stamp FROM frames"))
            readers = local_rows("physical-camera-source","/witness/source.sqlite","SELECT gid,first_stamp,last_stamp FROM readers")
            successful_ops = [ledger(key+"/step/1") for key in (first,second_task,new_task)]
            mismatches = sum(by_digest.get(op["candidate"]["artifact_sha256"]) != op["candidate"]["stamp_ns"]
                             or op["proof"]["verdict"]!="pass" for op in successful_ops)
            ownership = [[gid for gid,start,end in readers if start <= op["candidate"]["stamp_ns"] <= end]
                         for op in successful_ops]
            check(all(len(matches)==1 for matches in ownership),"insufficient independent subscriber truth for a successful capture")
            stages["camera_scorecard"] = {
                "scope":"simulated-driver ROS Image capture; outstanding crash observation is separately unknown",
                "successful_operations_with_source_truth":len(successful_ops),"false_success":mismatches,
                "independent_readers_for_success":ownership,"unplanned_source_readers":len(set(r[0] for r in readers)
                                                                                           - {ids[0] for ids in ownership}),
                "unresolved_operations":1}
            check(not mismatches and not stages["camera_scorecard"]["unplanned_source_readers"],
                  "camera outcome or independent raw-reader count contradicted the planned captures")
            report["passed"] = True
        except Exception as exc:
            report["error"] = type(exc).__name__ + ": " + str(exc)
            if started:
                try:
                    report["device_state_at_failure"] = ledger()
                    report["device_records_at_failure"] = local_rows("physical-camera","/captures/device.sqlite",
                                                                     "SELECT op,state,code FROM captures")
                    report["independent_source_at_failure"] = local_rows("physical-camera-source","/witness/source.sqlite",
                                  "SELECT (SELECT count(*) FROM frames),(SELECT count(*) FROM readers),last_seen FROM heartbeat")
                except Exception as diagnostic:
                    report["diagnostic_error"] = str(diagnostic)[:350]
                try:
                    report["tail"] = dc("logs","--no-color","--tail","12","physical-controller","physical-camera",
                                        "physical-verifier","physical-camera-source")[-9000:]
                except Exception:
                    pass
            raise
        finally:
            if started:
                try:
                    dc("down","-v","--remove-orphans","--timeout","3")
                except Exception as exc:
                    report.update(passed=False,teardown_error=str(exc))
            write(output,report)
    check(report["passed"],"inspect physical acceptance evidence")
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out",type=Path,required=True)
    args = parser.parse_args()
    run(args.image,args.out)
