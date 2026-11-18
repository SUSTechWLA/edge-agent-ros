"""Install both optional author plugins in the production profiles and verify their actual mount boundaries."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
DESIGN = "plugin-design"
SOFTWARE = "software-author"
MODULE = {DESIGN:"plugin.design", SOFTWARE:"plugin.software_author"}
EXPECTED = {
    DESIGN: {"/journal":True,"/private":True,"/decisions":False,"/contracts":False},
    SOFTWARE: {"/journal":True,"/private":True,"/drafts":False,"/contract.json":False},
}

# No model, ROS node, actuator or host port is required for this deployment check.
UNRELATED = """from http.server import BaseHTTPRequestHandler, HTTPServer
class Probe(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.count+=1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(str(self.server.count).encode())
server=HTTPServer(('127.0.0.1',8138),Probe)
server.count=0
server.serve_forever()
"""
TOUCH = """import errno,sqlite3,sys
from pathlib import Path
role=sys.argv[1]
private,journal=Path('/private'),Path('/journal')
assert (private/'intake.sqlite').is_file()
with sqlite3.connect('file:/journal/module-events.sqlite?mode=ro',uri=True) as db:
    identity=db.execute("select value from module_bus_meta where key='source_id'").fetchone()[0]
assert identity
for p in (private/'own-writer',journal/'own-writer'):
    if p.exists():
        assert p.read_text()==role
    p.write_text(role)
if role=='plugin-design':
    assert Path('/decisions/readonly-decision').read_text()=='trusted-fixture'
    (private/'drafts').mkdir(exist_ok=True)
    (private/'drafts/readonly-draft').write_text('design-owned')
    targets=[Path('/decisions/readonly-decision'),Path('/contracts/text-transform.json')]
else:
    assert Path('/drafts/readonly-draft').read_text()=='design-owned'
    targets=[Path('/drafts/readonly-draft'),Path('/contract.json')]
for path in targets:
    assert path.is_file()
    try:
        # Opening an existing file without truncation leaves the fixture intact on a failed test.
        with path.open('r+b'):
            pass
    except OSError as exc:
        assert exc.errno in (errno.EROFS,errno.EACCES,errno.EPERM)
    else:
        raise AssertionError(f'input writable: {path}')
assert not Path('/var/run/docker.sock').exists()
assert not (journal/'neighbor-secret').exists()
print(identity)
"""


def run_command(*cmd, env=None):
    result=subprocess.run(cmd,cwd=ROOT,env=env,capture_output=True,text=True,encoding="utf-8",timeout=90)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-1400:])
    return result.stdout.strip()


def profile(file, image, env, directory):
    project="edge-evolution-mounts-"+uuid.uuid4().hex[:12]
    override=directory/"unrelated.json"
    override.write_text(json.dumps({"services":{"unrelated":{"image":image,"pull_policy":"never","network_mode":"none",
        "entrypoint":["python3","-u","-c",UNRELATED]}}}),encoding="utf-8")
    compose=["docker","compose","-p",project,"-f",str(ROOT/file),"-f",str(override),
             "--profile","plugin-design","--profile","software-author"]
    def call(*args):
        return run_command(*compose,*args,env=env)
    def inspect(name):
        return json.loads(run_command("docker","inspect",call("ps","-a","-q",name)))[0]
    def unrelated(wait=0):
        code="from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8138',timeout=2).read().decode())"
        deadline=time.monotonic()+wait
        while True:
            try:
                return int(call("exec","-T","unrelated","python3","-c",code))
            except RuntimeError as exc:
                if time.monotonic() >= deadline or "Connection refused" not in str(exc):
                    raise
                time.sleep(.2)
    def touch(name):
        deadline=time.monotonic()+20
        while True:
            try:
                return call("exec","-T",name,"python3","-B","-c",TOUCH,name)
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
    try:
        call("up","--no-build","-d","unrelated")
        witness=inspect("unrelated")
        assert unrelated(wait=10)==1
        for service in (DESIGN,SOFTWARE):
            call("up","--no-build","-d",service)
        assert set(call("ps","--all","--services").splitlines())=={
            "unrelated",DESIGN,SOFTWARE,DESIGN+"-init",SOFTWARE+"-init"}
        receipts={}
        seed="""from pathlib import Path
Path('/author/decisions/readonly-decision').write_text('trusted-fixture')
Path('/sources/neighbor-secret').write_text('outside plugin directories')
"""
        helper=["docker","run","--rm","--network","none","--entrypoint","python3"]
        run_command(*helper,"-v",project+"_author-proposals:/author","-v",project+"_module-sources:/sources",image,"-c",seed)
        for service in (DESIGN,SOFTWARE):
            init=inspect(service+"-init")
            assert init["State"]["Status"]=="exited" and init["State"]["ExitCode"]==0
            assert init["HostConfig"]["NetworkMode"]=="none" and init["Config"]["Entrypoint"][:2]==["/bin/mkdir","-p"]
            info=inspect(service)
            config=info["HostConfig"]
            mounts={m["Destination"]:m for m in info["Mounts"]}
            assert {k:v["RW"] for k,v in mounts.items()}==EXPECTED[service]
            assert config["ReadonlyRootfs"] and not config["Privileged"] and not config["Devices"] and not config["DeviceRequests"]
            assert "ALL" in config["CapDrop"] and "no-new-privileges:true" in config["SecurityOpt"]
            options={m["Target"]:m.get("VolumeOptions",{}).get("Subpath") for m in config["Mounts"]}
            assert options["/journal"]==MODULE[service] and mounts["/journal"]["Name"]==project+"_module-sources"
            upstream="/decisions" if service==DESIGN else "/drafts"
            assert options[upstream]==upstream[1:]
            if service==SOFTWARE:
                assert mounts[upstream]["Name"]==receipts[DESIGN]["private_volume"]
                assert mounts["/private"]["Name"]!=receipts[DESIGN]["private_volume"]
            else:
                assert mounts[upstream]["Name"]==project+"_author-proposals"
            receipts[service]={"source_id":touch(service),"private_volume":mounts["/private"]["Name"],
                "actual_mounts":EXPECTED[service],"journal_subpath":options["/journal"],
                "upstream_subpath":options[upstream],"init_exit":init["State"]["ExitCode"],
                "image_id":info["Image"],"devices":config["Devices"],"device_requests":config["DeviceRequests"]}
        for index,service in enumerate((SOFTWARE,DESIGN),2):
            peer=DESIGN if service==SOFTWARE else SOFTWARE
            before=inspect(peer)["State"]["StartedAt"]
            call("stop","--timeout","3",service)
            assert unrelated()==index and inspect(peer)["State"]["StartedAt"]==before
            call("start",service)
            assert touch(service)==receipts[service]["source_id"]
            assert inspect(peer)["State"]["StartedAt"]==before
        assert unrelated()==4
        final=inspect("unrelated")
        assert (final["Id"],final["State"]["StartedAt"])==(witness["Id"],witness["State"]["StartedAt"])
        assert receipts[DESIGN]["source_id"]!=receipts[SOFTWARE]["source_id"]
        readback="""from pathlib import Path
for path,expected in (
    ('/sources/neighbor-secret','outside plugin directories'),
    ('/sources/plugin.design/own-writer','plugin-design'),('/design/own-writer','plugin-design'),
    ('/sources/plugin.software_author/own-writer','software-author'),('/software/own-writer','software-author'),
    ('/design/drafts/readonly-draft','design-owned'),('/author/decisions/readonly-decision','trusted-fixture')):
    assert Path(path).read_text()==expected,path
"""
        run_command(*helper,"-v",project+"_author-proposals:/author:ro","-v",project+"_module-sources:/sources:ro",
                    "-v",receipts[DESIGN]["private_volume"]+":/design:ro",
                    "-v",receipts[SOFTWARE]["private_volume"]+":/software:ro",image,"-c",readback)
        return {"passed":True,"compose_file":file,"project":project,"services":receipts,
                "independent_requests":4,"unchanged_unrelated_instance":True,"volume_readback_verified":True}
    finally:
        call("down","--volumes","--remove-orphans","--timeout","3")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",required=True)
    parser.add_argument("--hardware-image",required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    env={k:v for k,v in os.environ.items() if not k.startswith(("EDGE_","COMPOSE_"))}
    env.update(COMPOSE_DISABLE_ENV_FILE="1",EDGE_IMAGE=args.image,EDGE_HARDWARE_IMAGE=args.hardware_image,
               EDGE_CONSOLE_PORT="1",EDGE_DESIGN_MODEL_URL="http://127.0.0.1:1")
    report={"passed":False,"engine":run_command("docker","version","--format","{{.Server.Version}}"),
            "compose":run_command("docker","compose","version","--short"),"cases":[]}
    try:
        with tempfile.TemporaryDirectory(prefix="edge-evolution-mounts-") as work:
            for f,image in (("compose.yaml",args.image),("compose.hardware.yaml",args.hardware_image)):
                report["cases"].append(profile(f,image,env,Path(work)))
        report["passed"]=True
    finally:
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
