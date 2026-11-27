"""Exercise the operator commands in a fresh Docker project with fixture Agent and ROS inputs."""
import argparse
import json
import os
from pathlib import Path
import random
import shutil
import socket
import subprocess
import time
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]

# This fixture only publishes observations. Its Agent returns a terminal state when the test opens the gate.
FIXTURE = """
import os,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlsplit
import json
import rclpy
from sensor_msgs.msg import Image
from rosgraph_msgs.msg import Clock
class Agent(BaseHTTPRequestHandler):
    def do_GET(self):
        q=parse_qs(urlsplit(self.path).query)
        task=q["id"][0]
        done=task=="task-b" and os.path.exists("/tmp/finish-b")
        result=dict(id=task,state="SUCCEEDED" if done else "RUNNING",steps=[],cursor=0)
        if done:
            after=int(q.get("after",[0])[0]); limit=int(q["limit"][0])
            rows=[dict(seq=n,task_id=task,kind="tool.response",step=0,
                       operation_id=task+"/step/1",utc=f"2026-11-19T00:00:{n:02d}+00:00",
                       payload={"trace":{"layer":"tool","module":"check.fixture","outcome":"SUCCEEDED"}})
                  for n in range(1,34) if n>after]
            result.update(events=rows[:limit],events_has_more=len(rows)>limit)
        body=json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*a): pass
threading.Thread(target=ThreadingHTTPServer(("",8090),Agent).serve_forever,daemon=True).start()
rclpy.init()
node=rclpy.create_node("recording_fixture")
clock=node.create_publisher(Clock,"/clock",10)
camera=node.create_publisher(Image,"/fixture/rgb",10)
while True:
    stamp=node.get_clock().now().to_msg()
    c=Clock(); c.clock=stamp; clock.publish(c)
    m=Image(); m.header.stamp=stamp; m.height=2; m.width=2; m.step=6
    m.encoding="rgb8"; m.data=[0,192,64]*4; camera.publish(m)
    time.sleep(.25)
"""


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",required=True)
    parser.add_argument("--output",type=Path,required=True,help="new directory for this run")
    parser.add_argument("--sh",default=shutil.which("sh") or "sh")
    args=parser.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    plugins=output/"plugins"
    plugins.mkdir()
    project="recops-"+uuid4().hex[:10]
    fixture=project+"-fixture"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    domain=str(random.SystemRandom().randint(100,170))
    env={**os.environ,"COMPOSE_PROJECT_NAME":project,"COMPOSE_PROFILES":"","EDGE_IMAGE":args.image,
         "EDGE_RECORDER_IMAGE":args.image,"EDGE_ROS_DOMAIN_ID":domain,"EDGE_RECORDING_PORT":str(port),
         "EDGE_PLUGINS_DIR":plugins.as_posix(),"EDGE_RECORDING_STORAGE":"recording-data",
         "EDGE_RECORD_AGENT_URL":"http://fixture-agent:8090","EDGE_RECORD_CAMERA":"",
         "EDGE_RECORD_SOFTWARE_ONLY":"0"}
    archive=f"http://127.0.0.1:{port}"
    http=build_opener(ProxyHandler({}))
    cases={}
    def run(argv,ok=True,extra=None):
        p=subprocess.run(argv,cwd=ROOT,env={**env,**(extra or {})},capture_output=True,text=True,encoding="utf8",
                         errors="replace",timeout=100)
        if (p.returncode==0)!=ok:
            raise RuntimeError(p.stdout[-1800:]+" "+p.stderr[-1800:])
        return p.stdout.strip()
    def shell(*params,ok=True,extra=None):
        return run([args.sh,"scripts/record-task.sh","sim",*params],ok,extra)
    def poll(test,timeout=35):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if test():
                return
            time.sleep(.3)
        raise TimeoutError("condition not met")
    def summary(rid):
        with http.open(archive+"/recordings/"+rid,timeout=3) as stream:
            return json.load(stream)
    def mark(name,value=True):
        cases[name]=value
        print(name,flush=True)
    compose=["docker","compose","-f","compose.yaml","-f","compose/task-recorder.yaml"]
    try:
        default=run(compose+["config","--services"]).splitlines()
        assert not any("recorder" in s for s in default)
        mark("default_disabled")
        shell("enable")
        descriptor=plugins/"runtime/recorder.default.json"
        assert json.loads(descriptor.read_text())["provides"]==["task.record"]
        run(["docker","run","-d","--name",fixture,"--network",project+"_default","--network-alias","fixture-agent",
             "-e","ROS_DOMAIN_ID="+domain,"-e","RMW_IMPLEMENTATION=rmw_cyclonedds_cpp",args.image,"python3","-c",FIXTURE])
        mark("managed_service_published")
        from_peer = """from urllib.error import HTTPError
from urllib.request import ProxyHandler,Request,build_opener
try:
    build_opener(ProxyHandler({})).open(Request("http://task-recorder:8188/admin/drain",data=b"{}"),timeout=3)
except HTTPError as error:
    assert error.code == 403
else:
    raise AssertionError("peer changed admission")
"""
        run(["docker","exec",fixture,"python3","-c",from_peer])
        mark("peer_cannot_change_admission")
        sw={"EDGE_RECORD_SOFTWARE_ONLY":"1"}
        rgb={"EDGE_RECORD_CAMERA":"/fixture/rgb"}
        shell("start","task-a","rec-a",extra=sw)
        shell("start","task-b","rec-b",extra=rgb)
        shell("start","task-c","rec-c")
        poll(lambda: summary("rec-b")["counts"].get("rgb",0)>0 and summary("rec-c")["counts"].get("clock",0)>0)
        assert summary("rec-a")["sources"]==["agent_state","agent_trace"] and "rgb" not in summary("rec-c")["sources"]
        assert "agent_trace" in summary("rec-a")["missing_sources"]
        mark("software_ros_and_selected_camera_capture")
        if os.name=="nt":
            ps=shutil.which("pwsh.exe") or shutil.which("powershell.exe")
            status=run([ps,"-NoProfile","-File",str(ROOT/"scripts/record-task.ps1"),"sim","status","rec-a"])
            assert json.loads(status)["recording_id"]=="rec-a"
            mark("windows_powershell_entry_verified")
        shell("disable",ok=False)
        assert json.loads(descriptor.read_text())["draining"]
        shell("start","denied","rec-d",ok=False,extra=sw)
        shell("start","task-b","rec-b",extra=rgb)
        shell("start","task-b","rec-b",ok=False,extra={"EDGE_RECORD_CAMERA":"/other/rgb"})
        assert summary("rec-b")["status"]=="ACTIVE"
        mark("drain_rejects_new_preserves_inflight_and_idempotent_start")
        shell("stop","rec-a")
        shell("stop","rec-c")
        assert summary("rec-a")["task_status"]=="STOPPED" and summary("rec-b")["status"]=="ACTIVE"
        mark("per_task_manual_stop")
        run(["docker","exec",fixture,"touch","/tmp/finish-b"])
        poll(lambda: summary("rec-b")["task_status"]=="SUCCEEDED")
        assert summary("rec-b")["counts"]["agent_trace"]==33
        mark("watcher_observed_terminal",summary("rec-b")["counts"])
        exp=output/"recording-b"
        shell("export","rec-b",exp.as_posix())
        assert (exp/"recording.json").is_file() and any((exp/"media").rglob("*.png"))
        timeline=exp/"media"/"mission"/"timeline.jsonl"
        traces=[r for r in map(json.loads,timeline.read_text("utf8").splitlines()) if r["kind"]=="agent_trace"]
        assert [r["source_seq"] for r in traces]==list(range(1,34)) and all(r["tool"]=="check.fixture" for r in traces)
        mark("backfilled_two_pages_before_terminal_finish")
        mark("export_includes_manifest_and_real_ros_image")
        shell("disable")
        assert not descriptor.exists() and not run(compose+["ps","-q","task-recorder"])
        shell("start","denied","rec-d",ok=False,extra=sw)
        assert not run(compose+["ps","-q","task-recorder"])
        run(compose+["up","-d","--no-deps","task-recorder"])
        shell("start","denied","rec-d",ok=False,extra=sw)
        assert not descriptor.exists()
        assert summary("rec-b")["task_status"]=="SUCCEEDED"
        shell("enable")
        shell("start","task-e","rec-e",extra=sw)
        shell("stop","rec-e")
        shell("disable")
        mark("drain_persists_restart_explicit_enable_readmits")
    finally:
        (output/"results.json").write_text(json.dumps(dict(image=args.image,fixture="test Agent and ROS RGB/clock",
                                       cases=cases),indent=2)+"\n","utf8")
        subprocess.run(["docker","rm","-f",fixture],capture_output=True)
        subprocess.run(compose+["--profile","recording","--profile","recording-tools","down","-v","--remove-orphans"],
                       env=env,cwd=ROOT,capture_output=True)


if __name__=="__main__":
    main()
