"""Build actual deploy/robot Git blobs, observe without actuators, and verify source provenance."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from release_robot import entries, projection


def git(*args):
    return subprocess.check_output(["git",*args],cwd=ROOT,text=True).strip()


def verify(ref, directory, platform, ros_base):
    directory.mkdir(parents=True,exist_ok=False)
    directory = directory.resolve()
    revision = git("rev-parse","--verify","--end-of-options",ref+"^{commit}")
    source = git("show",revision+":ROBOT_SOURCE")
    actual = entries(revision)
    actual.pop("ROBOT_SOURCE")
    if actual != projection(source):
        raise ValueError("deployment differs from the original source projection")
    required = {"scripts/robot.sh","scripts/check_board_plugins.py","compose.physical-camera.yaml",
                "profiles/bench-camera.json","capabilities/camera/pack.json"}
    if required-actual.keys() or any(p.startswith(("tests/","docs/","sim/","artifacts/")) for p in actual):
        raise ValueError("incomplete board distribution or development fixtures included")
    image = "edge-agent-ros/hardware:projection-"+source[:12]+("-"+platform.split("/")[-1] if platform else "")
    env = {k:v for k,v in os.environ.items() if not k.startswith(("COMPOSE_","EDGE_"))}
    env.update(COMPOSE_DISABLE_ENV_FILE="1",COMPOSE_PROJECT_NAME="edge-test-release-"+uuid.uuid4().hex[:10],
               EDGE_HARDWARE_IMAGE=image,EDGE_REVISION=source,EDGE_ROS_DOMAIN_ID="194")
    if platform:
        env["DOCKER_DEFAULT_PLATFORM"] = platform
    if ros_base:
        env["EDGE_ROS_BASE"] = ros_base
    result = {"passed":False,"source":source,"deployment":revision,"tree":git("rev-parse",revision+"^{tree}"),
              "runtime_files":len(actual),"image":image,"scope":"isolated Linux containers, synthetic ROS input, no hardware"}
    def run(name,args,cwd):
        with (directory/(name+".log")).open("wb") as log:
            subprocess.run(args,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=1200)
        return (directory/(name+".log")).read_text(encoding="utf-8",errors="replace").strip()
    try:
        with tempfile.TemporaryDirectory(prefix="edge-release-build-") as temp:
            temp = Path(temp)
            archive = temp/"runtime.zip"
            run("archive",["git","archive","--format=zip","--output",str(archive),revision],ROOT)
            folder = temp/"runtime"
            with zipfile.ZipFile(archive) as zipped:
                zipped.extractall(folder)
            compose = ["docker","compose","-f","compose.hardware.yaml"]
            run("build",[*compose,"build","core"],folder)
            info = json.loads(run("inspect",["docker","image","inspect",image],folder))[0]
            result.update(image_id=info["Id"],platform=info["Os"]+"/"+info["Architecture"])
            if platform and result["platform"] != platform:
                raise ValueError("built hardware image has a different CPU architecture")
            if info["Config"]["Labels"].get("org.opencontainers.image.revision") != source:
                raise ValueError("hardware image has a different source revision")
            isolated = temp/"observe.json"
            isolated.write_text(json.dumps({"services":{name:{"network_mode":"none",
                "environment":{"ROS_LOCALHOST_ONLY":"1"}} for name in ("console","perception")}}),encoding="utf-8")
            observe = [*compose,"-f","compose.hardware-observe.yaml","-f",str(isolated)]
            run("compose",[*observe,"config","--quiet"],folder)
            try:
                run("observe-start",[*observe,"up","-d","--no-deps","--no-build","perception","console"],folder)
                running = sorted(run("observe-services",[*observe,"ps","--status","running","--services"],folder).splitlines())
                if running != ["console","perception"]:
                    raise ValueError(f"unexpected services in observe: {running}")
                response = json.loads(run("observe-api",[*observe,"exec","-T","console","python3","-c",PROBE],folder))
                result["observe"] = {**response,"running":running}
            finally:
                run("observe-stop",[*observe,"down","-v","--remove-orphans","--timeout","3"],folder)
            run("board-smoke",[sys.executable,"-B",str(ROOT/"tests/system/board_smoke.py"),"--image",image,
                               "--config-root",str(folder),"--out",str(directory/"board.json")],ROOT)
            result["passed"] = True
    except BaseException as exc:
        result["error"] = str(exc)
        raise
    finally:
        (directory/"summary.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")


PROBE = """import json,time,urllib.request,urllib.error
for _ in range(60):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8765/api/state',timeout=1) as reply:
            state=json.load(reply)
        break
    except OSError:
        time.sleep(.25)
else:
    raise RuntimeError('observe dashboard unavailable')
assert state['task_proxy_mode']=='observe' and state['environment']=='hardware'
try:
    urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8765/agent/tasks',b'{}'),timeout=2)
except urllib.error.HTTPError as ex:
    code=json.load(ex)['code']
    assert ex.code==403 and code=='console.observe_only'
else:
    raise RuntimeError('observe accepted task submission')
print(json.dumps({'mode':state['task_proxy_mode'],'environment':state['environment'],'denied':code}))
"""

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy-ref",default="deploy/robot")
    parser.add_argument("--out",required=True,type=Path)
    parser.add_argument("--platform",choices=("linux/amd64","linux/arm64"),help="target for the board image and observe")
    parser.add_argument("--ros-base",help="approved platform-specific ROS base image")
    args=parser.parse_args()
    verify(args.deploy_ref,args.out,args.platform,args.ros_base)
