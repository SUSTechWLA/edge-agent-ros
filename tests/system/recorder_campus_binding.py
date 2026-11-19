"""Attach the recorder to a running campus-style hub that owns a named plugin volume."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]


def run(image, out):
    project="campus-record-test-"+uuid4().hex[:8]
    # Keep the standard service label but give the registry a deliberately non-default name.
    env={k:v for k,v in os.environ.items() if not k.startswith(("COMPOSE_","EDGE_"))}
    env.update(COMPOSE_PROJECT_NAME=project,COMPOSE_DISABLE_ENV_FILE="1",EDGE_RECORDING_PORT="0",
               EDGE_RECORD_SOFTWARE_ONLY="1")
    shell=["sh","scripts/record-task.sh"] if os.name!="nt" else [
        shutil.which("pwsh") or "powershell","-NoProfile","-File","scripts/record-task.ps1"]
    local=build_opener(ProxyHandler({}))
    with tempfile.TemporaryDirectory() as temp:
        spec=Path(temp)/"compose.json"
        data={"services":{"agent":{"image":image,"ports":["127.0.0.1::8090"],"pull_policy":"never",
               "environment":{"EDGE_EXTRA_PLUGIN_DIR":"","ROS_DOMAIN_ID":"162"},"command":["python3","/opt/edge/agent/service.py"],
               "volumes":["registry:/etc/edge/plugins:ro"]}},"volumes":{"registry":{"name":project+"-custom"}}}
        spec.write_text(json.dumps(data),"utf8")
        base=["docker","compose","-p",project,"-f",str(spec)]
        def call(args):
            return subprocess.check_output(args,cwd=ROOT,env=env,text=True,encoding="utf8",errors="replace",timeout=75).strip()
        def lookup(url):
            try:
                with local.open(url+"/agent/services?name=task.record",timeout=3) as stream:
                    return stream.status,json.load(stream)
            except HTTPError as e:
                return e.code,{}
            except (URLError,TimeoutError,ConnectionError):
                return 0,{}
        def wait_for(url,code):
            for _ in range(80):
                status,details=lookup(url)
                if status==code: return details
                time.sleep(.25)
            raise RuntimeError(f"Agent service lookup returned {status}, expected {code}")
        rec=["docker","compose","-p",project,"-f","compose.yaml","-f","compose.task-recorder.yaml","--profile","recording"]
        try:
            call(base+["up","-d","--no-build","agent"])
            hub=call(base+["ps","-q","agent"])
            url="http://"+call(base+["port","agent","8090"])
            wait_for(url,409)
            assert json.loads(call(shell+["campus","start","future-task","take"]))["status"]=="ACTIVE"
            registered=wait_for(url,200)
            assert "task-recorder:8188" in json.dumps(registered)
            # Confirm the deployed source inherited the existing hub's actual ROS domain and image.
            collector="edge-rec-"+project+"-take"
            domain=call(["docker","inspect","--format",
                         '{{range .Config.Env}}{{if eq (index (split . "=") 0) "ROS_DOMAIN_ID"}}{{println .}}{{end}}{{end}}',collector])
            assert domain=="ROS_DOMAIN_ID=162"
            assert call(["docker","inspect","--format","{{.Config.Image}}",collector])==image
            assert json.loads(call(shell+["campus","status","take"]))["status"]=="ACTIVE"
            call(shell+["campus","stop","take"])
            # Reproduce a running collector from before the optional overview label existed.
            rid="legacy"
            old_name=f"edge-rec-{project}-{rid}"
            call(rec+["exec","-T","task-recorder","python3","-m","task_recorder.cli","start",rid,
                      "--task-id","old-task","--sources","agent_state","agent_trace"])
            old=call(rec+["run","-d","--no-deps","--rm","--label","edge.recorder.camera=",
                          "--label","edge.recorder.signals=","--name",old_name,"recorder-source","sleep","90"])
            for _ in range(2):
                assert json.loads(call(shell+["campus","start","old-task",rid]))["status"]=="ACTIVE"
                assert call(["docker","inspect","--format","{{.Id}}",old_name])==old
            call(shell+["campus","stop",rid])
            call(["docker","stop","-t","1",old_name])
            # Select two actual transports via the normal operator entry point. No Gazebo physics needed.
            env["EDGE_RECORD_SOFTWARE_ONLY"]="0"
            data["services"]["sim"]=dict(image=image,pull_policy="never",environment={"ROS_DOMAIN_ID":"162",
                       "GZ_PARTITION":project,"RMW_IMPLEMENTATION":"rmw_cyclonedds_cpp"},
                       volumes=[{"type":"bind","source":str(ROOT/"tests/system/recording_camera_fixture.py"),
                                 "target":"/fixture.py","read_only":True}],command=["python3","-B","/fixture.py"])
            spec.write_text(json.dumps(data),"utf8")
            call(base+["up","-d","--no-build","sim"])
            both=["campus","start","camera-task","both","--camera","/fixture/onboard","--overview","/fixture/overview",
                  "--clock-domain","operator_fixture_ns"]
            call(shell+both)
            recorder_url="http://"+call(rec+["port","task-recorder","8188"])
            for _ in range(80):
                with local.open(recorder_url+"/recordings/both",timeout=4) as result: summary=json.load(result)
                if all(summary["counts"].get(k,0)>=2 for k in ("rgb","overview")): break
                time.sleep(.25)
            else: raise AssertionError("missing operator camera streams: "+repr(summary["counts"]))
            active=f"edge-rec-{project}-both"
            camera_source=call(["docker","inspect","--format","{{.Id}}",active])
            call(shell+both)
            assert call(["docker","inspect","--format","{{.Id}}",active])==camera_source
            call(shell+["campus","stop","both"])
            records=call(rec+["exec","-T","task-recorder","cat","/recordings/both/media/mission/timeline.jsonl"])
            for kind,role,topic in (("rgb","onboard","/fixture/onboard"),("overview","overview","/fixture/overview")):
                frames=[r for r in map(json.loads,records.splitlines()) if r["kind"]==kind]
                assert len({r["sha256"] for r in frames})>=2
                assert all(r["image_role"]==role and r["topic"]==topic and r["file"].startswith(kind+"/") for r in frames)
                assert all(r["clock_domain"]=="operator_fixture_ns" and r["stamp_origin"]=="header" for r in frames)
            # A separately authorized producer may send an overview without any Gazebo connection.
            call(shell+["campus","start","external-task","field","--external-overview"])
            external=f"edge-rec-{project}-field"
            argv=json.loads(call(["docker","inspect","--format","{{json .Config.Cmd}}",external]))
            assert "--camera" not in argv and "--overview" not in argv
            assert "overview" in json.loads(call(shell+["campus","status","field"]))["sources"]
            original=next(r for r in map(json.loads,records.splitlines()) if r["kind"]=="overview")
            payload=call(rec+["exec","-T","task-recorder","base64","-w","0","/recordings/both/media/mission/"+original["file"]])
            event=dict(task_id="external-task",kind="overview",event_id="authorized-observer-sample",
                       topic="/field/entrance",source="fixture.external",image_role="overview",
                       stamp_ns=original["stamp_ns"],clock_domain="fixture_observer",stamp_origin="header",
                       width=original["width"],height=original["height"],media=dict(name="overview/1.png",base64=payload))
            for duplicate in (False,True):
                req=Request(recorder_url+"/recordings/field/events",data=json.dumps(event).encode(),
                            headers={"Content-Type":"application/json"})
                with local.open(req,timeout=4) as receipt: assert json.load(receipt)["duplicate"]==duplicate
            call(shell+["campus","stop","field"])
            assert json.loads(call(shell+["campus","status","field"]))["counts"]["overview"]==1
            call(shell+["campus","disable"])
            wait_for(url,409)
            assert call(base+["ps","-q","agent"])==hub
            out.parent.mkdir(parents=True,exist_ok=True)
            out.write_text(json.dumps(dict(passed=True,registered=registered,domain=162,
                                agent_container_preserved=True,registry="existing custom-named volume",
                                image=image,legacy_id_preserved=True,dual_camera_frames=summary["counts"],
                                external_subscription_added=False),ensure_ascii=False,indent=2)+"\n","utf8")
        finally:
            owned=call(["docker","ps","-q","--filter","label=com.docker.compose.project="+project,
                        "--filter","label=com.docker.compose.service=recorder-source"]).splitlines()
            if owned:
                subprocess.run(["docker","stop","-t","2",*owned],check=False,capture_output=True)
            # Both Compose definitions belong exclusively to the fresh project above.
            subprocess.run(rec+["down","--volumes","--remove-orphans"],cwd=ROOT,env=env,check=False)
            subprocess.run(base+["down","--volumes","--remove-orphans"],cwd=ROOT,env=env,check=False)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",required=True)
    parser.add_argument("--out",required=True,type=Path)
    opts=parser.parse_args()
    run(opts.image,opts.out)
