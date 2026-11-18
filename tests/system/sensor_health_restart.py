"""Exercise Docker's actual restart policy and a mounted incident journal on private loopback DDS."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler
import uuid

PROVIDER="/opt/edge/perception/sensor_health/provider.py"
PROBE="/tmp/test-sensor-health-restart.py"
TOPIC="/restart_test/rgb"


def probe(action):
    import sqlite3
    http=build_opener(ProxyHandler({}))
    def status():
        with http.open("http://127.0.0.1:18136/status",timeout=2) as r:
            return json.load(r)["inputs"]["rgb"]
    if action=="events":
        with sqlite3.connect("file:/journal/module-events.sqlite?mode=ro",uri=True) as db:
            return [json.loads(r[0]) for r in db.execute("SELECT event FROM module_bus_events ORDER BY seq")]
    if action=="status":
        return status()
    if action=="crash":
        # Docker's init is PID 1. Kill its workload from inside the isolated namespace.
        # A Docker stop/kill API request would test an operator command instead.
        found=[]
        for path in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                args=path.read_bytes().rstrip(b"\x00").split(b"\x00")
                # Fields following the final ')' are state and PPID. The guest's
                # argv[0] can identify QEMU even when comm identifies Python.
                ppid=int((path.parent/"stat").read_text().rsplit(")",1)[1].split()[1])
                comm=(path.parent/"comm").read_text().strip()
            except (FileNotFoundError,ProcessLookupError): continue
            if ppid==1 and comm=="python3" and args[-2:]==[b"-B",PROVIDER.encode()]:
                found.append(dict(pid=int(path.parent.name),parent_pid=ppid,comm=comm,
                                  argv=[os.fsdecode(a) for a in args],signal="SIGKILL"))
        assert len(found)==1 and found[0]["pid"]!=1,found
        # PID 1 can exit immediately after SIGKILL, taking this exec process with it.
        print(json.dumps(found[0]),flush=True)
        os.kill(found[0]["pid"],signal.SIGKILL)
        sys.exit(0)
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image
    rclpy.init()
    node=rclpy.create_node("sensor_restart_fixture")
    pub=node.create_publisher(Image,TOPIC,qos_profile_sensor_data)
    rows=[]
    try:
        deadline=time.monotonic()+6
        while pub.get_subscription_count()==0 and time.monotonic()<deadline: time.sleep(.05)
        assert pub.get_subscription_count()>0
        time.sleep(.5)  # Both sides must discover the BEST_EFFORT data path, including our new writer.
        # Exactly one emitted good frame per step, waiting for its own read-back before the next one.
        count={"normal":1,"black":6,"three":3,"two":2}[action]
        for _ in range(count):
            msg=Image()
            msg.header.frame_id="front_optical"
            msg.header.stamp=node.get_clock().now().to_msg()
            msg.width=msg.height=16
            msg.encoding="mono8"
            msg.step=16
            msg.data=bytes(256) if action=="black" else bytes([30,200])*128
            stamp=msg.header.stamp.sec*10**9+msg.header.stamp.nanosec
            sent=0
            limit=time.monotonic()+4
            while time.monotonic()<limit:
                value=status()
                if value["latest"].get("stamp_ns")==stamp:break
                if time.monotonic()-sent>.35:
                    pub.publish(msg)  # Retransmission has the same ROS stamp; it cannot count as a different sample.
                    sent=time.monotonic()
                time.sleep(.025)
            assert value["latest"].get("stamp_ns")==stamp,(action,value)
            rows.append({k:value[k] for k in ("state","code","consecutive_good","event_id")}|{"stamp_ns":stamp})
            time.sleep(.14)
        return rows
    finally:
        node.destroy_node()
        rclpy.shutdown()


def verify(image,out,source_dir=None):
    suffix=uuid.uuid4().hex[:11]
    name="sensor-restart-"+suffix
    volume=name+"-journal"
    def docker(*args,check=True):
        p=subprocess.run(["docker",*args],capture_output=True,text=True,encoding="utf-8",timeout=50)
        if check and p.returncode:raise RuntimeError(f"{args[:2]}: {p.stderr[-1500:]} {p.stdout[-500:]}")
        return p
    def call(action,check=True):
        prefix=[] if action in {"status","events","crash"} else ["/usr/local/bin/edge-entrypoint"]
        p=docker("exec",name,*prefix,"python3","-B",PROBE,"--probe",action,check=check)
        if action=="crash":
            if p.returncode not in (0,137) or not p.stdout.strip():
                raise RuntimeError(f"crash injection failed ({p.returncode}): {p.stderr[-600:]}")
            return json.loads(p.stdout)
        return json.loads(p.stdout) if p.returncode==0 and p.stdout.strip() else None
    def inspect():
        obj=json.loads(docker("inspect",name).stdout)[0]
        return {"id":obj["Id"],"boot":obj["State"]["StartedAt"],"running":obj["State"]["Running"],
                "restart_count":obj["RestartCount"],"policy":obj["HostConfig"]["RestartPolicy"]["Name"]}
    def start(robot):
        mounts=["--mount","type=volume,source="+volume+",target=/journal","--mount",
                "type=bind,source="+str(Path(__file__).resolve())+",target="+PROBE+",readonly"]
        if source_dir:
            mounts+=["--mount","type=bind,source="+str(source_dir.resolve())+",target=/opt/edge/perception/sensor_health,readonly"]
        docker("run","-d","--name",name,"--init","--restart","unless-stopped","--network","none",
               "--read-only","--tmpfs","/tmp",*mounts,"-e","ROS_LOCALHOST_ONLY=1","-e","RMW_IMPLEMENTATION=rmw_cyclonedds_cpp",
               "-e","ROS_LOG_DIR=/tmp/ros","-e","ROS_DOMAIN_ID=186","-e","EDGE_HEALTH_RGB_TOPIC="+TOPIC,
               "-e","EDGE_HEALTH_RGB_FRAME=front_optical","-e","EDGE_HEALTH_ROBOT_ID="+robot,
               "-e","EDGE_HEALTH_RECHECK_S=.4","-e","EDGE_HEALTH_TIMEOUT_S=45","-e","EDGE_HEALTH_PORT=18136",
               image,"python3","-B",PROVIDER)
    def await_start(min_restarts):
        end=time.monotonic()+35
        while time.monotonic()<end:
            current=inspect()
            if current["running"] and current["restart_count"]>=min_restarts:
                current_state=call("status",False)
                if current_state:return current,current_state
            time.sleep(.15)
        raise AssertionError("no running observer: "+docker("logs",name,check=False).stderr[-800:])
    report={"passed":False,"image":image,"image_id":docker("image","inspect",image,"--format","{{.Id}}").stdout.strip(),
            "provider_source":"checkout mount" if source_dir else "packaged image","container":name,
            "policy":"unless-stopped","private_ros_domain":186}
    try:
        docker("volume","create",volume)
        start("robot-a")
        before,initial=await_start(0)
        assert before["policy"]=="unless-stopped" and initial["state"]=="waiting"
        report["initial"]=call("normal")[0]
        assert report["initial"]["state"]=="observed"
        open_alarm=call("black")[-1]
        assert open_alarm["state"]=="needs_attention"
        prior_good=call("two")
        assert [x["consecutive_good"] for x in prior_good]==[1,2]
        report.update(before=before,original_alarm=open_alarm,before_crash_good=prior_good)
        report["crash_injection"]=call("crash",False)
        boot,alarm=await_start(before["restart_count"]+1)
        assert boot["id"]==before["id"] and boot["boot"]!=before["boot"]
        assert alarm["state"]=="needs_attention" and alarm["consecutive_good"]==0 and alarm["latest"]=={}
        chain=call("events")
        resumed=next(e for e in chain if e["event_id"]==alarm["event_id"])
        assert resumed["kind"]=="sensor.incident_resumed" and resumed["parent_event_id"]==open_alarm["event_id"]
        assert resumed["data"]["observation_gap"]=="unobserved"
        after_bad=call("black")[-1]
        assert after_bad["state"]=="needs_attention" and after_bad["consecutive_good"]==0
        after=call("three")
        assert [x["state"] for x in after]==["needs_attention","needs_attention","recovered"]
        assert [x["consecutive_good"] for x in after]==[1,2,3]
        assert len({v["stamp_ns"] for v in after})==3
        closed=next(e for e in call("events") if e["event_id"]==after[-1]["event_id"])
        assert closed["parent_event_id"]==resumed["event_id"]
        report.update(after_auto_restart=boot,resumed_event=resumed,continued_bad=after_bad,three_fresh=after,
                      recovery_event=closed)
        report["closed_crash_injection"]=call("crash",False)
        post,zero=await_start(boot["restart_count"]+1)
        assert zero["state"]=="waiting" and call("normal")[0]["state"]=="observed"
        old=call("black")[-1]
        assert old["state"]=="needs_attention"
        docker("stop","--time","3",name)
        stopped=inspect()
        time.sleep(3)
        assert inspect()==stopped and not stopped["running"] and stopped["restart_count"]==post["restart_count"]
        report.update(closed_did_not_reopen=True,operator_stopped=stopped,stop_observation_s=3)
        docker("rm",name)
        start("robot-b")
        _,new=await_start(0)
        assert new["state"]=="waiting" and call("normal")[0]["state"]=="observed"
        detached=next(e for e in call("events") if e["kind"]=="sensor.source_detached" and e["parent_event_id"]==old["event_id"])
        assert detached["data"]["prior_identity"]["robot"]=="robot-a" and detached["data"]["current_identity"]["robot"]=="robot-b"
        report.update(new_identity=detached,passed=True)
    except Exception as error:
        report["error"]=f"{type(error).__name__}: {error}"
        raise
    finally:
        docker("rm","-f",name,check=False)
        docker("volume","rm",volume,check=False)
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)+"\n","utf-8")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--probe")
    p.add_argument("--image")
    p.add_argument("--out",type=Path)
    p.add_argument("--source-dir",type=Path)
    args=p.parse_args()
    if args.probe:print(json.dumps(probe(args.probe)))
    elif args.image and args.out:verify(args.image,args.out,args.source_dir)
    else:p.error("provide --image and --out")
