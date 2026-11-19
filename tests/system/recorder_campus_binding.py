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
from urllib.request import ProxyHandler, build_opener
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
            call(shell+["campus","start","future-task","take"])
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
            call(shell+["campus","disable"])
            wait_for(url,409)
            assert call(base+["ps","-q","agent"])==hub
            out.parent.mkdir(parents=True,exist_ok=True)
            out.write_text(json.dumps(dict(passed=True,registered=registered,domain=162,
                                agent_container_preserved=True,registry="existing custom-named volume",
                                image=image),ensure_ascii=False,indent=2)+"\n","utf8")
        finally:
            # Both Compose definitions belong exclusively to the fresh project above.
            subprocess.run(rec+["down","--volumes","--remove-orphans"],cwd=ROOT,env=env,check=False)
            subprocess.run(base+["down","--volumes","--remove-orphans"],cwd=ROOT,env=env,check=False)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",required=True)
    parser.add_argument("--out",required=True,type=Path)
    opts=parser.parse_args()
    run(opts.image,opts.out)
