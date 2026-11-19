"""Run an isolated factory fixture and physical Gazebo rover exploration; observe truth independently."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]


def read_snapshot(path):
    # Docker Desktop can briefly deny a Windows reader while the witness replaces this file.
    deadline = time.monotonic() + 1
    while True:
        try:
            return json.loads(path.read_bytes())
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.025)


def witness(path, record=False):
    import rclpy
    from nav_msgs.msg import Odometry
    from geometry_msgs.msg import Twist
    from rclpy.qos import qos_profile_sensor_data
    rclpy.init()
    node=rclpy.create_node("independent_exploration_witness")
    if record:
        from commission_recording import Recorder
        sys.path.insert(0,str(ROOT))
        recorder=Recorder(node,path.parent)
    data={"first":None,"last":None,"max_displacement_m":0.,"max_linear_command_mps":0.,"samples":0,
          "moving_commands":0,"witness_instance":uuid.uuid4().hex,"closest_pillar_clearance_m":10.0}
    def observed(msg):
        p=msg.pose.pose.position
        xy=[p.x,p.y]
        # Independent geometry oracle for the axis-aligned fixture pillar below.
        clearance=math.hypot(max(0.,abs(p.x-1.35)-.25), max(0.,abs(p.y-1.35)-.25))
        data["closest_pillar_clearance_m"]=min(data["closest_pillar_clearance_m"],clearance)
        data["first"]=data["first"] or xy
        data.update(last=xy,samples=data["samples"]+1,
                    max_displacement_m=max(data["max_displacement_m"],math.dist(xy,data["first"])))
    def commanded(msg):
        data["max_linear_command_mps"]=max(abs(msg.linear.x),data["max_linear_command_mps"])
        data["latest_twist"]=[msg.linear.x,msg.angular.z]
        if max(abs(msg.linear.x),abs(msg.angular.z))>.02:
            data["moving_commands"]+=1
    def write():
        tmp=path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data),encoding="utf-8")
        tmp.replace(path)
    node.create_subscription(Odometry,"/ground_truth",observed,qos_profile_sensor_data)
    node.create_subscription(Twist,"/cmd_vel",commanded,qos_profile_sensor_data)
    node.create_timer(.4,write)
    rclpy.spin(node)


def trial(args):
    out=args.out_dir.resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("output directory contains an earlier run; choose a new directory")
    out.mkdir(parents=True,exist_ok=True)
    token=uuid.uuid4().hex[:12]
    project="commission-"+token
    partition="commission_"+token
    robot="sim-rover-"+token
    sys.path.insert(0,str(ROOT/"tests"))
    from test_factory_calibration import fixture
    profile,capture=fixture(robot_id=robot)
    for key,value in (("profile",profile),("capture",capture)):
        (out/(key+".json")).write_text(json.dumps(value,allow_nan=False),encoding="utf-8")
    proof=dict(passed=False,project=project,image=args.image,
               factory_inputs="synthetic fixture for robot identity and publication checks",
               exploration_inputs="Gazebo planar laser, IMU, wheel odometry; ray-integrated local occupancy grid",
               truth_scope="independent Gazebo world pose; not available to frontier planner")
    with tempfile.TemporaryDirectory(prefix=project) as scratch:
        tmp=Path(scratch)
        sdf=ET.parse(ROOT/"sim/world.sdf")
        # Finite field of view in range makes new observations possible in a small ROI.
        sdf.find(".//sensor[@name='horizontal_lidar']/lidar/range/max").text="2.1"
        # Both laser rendering and collisions see this pillar. Its position is never sent to planning.
        sdf.getroot().find("world").append(ET.fromstring('''<model name="commission_pillar">
          <static>true</static><pose>1.35 1.35 0.5 0 0 0</pose><link name="body">
          <visual name="visible"><geometry><box><size>0.5 0.5 1</size></box></geometry></visual>
          <collision name="solid"><geometry><box><size>0.5 0.5 1</size></box></geometry></collision>
          </link></model>'''))
        if args.record:
            from commission_recording import add_camera
            add_camera(sdf.getroot().find("world"))
        sdf.write(tmp/"world.sdf",encoding="utf-8",xml_declaration=True)
        proof["sdf_sha256"]=hashlib.sha256((tmp/"world.sdf").read_bytes()).hexdigest()
        if args.record:
            (out/"recorded-world.sdf").write_bytes((tmp/"world.sdf").read_bytes())
        domain=20+int(token[:4],16)%40
        env={**os.environ,"EDGE_ROS_DOMAIN_ID":str(domain),"EDGE_IMAGE":args.image}
        services={s:{"environment":{"GZ_PARTITION":partition}} for s in
                  ("sim","bridge","witness","core","gateway","perception")}
        def mount(source,target,ro=True):
            return {"type":"bind","source":str(source),"target":target,"read_only":ro}
        services["sim"].update(command=["gz","sim","-s","-r","--headless-rendering","/tmp/commission-world.sdf"],
                               volumes=[mount(tmp/"world.sdf","/tmp/commission-world.sdf")])
        modules=[mount(ROOT/s,"/opt/edge/"+s) for s in ("calibration","exploration")]
        services["trial"]={"image":args.image,"pull_policy":"never",
            "environment":{"EDGE_COMMISSION_MODE":"isolated_gazebo","ROS_DOMAIN_ID":str(domain),"EDGE_ROBOT_ID":robot,
                           "GZ_PARTITION":partition,"RMW_IMPLEMENTATION":"rmw_cyclonedds_cpp"},
            "working_dir":"/opt/edge",
            "command":["python3","-B","/opt/edge/tests/system/commission_exploration.py","--witness","/out/truth.json",
                       *(["--record"] if args.record else [])],
            "volumes":[mount(out,"/out",False),
                       mount(Path(__file__).resolve(),"/opt/edge/tests/system/commission_exploration.py"),
                       mount(Path(__file__).with_name("commission_recording.py"),"/opt/edge/tests/system/commission_recording.py"),
                       *([] if args.packaged else modules)]}
        override=tmp/"compose.json"
        override.write_text(json.dumps({"services":services}),encoding="utf-8")
        dc=["docker","compose","-p",project,"-f",str(ROOT/"compose.yaml"),"-f",str(override)]
        def command(*parts,check=True,timeout=120):
            return subprocess.run(dc+list(parts),cwd=ROOT,env=env,timeout=timeout,text=True,
                                  encoding="utf-8",errors="replace",check=check,capture_output=True)
        def worker(*parts,check=True):
            return command("exec","-T","trial","/usr/local/bin/edge-entrypoint","python3","-B","-m",
                           *parts,check=check,timeout=300)
        proof["image_id"]=subprocess.check_output(["docker","image","inspect",args.image,"--format","{{.Id}}"],text=True).strip()
        proof["plugin_source"]="packaged" if args.packaged else "current checkout mounted read-only"
        proof["test_driver_sha256"]=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        try:
            command("up","-d","--no-build",*services)
            hash_modules="""import hashlib, json
from pathlib import Path
print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest()
 for folder in ('calibration','exploration') for p in sorted(Path(folder).glob('*.py'))}))"""
            proof["runtime_source_sha256"]=json.loads(command("exec","-T","trial","python3","-c",hash_modules).stdout)
            calibrated=worker("calibration.factory","verify","--profile","/out/profile.json","--capture","/out/capture.json",
                              "--state","/out/factory","--operation","factory-"+token)
            proof["factory"]=json.loads(calibrated.stdout)
            assert proof["factory"]["status"]=="VERIFIED"
            invocation=["exploration.ros_sim","--robot-id",robot,"--calibration","/out/factory","--output-dir","/out",
                        "--max-goals","4"]
            op="explore-"+token
            run=worker(*invocation,"--run-id",op,check=False)
            report=json.loads((out/op/"report.json").read_bytes())
            proof["exploration"]=report
            assert run.returncode==0 and report["state"] in ("PARTIAL","COMPLETE"),(report["reason"],run.stderr[-1500:])
            assert 2<=len(report["steps"])<=4 and all(s["core_state"]=="SUCCEEDED" and s["verified_count"]==1 for s in report["steps"])
            assert report["final_known_cells"]>report["initial_known_cells"]
            with sqlite3.connect(out/op/"module-events.sqlite") as journal:
                events=[json.loads(row[0]) for row in journal.execute("SELECT event FROM module_bus_events ORDER BY seq")]
            assert [e["kind"] for e in events]==["exploration.started"]+["exploration.dispatch","exploration.receipt"]*len(report["steps"])+["exploration.finished"]
            assert all(e["parent_event_id"]==prev for e,prev in zip(events,[proof["factory"]["event_id"]]+[e["event_id"] for e in events]))
            proof["linked_module_events"]=len(events)
            for name,digest in report["map"]["sha256"].items():
                assert hashlib.sha256((out/op/"map"/name).read_bytes()).hexdigest()==digest,name
            occupancy=json.loads((out/op/"map"/"occupancy.json").read_bytes())
            x0,y0,_=occupancy["origin"]
            width,spacing=occupancy["width"],occupancy["resolution"]
            occupied_pillar=sum(1 for i,v in enumerate(occupancy["cells"]) if v==100
                and 1.0<=x0+(i%width+.5)*spacing<=1.7 and 1.0<=y0+(i//width+.5)*spacing<=1.7)
            assert occupied_pillar>=2,"test pillar missing from the laser-integrated occupancy grid"
            proof["observed_pillar_cells"]=occupied_pillar
            truth=read_snapshot(out/"truth.json")
            proof["truth"]=truth
            assert truth["samples"]>=10 and truth["max_displacement_m"]>.2 and truth["max_linear_command_mps"]>.05,truth
            assert truth["closest_pillar_clearance_m"]>.30,truth
            assert max(abs(v) for v in truth["latest_twist"])<.01,truth
            assert math.dist(truth["last"],report["steps"][-1]["measured_xy"][0])<.25,truth
            before=(out/op/"report.json").read_bytes()
            repeated=worker(*invocation,"--run-id",op,check=False)
            assert repeated.returncode and before==(out/op/"report.json").read_bytes()
            proof["repeat_refused"]=True
            wrong=worker(*["wrong-robot" if x==robot else x for x in invocation],"--run-id","wrong-"+token,check=False)
            assert wrong.returncode and not (out/("wrong-"+token)).exists()
            proof["other_robot_refused"]=True

            # Signal only our own CLI after the independent witness sees a command
            # from its accepted in-flight mission. Core/Gateway must perform the stop.
            canceled="cancel-"+token
            # Reset the isolated Gazebo + mapping domain to the unobserved start.
            # Reusing the map left by a prior operation can legitimately end before any motion.
            command("restart","sim","bridge","gateway","perception","witness","trial")
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                current=read_snapshot(out/"truth.json")
                if current["witness_instance"]!=truth["witness_instance"] and current["samples"]>0:
                    break
                time.sleep(.15)
            else:
                raise RuntimeError("private Gazebo restart produced no new independent witness")
            before_cancel=current["moving_commands"]
            invoke=dc+["exec","-T","trial","/usr/local/bin/edge-entrypoint","python3","-B","-m",
                       *invocation,"--run-id",canceled]
            active=subprocess.Popen(invoke,cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            cancel_file=out/canceled/"report.json"
            try:
                deadline=time.monotonic()+35
                while time.monotonic()<deadline and active.poll() is None:
                    if cancel_file.exists():
                        progress=read_snapshot(cancel_file)
                        witness_data=read_snapshot(out/"truth.json")
                        if (progress["steps"] and progress["steps"][-1].get("accepted")
                                and witness_data["moving_commands"]>before_cancel
                                and witness_data["max_displacement_m"]>.08):
                            break
                    time.sleep(.1)
                else:
                    raise RuntimeError("no accepted moving mission available for cancellation")
                signal_cli="""import os, signal, sys
from pathlib import Path
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        argv=path.read_bytes().split(b'\\0')
    except (FileNotFoundError, ProcessLookupError):
        continue
    if b'exploration.ros_sim' in argv and sys.argv[1].encode() in argv and int(path.parent.name)!=os.getpid():
        os.kill(int(path.parent.name),signal.SIGTERM)
        break
else:
    raise SystemExit('in-flight exploration worker not found')
"""
                command("exec","-T","trial","python3","-c",signal_cli,canceled)
                assert active.wait(timeout=25)==2
                stopped=read_snapshot(cancel_file)
                assert stopped["state"]=="CANCELED" and stopped["reason"]=="operator_cancel",stopped
                assert len(stopped["steps"])==1 and stopped["cancel"]["mission_id"]==stopped["steps"][0]["mission_id"]
                assert stopped["cancel"]["state"]=="CANCELED" and stopped["map"]["metadata"]["state"]=="CANCELED"
                time.sleep(.8)
                truth_after=read_snapshot(out/"truth.json")
                assert max(abs(v) for v in truth_after["latest_twist"])<.01
                time.sleep(.8)
                stationary=read_snapshot(out/"truth.json")
                stop_drift=math.dist(truth_after["last"],stationary["last"])
                assert stationary["samples"]>truth_after["samples"] and stop_drift<.015
                proof["inflight_cancel"]=dict(receipt=stopped["cancel"],state=stopped["state"],reason=stopped["reason"],
                    goal_count=len(stopped["steps"]),last_twist=truth_after["latest_twist"],partial_map=True,
                    pre_cancel_displacement_m=witness_data["max_displacement_m"],stopped_drift_m=stop_drift)
            finally:
                if active.poll() is None:
                    active.kill()
                active.wait(timeout=5)
            command("stop","perception")
            blocked="missing-"+token
            lost=worker(*invocation,"--run-id",blocked,check=False)
            missing=json.loads((out/blocked/"report.json").read_bytes())
            assert lost.returncode and missing["state"]=="BLOCKED" and not missing["steps"]
            proof["mapping_offline"]={"state":missing["state"],"reason":missing["reason"],"steps":len(missing["steps"])}
            if args.record:
                from collections import Counter
                proof["media"]={}
                for epoch in ("explore","cancel"):
                    root=out/"media"/epoch
                    rows=[json.loads(line) for line in (root/"timeline.jsonl").read_text(encoding="utf-8").splitlines()]
                    counts=Counter(row["kind"] for row in rows)
                    assert all(counts.get(kind,0)>=1 for kind in ("world_pose","fused_pose","map","twist","mission")),counts
                    assert counts["rgb"]>=12 and counts["map"]>=2,counts
                    assert any(r["kind"]=="mission" and r["phase"]=="finished" for r in rows)
                    proof["media"][epoch]=dict(counts=dict(counts),rgb_size=[768,512],timeline="media/"+epoch+"/timeline.jsonl")
            proof["passed"]=True
        except Exception as exc:
            proof["error"]=str(exc)
            if isinstance(exc,subprocess.CalledProcessError):
                proof["stderr"]=exc.stderr[-2000:]
                proof["stdout"]=exc.stdout[-1000:]
            proof["service_logs"]=command("logs","--no-color","--tail","4","core","gateway","trial","perception",check=False).stdout[-3500:]
            raise
        finally:
            command("down","--volumes","--remove-orphans","--timeout","4",check=False)
            (out/"acceptance.json").write_text(json.dumps(proof,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"passed":proof["passed"],"report":str(out/"acceptance.json")}))


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--image",default="edge-agent-ros:field-ready")
    cli.add_argument("--out-dir",type=Path)
    cli.add_argument("--packaged",action="store_true",help="test immutable image instead of mounting plugin sources")
    cli.add_argument("--record",action="store_true",help="save time-stamped Gazebo RGB, measured maps and motion evidence")
    cli.add_argument("--witness",type=Path,help=argparse.SUPPRESS)
    args=cli.parse_args()
    if args.witness:
        witness(args.witness,args.record)
    elif args.out_dir:
        trial(args)
    else:
        cli.error("provide --out-dir")
