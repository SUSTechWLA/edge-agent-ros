"""Check the actual simulation/hardware Compose profile creates a confined Watchdog journal."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
CONFINE = """
import errno,json,sqlite3
from pathlib import Path
root=Path('/state')
assert (root/'watchdog/module-events.sqlite').is_file()
neighbor=root/'session.default'
assert not neighbor.exists()
try:
    neighbor.mkdir()
except OSError as exc:
    assert exc.errno in (errno.EROFS,errno.EPERM,errno.EACCES)
else:
    raise AssertionError('supervisor can create a sibling mount')
print('confined')
"""
READBACK = """
import sqlite3,sys
from pathlib import Path
assert Path('/state/session.default/sentinel').read_bytes()==b'unchanged'
with sqlite3.connect('file:/state/watchdog/module-events.sqlite?mode=ro',uri=True) as db:
    print(db.execute("SELECT value FROM module_bus_meta WHERE key='source_id'").fetchone()[0])
"""


def checked(*cmd, env=None):
    return subprocess.check_output(cmd, cwd=ROOT, env=env, text=True, encoding="utf-8", timeout=90).strip()


def check_config(file, image, env, directory):
    project = "edge-wdmount-"+uuid.uuid4().hex[:10]
    override=Path(directory)/"isolated.json"
    override.write_text(json.dumps({"services":{"watchdog":{"command":["--agent-url","http://127.0.0.1:1",
                           "--console-url","http://127.0.0.1:1","--grace","60"]}}}),encoding="utf-8")
    compose=["docker","compose","-p",project,"-f",str(ROOT/file),"-f",str(override),"--profile","watchdog"]
    def call(*args):
        return checked(*compose,*args,env=env)
    volume=project+"_module-sources"
    try:
        call("up","-d","watchdog")
        services=set(call("ps","-a","--services").splitlines())
        assert services=={"watchdog","watchdog-init"},services
        inspect=json.loads(checked("docker","inspect",call("ps","-q","watchdog")))[0]
        mount=next(m for m in inspect["HostConfig"]["Mounts"] if m["Target"]=="/state/watchdog")
        assert mount["VolumeOptions"]["Subpath"]=="watchdog" and len(inspect["Mounts"])==1
        assert inspect["HostConfig"]["ReadonlyRootfs"]
        seed="from pathlib import Path;p=Path('/state/session.default');p.mkdir();(p/'sentinel').write_bytes(b'unchanged')"
        helper=["docker","run","--rm","--network","none","--entrypoint","python3","-v"]
        checked(*helper,volume+":/state",image,"-c",seed)
        source_ids=[]
        for attempt in range(2):
            if attempt:
                call("restart","watchdog")
            for _ in range(50):
                code=subprocess.run([*compose,"exec","-T","watchdog","python3","-c",CONFINE],
                                    capture_output=True,env=env,timeout=5)
                if code.returncode==0:
                    break
                time.sleep(.1)
            assert code.returncode==0,code.stderr
            source_ids.append(checked(*helper,volume+":/state:ro",image,"-c",READBACK))
        assert source_ids[0] and source_ids[0]==source_ids[1]
        return {"passed":True,"compose":file,"project":project,"image_id":inspect["Image"],
                "watchdog_subpath":mount["VolumeOptions"]["Subpath"],"services":sorted(services),
                "source_id_before_after_restart":source_ids,"neighbor_visible":False,"neighbor_writable":False}
    finally:
        call("down","--volumes","--remove-orphans","--timeout","2")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--hardware-image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args=parser.parse_args()
    env={k:v for k,v in os.environ.items() if not k.startswith(("EDGE_","COMPOSE_"))}
    env.update(COMPOSE_DISABLE_ENV_FILE="1", EDGE_IMAGE=args.image,EDGE_HARDWARE_IMAGE=args.hardware_image)
    with tempfile.TemporaryDirectory() as directory:
        cases=[check_config(file,image,env,directory) for file,image in (
            ("compose.yaml",args.image),("compose.hardware.yaml",args.hardware_image))]
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps({"passed":True,"compose_version":checked("docker","compose","version","--short"),
                        "engine_version":checked("docker","version","--format","{{.Server.Version}}"),
                        "cases":cases},indent=2)+"\n",encoding="utf-8")
