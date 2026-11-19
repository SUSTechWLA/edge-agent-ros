"""SDD-045 AC8. Exercise real site locks, privileged boundaries and read-only board facts without hardware access."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


ROOT=Path(__file__).resolve().parents[2]
PROBE="""
from http.client import HTTPConnection
import json,socket,time
def read(method,path,body=None):
    c=HTTPConnection('soft-gateway',8129,timeout=4)
    try:
        c.request(method,path,body=body)
        r=c.getresponse()
        return r.status,r.read(131072)
    finally: c.close()
for _ in range(40):
    try:
        status,raw=read('GET','/api/events?limit=1')
        if status==200 and isinstance(json.loads(raw)['events'],list): break
    except (OSError,ValueError): pass
    time.sleep(.3)
else: raise RuntimeError('board facts bridge unavailable')
denied,_=read('POST','/agent/tasks',b'{}')
assert denied==403
try: peer=socket.getaddrinfo('console',8765)
except socket.gaierror: peer=[]
assert not peer
print(json.dumps({'public_events_http':status,'task_submit_http':denied,'direct_console_dns':bool(peer)}))
"""


def run(image,base,out):
    if out.exists():
        raise FileExistsError(f"preserve earlier evidence: {out}")
    base=base.resolve()
    out.parent.mkdir(parents=True,exist_ok=True)
    project="edge-test-board-pack-"+uuid.uuid4().hex[:10]
    env={k:v for k,v in os.environ.items() if not k.startswith(("EDGE_","COMPOSE_"))}
    env.update(COMPOSE_PROJECT_NAME=project,COMPOSE_DISABLE_ENV_FILE="1")
    report={"passed":False,"project":project,"stages":{}}
    def call(*args,timeout=150):
        p=subprocess.run(args,cwd=base,env=env,capture_output=True,encoding="utf8",errors="replace",timeout=timeout)
        if p.returncode:
            raise RuntimeError(f"{args[:4]} ({p.returncode}) {p.stderr[-1600:]} {p.stdout[-350:]}")
        return p.stdout.strip()
    def py(script,*args):
        return call(sys.executable,"-B",script,*map(str,args))

    with tempfile.TemporaryDirectory(prefix=project+"-") as tmp:
        folder=Path(tmp)
        safe=folder/"isolate.json"
        safe.write_text(json.dumps({"services":{k:{"network_mode":"none","environment":{"ROS_LOCALHOST_ONLY":"1"}}
                                       for k in ("perception","console")}}),encoding="utf8")
        def files(track):
            files=["compose.hardware.yaml"]
            files+=["compose.soft-runtime.yaml","compose.soft-runtime-hardware.yaml","compose.hardware-observe.yaml"] if track=="soft" else ["compose.physical-camera.yaml"]
            return [v for file in [*files,str(safe)] for v in ("-f",file)]
        def dc(track,*args):
            return call("docker","compose",*files(track),*args)
        def guard(track,*args):
            return json.loads(py("scripts/check_board_plugins.py","--track",track,"--image",image,"--source",release,
                                "--lock",lock,"--site",site,*files(track),*args))
        stages=report["stages"]
        started=set()
        try:
            info=json.loads(call("docker","image","inspect",image))[0]
            release=info["Config"]["Labels"]["org.opencontainers.image.revision"]
            source=base/"ROBOT_SOURCE"
            if source.exists() and source.read_text().strip()!=release:
                raise ValueError("projection and deployment image source mismatch")
            camera=folder/"camera-approval"
            site=folder/"resolved-site"
            manifest=json.loads(call("docker","image","inspect","--platform",info["Os"]+"/"+info["Architecture"],info["Id"]))[0]["Descriptor"]["digest"]
            py("scripts/prepare_physical_camera.py","--image",image,"--source-commit",release,"--site",camera)
            result=json.loads(py("scripts/prepare_soft_pilots.py","--site",site,"--source-commit",release,
                                 "--image-manifest",camera/"trust/image.json","--image-digest",manifest,
                                 "--profile","profiles/bench-camera.json","--physical-site",camera))
            lock=result["lock"]
            report.update(source_commit=release,image_id=info["Id"],platform_digest=manifest,lock=lock,
                          projection=source.exists(),resolved=result["plugins"])
            env.update(EDGE_HARDWARE_IMAGE=info["Id"],EDGE_SOFT_IMAGE=info["Id"],EDGE_SOFT_PLATFORM_DIGEST=manifest,
                       EDGE_PHYSICAL_IMAGE=info["Id"],EDGE_PHYSICAL_PLATFORM_DIGEST=manifest,EDGE_SOFT_SITE=str(site),
                       EDGE_PHYSICAL_SITE=str(site/"physical"),EDGE_PACK_LOCK=lock)
            stages["soft_preflight"]=guard("soft")
            stages["physical_preflight"]=guard("physical")
            started.add("soft")
            activate=json.loads(dc("soft","run","--rm","-T","--no-deps","--entrypoint","python3","soft-admin",
                                   "-B","/opt/edge/agent/site_pack.py","activate","--root","/registry","--site","/site","--expect-lock",lock))
            assert len(activate["admitted"])==5 and activate["requires_deployment"]==["camera.capture"],activate
            stages["pack_admission"]=activate
            dc("soft","up","-d","--no-build","perception","console","soft-facts","soft-host","soft-gateway","evidence","session")
            stages["runtime"]=guard("soft","--running")
            stages["public_facts"]=json.loads(dc("soft","exec","-T","evidence","python3","-Bc",PROBE))
            started.add("physical")
            dc("physical","run","--rm","-T","--no-deps","physical-admin","install")
            dc("physical","up","-d","--no-build","physical-controller","physical-camera","physical-verifier")
            stages["camera_runtime"]=guard("physical","--running")
            status=json.loads(dc("physical","run","--rm","-T","--no-deps","physical-admin","status"))
            assert status["phase"]=="ACTIVE" and not status["operations"],status
            stages["physical"]={"phase":status["phase"],"recorded_operations":len(status["operations"]),
                                "site_driver_present":False,"physical_requests":0}
            dc("physical","run","--rm","-T","--no-deps","physical-admin","drain")
            try:
                dc("physical","run","--rm","-T","--no-deps","physical-admin","release")
            except RuntimeError as error:
                if "plugin.stop_unconfirmed" not in str(error):
                    raise
                stages["physical"]["release_without_witness"]="plugin.stop_unconfirmed"
            else:
                raise AssertionError("missing external witness allowed field release")
            stages["physical"]["teardown"]="disposable isolated test namespace; field handoff remains unconfirmed"
            report["passed"]=True
        except BaseException as exc:
            report["error"]=str(exc)
            raise
        finally:
            filelist=["compose.hardware.yaml","compose.soft-runtime.yaml","compose.soft-runtime-hardware.yaml",
                      "compose.hardware-observe.yaml","compose.physical-camera.yaml",str(safe)]
            if started:
                try:
                    call("docker","compose",*(v for f in filelist for v in ("-f",f)),"down","-v","--remove-orphans","--timeout","2")
                except Exception as exc:
                    report["passed"]=False
                    report["cleanup_error"]=str(exc)
            out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    if not report["passed"]:
        raise RuntimeError("deployment check failed: "+str(out))
    print(out)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",required=True)
    parser.add_argument("--root",type=Path,default=ROOT,help="generated deployment checkout")
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    run(args.image,args.root,args.out)
