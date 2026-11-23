"""Run paired short point missions in isolated Gazebo campus worlds; the witness scores each run."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
BIN = "/opt/edge/tests/system/campus_local_plan_acceptance.py"
WITNESS = "/opt/edge/tests/system/campus_local_plan_witness.py"
WORLD = "edge_lab"
BOX = "sdd056_center_box"
LINGAO_PROFILE = "/opt/edge/install/share/edge_gateway/config/footprints/lingao-archive-v1.json"
START = (6.0, 0.0)
GOAL = (9.8, 0.0)
SERVICES = ("sim","bridge","witness","gateway","core")


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+"\n", encoding="utf8")


def fixture(world_path, scenario, vehicle="campus"):
    tree = ET.parse(world_path)
    world = tree.getroot().find("world")
    if world.get("name")!=WORLD or world.find("model[@name='edge_rover']") is None or world.find(f"model[@name='{BOX}']") is not None:
        raise ValueError("expected a pristine campus world with the rover")
    rover = world.find("model[@name='edge_rover']")
    if vehicle == "lingao":
        from campus_lingao_fixture import install
        install(rover)
    pose = rover.find("pose")
    pose.text = f"{START[0]} {START[1]} {pose.text.split()[2]} 0 0 0"
    ET.SubElement(world,"plugin",filename="gz-sim-contact-system",name="gz::sim::systems::Contact")
    for link in rover.findall("link"):
        name = link.get("name")
        collisions = link.findall("collision")
        if len(collisions) != 1:
            raise ValueError("expected a single monitored shape per link: "+name)
        collision = collisions[0].get("name")
        sensor=ET.SubElement(link,"sensor",name="acceptance_contact",type="contact")
        ET.SubElement(sensor,"always_on").text="true"
        ET.SubElement(sensor,"update_rate").text="50"
        contact=ET.SubElement(sensor,"contact")
        ET.SubElement(contact,"collision").text=collision
        ET.SubElement(contact,"topic").text="/sdd056/contacts/"+name
    if scenario=="centered":
        model=ET.SubElement(world,"model",name=BOX)
        ET.SubElement(model,"static").text="true"
        ET.SubElement(model,"pose").text=f"{(START[0]+GOAL[0])/2} {START[1]} 0.5 0 0 0"
        link=ET.SubElement(model,"link",name="body")
        for kind in ("collision","visual"):
            geometry=ET.SubElement(ET.SubElement(link,kind,name=kind),"geometry")
            ET.SubElement(ET.SubElement(geometry,"box"),"size").text="0.5 0.5 1.0"
    if scenario=="crossing":
        from campus_crossing_fixture import install
        install(world)
    tree.write(world_path,encoding="utf8",xml_declaration=True)


def mission(a):
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from geometry_msgs.msg import Point
    from nav_msgs.msg import Odometry
    from edge_interfaces.srv import GetMission, SubmitMission

    rclpy.init()
    node=rclpy.create_node("campus_local_plan_submission")
    odom=[]
    node.create_subscription(Odometry,"/odom",lambda m: odom.append(m),qos_profile_sensor_data)
    clients={kind:node.create_client(cls,"/edge/"+kind+"_mission") for kind,cls in
             (("submit",SubmitMission),("get",GetMission))}
    for client in clients.values():
        if not client.wait_for_service(timeout_sec=30):
            raise TimeoutError("mission service unavailable")
    end=time.monotonic()+30
    while len(odom)<4 and time.monotonic()<end:
        rclpy.spin_once(node,timeout_sec=.2)
    if len(odom)<4:
        raise RuntimeError("missing odometry")
    p=odom[-1].pose.pose
    q=p.orientation
    yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
    dx=GOAL[0]-START[0]
    target=(p.position.x+dx*math.cos(yaw),p.position.y+dx*math.sin(yaw))
    if any(abs(c)>4 for c in target):
        raise ValueError(f"target outside Core odom envelope: {target}")
    def call(kind,req):
        future=clients[kind].call_async(req)
        rclpy.spin_until_future_complete(node,future,timeout_sec=8)
        if not future.done() or future.exception():
            raise RuntimeError("mission service timeout/error: "+kind)
        return future.result()
    req=SubmitMission.Request()
    req.mission_id=a.task
    req.waypoints=[Point(x=target[0],y=target[1],z=0.0)]
    accepted=call("submit",req)
    result={"task":a.task,"operation":a.task+"/waypoint/1","world_start":START,"world_goal":GOAL,
            "odom_initial":[p.position.x,p.position.y,yaw],"odom_goal":target,"accepted":accepted.accepted,
            "state":accepted.state,"detail":accepted.detail}
    save(a.output,result)
    if accepted.accepted:
        poll=GetMission.Request()
        poll.mission_id=a.task
        end=time.monotonic()+110
        while time.monotonic()<end:
            answer=call("get",poll)
            if answer.state in ("SUCCEEDED","FAILED","CANCELED","RECOVERY_REQUIRED","BLOCKED"):
                result.update(state=answer.state,verified_count=answer.verified_count,detail=answer.detail,
                              odom_evidence=[[pt.x,pt.y] for pt in answer.evidence])
                break
            time.sleep(.25)
        else:
            raise TimeoutError("no terminal core receipt")
    save(a.output,result)
    with sqlite3.connect("file:/core-state/edge.db?mode=ro",uri=True) as db:
        db.row_factory=sqlite3.Row
        events=[dict(r) for r in db.execute("SELECT * FROM mission_events WHERE mission_id=? ORDER BY seq",(a.task,))]
        steps=[dict(r) for r in db.execute("SELECT * FROM steps WHERE mission_id=? ORDER BY step",(a.task,))]
    save(a.output.with_name("core-ledger.json"),{"mission_id":a.task,"events":events,"steps":steps})
    if not events or events[-1]["state"]!=result["state"] or (accepted.accepted and
            (len(steps)!=1 or steps[0]["operation_id"]!=result["operation"])):
        raise RuntimeError("Core event ledger and public task result differ")
    node.destroy_node()
    rclpy.shutdown()


def execute(a, scenario, out, *, preflight=False):
    out.mkdir(parents=True,exist_ok=False)
    token=uuid.uuid4().hex[:10]
    project="edge-local-"+token
    domain=str(100+int(token[:4],16)%100)
    env={k:v for k,v in os.environ.items() if not k.startswith(("COMPOSE_","EDGE_"))}
    env.update(COMPOSE_DISABLE_ENV_FILE="1",EDGE_IMAGE=a.image,EDGE_ROS_DOMAIN_ID=domain,
               EDGE_CAMPUS_ASSETS_DIR=str(a.assets.resolve()))
    common={"GZ_PARTITION":project,"ROS_DOMAIN_ID":domain}
    script_mount=str(Path(__file__).resolve())+":"+BIN+":ro"
    witness_file=ROOT/"tests/system/campus_local_plan_witness.py"
    if a.planner=="footprint_follow" and a.vehicle!="lingao":
        raise ValueError("the archive footprint requires the matching LingAo body")
    fixture_mount=str(witness_file.with_name("campus_lingao_fixture.py"))+":/opt/edge/tests/system/campus_lingao_fixture.py:ro"
    crossing_bin="/opt/edge/tests/system/campus_crossing_fixture.py"
    crossing_mount=str(witness_file.with_name("campus_crossing_fixture.py"))+":"+crossing_bin+":ro"
    overrides={s:{"environment":common.copy()} for s in SERVICES}
    overrides["campus-build"]={"volumes":[script_mount,fixture_mount,crossing_mount],"command":["sh","-ec",
        "python3 -B /opt/edge/scene/campus_large/build.py --assets /assets --base /opt/edge/sim/world.sdf --output /campus && "
        +f"python3 -B {BIN} fixture --scenario {scenario} --vehicle {a.vehicle} --world-file /campus/world.sdf"]}
    overrides["sim"]["environment"]["LP_NUM_THREADS"]="4"
    localization=a.localization or ("wheel_gyro_scan" if a.vehicle=="lingao" else "wheel")
    if localization!="wheel":
        if a.vehicle!="lingao":
            raise ValueError("wheel+gyro fixture supports the LingAo body")
        overrides["bridge"]["command"]=["ros2","run","ros_gz_bridge","parameter_bridge","--ros-args",
            "-r","__node:=edge_control_bridge","-r","/odom:=/lingao/wheel_odom","-p","config_file:=/opt/edge/sim/bridge.yaml"]
        odom_script=witness_file.with_name("campus_wheel_gyro_odom.py")
        correction=witness_file.with_name("campus_scan_translation.py")
        overrides["wheel-gyro"]={"image":a.image,"pull_policy":"never","init":True,
            "command":["python3","-B","/wheel-gyro.py"]+(["--scan"] if localization=="wheel_gyro_scan" else []),
            "volumes":[str(odom_script)+":/wheel-gyro.py:ro",str(correction)+":/campus_scan_translation.py:ro"],
            "environment":{**common,"RMW_IMPLEMENTATION":"rmw_cyclonedds_cpp","ROS_LOG_DIR":"/tmp/ros-logs"}}
    overrides["gateway"]["command"]=["ros2","run","edge_gateway","edge_gateway_node","--ros-args","-p",
         "require_scan:=true","-p","local_planner:="+a.planner,"-p","scan_calibration:="+
         ("lingao_sim_planar_laser_v1" if a.vehicle=="lingao" else "campus_rover_laser_v1"),
         "-p","scan_origin_x_m:=0.08","-p","scan_range_resolution_m:=0.01"]
    if a.planner=="footprint_follow":
        overrides["gateway"]["command"].extend(["-p","vehicle_profile:="+LINGAO_PROFILE])
    if a.dynamic_policy:
        overrides["gateway"]["command"].extend(["-p","dynamic_observer:=scan_clusters","-p","dynamic_policy:="+a.dynamic_policy])
    mounts=[str(out)+":/audit",script_mount,crossing_mount,"campus-world:/campus:ro","state:/core-state:ro",str(a.assets.resolve())+":/assets:ro"]
    mounts.extend([str(witness_file)+":"+WITNESS+":ro",
                   str(witness_file.with_name("campus_route_geometry.py"))+":/opt/edge/tests/system/campus_route_geometry.py:ro"])
    overrides["audit"]={"image":a.image,"pull_policy":"never","init":True,"volumes":mounts,
        "command":["sleep","infinity"],"environment":{**common,"RMW_IMPLEMENTATION":"rmw_cyclonedds_cpp",
        "ROS_LOG_DIR":"/tmp/ros-logs"}}
    override=out/"compose.override.json"
    overlay={"services":overrides}
    extensions=()
    compose_files=[ROOT/"compose.yaml",ROOT/"compose.campus.yaml"]
    if a.mission_driver=="agent":
        import agent_avoidance_acceptance as agent_driver
        extensions=tuple(agent_driver.configure(overrides,common,a,out,env,project))
        overlay["volumes"]=agent_driver.volumes()
        compose_files.append(ROOT/"compose.rover-maneuver.yaml")
    save(override,overlay)
    dc=["docker","compose","-p",project]
    for path in [*compose_files,override]:
        dc.extend(("-f",str(path)))
    def compose(*args,timeout=180):
        return subprocess.run([*dc,*map(str,args)],cwd=ROOT,env=env,check=True,capture_output=True,
                              encoding="utf8",errors="replace",timeout=timeout).stdout
    def ros(*args,timeout=140):
        return compose("exec","-T","audit","/usr/local/bin/edge-entrypoint",*args,timeout=timeout)
    def wait_file(name,limit):
        end=time.monotonic()+limit
        path=out/"witness"/name
        while time.monotonic()<end:
            if path.is_file():
                return
            worker="cart" if name=="cart-armed.json" else "record"
            status=out/"witness"/(worker+".exit")
            if status.exists():
                detail=(out/(worker+".log")).read_text(encoding="utf8",errors="replace")[-1600:]
                raise RuntimeError(f"{worker} exited with status {status.read_text().strip()} before {name}\n{detail}")
            time.sleep(.5)
        raise TimeoutError("missing witness file: "+name)
    task="local-"+scenario+"-"+token
    meta={"scenario":scenario,"project":project,"ros_domain":domain,"image":a.image,"task":task,
          "world":WORLD,"start":START,"goal":GOAL,"vehicle":a.vehicle,"planner":a.planner,"localization":localization,
          "mission_driver":a.mission_driver,"dynamic_policy":a.dynamic_policy,
          "obstacle":BOX if scenario=="centered" else "crossing_cart" if scenario=="crossing" else None,
          "utc":datetime.now(timezone.utc).isoformat()}
    save(out/"run.json",meta)
    try:
        up=SERVICES if not preflight else ("sim","bridge","witness")
        if localization!="wheel":
            up=(*up,"wheel-gyro")
        if not preflight:up=(*up,*extensions)
        compose("up","-d","--no-build",*up,"audit",timeout=240)
        if a.planner=="footprint_follow":
            profile_text=compose("exec","-T","audit","cat",LINGAO_PROFILE)
            (out/"vehicle-profile.json").write_bytes(profile_text.encode("utf8"))
        meta["image_id"]=subprocess.check_output(["docker","image","inspect",a.image,"--format","{{.Id}}"],text=True).strip()
        meta["deployed_world_sha256"]=compose("exec","-T","audit","sha256sum","/campus/world.sdf").split()[0]
        save(out/"run.json",meta)
        (out/"witness").mkdir(exist_ok=True)
        compose("exec","-d","-T","audit","/usr/local/bin/edge-entrypoint","sh","-c",
          f"python3 -B {WITNESS} record --out /audit/witness --world {WORLD} >/audit/record.log 2>&1; "
          'status=$?; echo "$status" >/audit/witness/record.exit; exit "$status"')
        wait_file("ready.json",120)
        if scenario=="crossing" and not preflight:
            compose("exec","-d","-T","audit","/usr/local/bin/edge-entrypoint","sh","-c",
                    f"python3 -B {crossing_bin} --arm /audit/witness >/audit/cart.log 2>&1; "
                    'status=$?; echo "$status" >/audit/witness/cart.exit; exit "$status"')
            wait_file("cart-armed.json",20)
        if preflight:
            diagnostic={}
            for topic in ("/clock","/scan","/odom","/ground_truth"):
                sample=ros("timeout","60","ros2","topic","echo",topic,"--once","--qos-reliability","best_effort",timeout=72)
                diagnostic[topic]=sample[:500]
            save(out/"preflight.json",diagnostic)
            (out/"witness/finish").touch()
            wait_file("complete.json",45)
            return
        if a.mission_driver=="agent":
            result=agent_driver.execute(compose,ros,out,task,scenario)
            if result is not None:save(out/"mission.json",result)
        else:
            ros("python3","-B",BIN,"mission","--task",task,"--output","/audit/mission.json")
        time.sleep(1)
        (out/"witness/finish").touch()
        wait_file("complete.json",45)
        run=json.loads((out/"mission.json").read_bytes())
        world_goal=GOAL
        if a.mission_driver=="agent":
            # Map the provider's frozen local-odometry goal into world for grading only.
            observations=out/"witness/observations.jsonl"
            with observations.open(encoding="utf8") as f:
                initial_truth=next(row for line in f if (row:=json.loads(line))["kind"]=="truth")
            x,y,yaw=run["odom_initial"];gx,gy=run["odom_goal"]
            theta=initial_truth["yaw"]-yaw;dx,dy=gx-x,gy-y
            world_goal=(initial_truth["xy"][0]+math.cos(theta)*dx-math.sin(theta)*dy,
                        initial_truth["xy"][1]+math.sin(theta)*dx+math.cos(theta)*dy)
            meta.update(goal=world_goal,task=run["task"],operation=run["operation"],odom_goal=run["odom_goal"])
            save(out/"run.json",meta)
        score=ros("python3","-B",WITNESS,"score","--out","/audit/witness","--scenario",scenario,
                  "--operation",run["operation"],"--start",*START,"--goal",*world_goal,"--obstacle",meta["obstacle"] or BOX,"--vehicle",a.vehicle,
                  "--localization",localization,"--planner",a.planner,
                  *(["--dynamic-policy",a.dynamic_policy] if a.dynamic_policy else []))
        (out/"score.log").write_text(score,encoding="utf8")
        if run["state"]!="SUCCEEDED" or run.get("verified_count")!=1:
            raise RuntimeError("Core did not verify arrival: "+str(run))
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or "")+"\n"+(exc.stdout or "")) from exc
    finally:
        try:
            # A refused/failed Agent submission can end before normal scoring.
            # Give the observer the same opportunity to flush physical evidence.
            if (out/"witness/ready.json").is_file() and not (out/"witness/finish").exists():
                (out/"witness/finish").touch()
                try:
                    wait_file("complete.json",30)
                except (TimeoutError,RuntimeError) as error:
                    (out/"witness-shutdown.log").write_text(str(error),encoding="utf8")
            (out/"services.log").write_text(compose("logs","--no-color","--tail","350"),encoding="utf8")
        finally:
            compose("down","--volumes",timeout=90)


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    sub=cli.add_subparsers(dest="mode",required=True)
    for mode in ("run","preflight"):
        cmd=sub.add_parser(mode)
        cmd.add_argument("--image",required=True)
        cmd.add_argument("--assets",type=Path,required=True)
        cmd.add_argument("--out",type=Path,required=True)
        cmd.add_argument("--scenario",choices=("clear","centered","crossing","both"),default="both")
        cmd.add_argument("--vehicle",choices=("campus","lingao"),default="campus")
        cmd.add_argument("--planner",choices=("gap_follow","footprint_follow"),default="gap_follow")
        cmd.add_argument("--localization",choices=("wheel","wheel_gyro","wheel_gyro_scan"))
        cmd.add_argument("--mission-driver",choices=("core","agent"),default="core")
        cmd.add_argument("--dynamic-policy",choices=("yield_crossing",),help="enable the same moving-obstacle observer and policy in either driver")
    cmd=sub.add_parser("fixture")
    cmd.add_argument("--world-file",type=Path,required=True)
    cmd.add_argument("--scenario",choices=("clear","centered","crossing"),required=True)
    cmd.add_argument("--vehicle",choices=("campus","lingao"),default="campus")
    cmd=sub.add_parser("mission")
    cmd.add_argument("--task",required=True)
    cmd.add_argument("--output",required=True,type=Path)
    a=cli.parse_args()
    if a.mode=="fixture":
        fixture(a.world_file,a.scenario,a.vehicle)
    elif a.mode=="mission":
        mission(a)
    else:
        for case in (("clear","centered") if a.scenario=="both" else (a.scenario,)):
            execute(a,case,a.out.resolve()/case,preflight=a.mode=="preflight")
