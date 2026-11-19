"""Single-epoch, test-only RGB and ROS recording for the named-place mission."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from xml.etree import ElementTree as ET

from commission_recording import Recorder, add_camera


def make_world(source,output):
    source,output=Path(source).resolve(),Path(output).resolve()
    base=source/"sim/world.sdf" if source.is_dir() else source
    root=base.parents[1]
    sys.path.insert(0,str(root/"scene"))
    from layout import Layout
    from start_world import generate
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():
        raise FileExistsError(output)
    generate(base,Layout(root/"scene/campus.json"),output)
    tree=ET.parse(output)
    world=tree.getroot().find("world")
    add_camera(world)
    world.find("./model[@name='commission_recording_camera']/pose").text="1.55 -4.6 5.7 0 .88 1.5707963268"
    # Camera-only visibility mask keeps these visuals for the GPU lidar and all physics.
    # All other geometry still uses the default visibility flags (all bits set).
    camera=world.find("./model[@name='commission_recording_camera']/link/sensor/camera")
    ET.SubElement(camera,"visibility_mask").text="1"
    for name in ("roof","door_wall_south","workshop_south"):
        visual=world.find(f"./model[@name='{name}']/link/visual")
        ET.SubElement(visual,"visibility_flags").text="2"
    tree.write(output,encoding="utf-8",xml_declaration=True)
    return output


def recording_services(image,domain,partition,world,out):
    here=Path(__file__).resolve().parent
    world,out=Path(world).resolve(),Path(out).resolve()
    out.mkdir(parents=True,exist_ok=True)
    def bind(source,target,readonly=True):
        return {"type":"bind","source":str(source),"target":target,"read_only":readonly}
    env={"ROS_DOMAIN_ID":str(domain),"GZ_PARTITION":partition,"RMW_IMPLEMENTATION":"rmw_cyclonedds_cpp"}
    common={name:{"environment":dict(env)} for name in ("sim","bridge","witness","perception")}
    common["sim"].update(command=["gz","sim","-s","-r","--headless-rendering","/tmp/long-mission.sdf"],
                         volumes=[bind(world,"/tmp/long-mission.sdf")])
    common["recorder"]={"image":image,"pull_policy":"never","environment":env,"working_dir":"/opt/edge","init":True,
        "command":["python3","-B","/opt/edge/tests/system/long_mission_world.py","--capture","/out"],
        "volumes":[bind(out,"/out",False),bind(here/"commission_recording.py","/opt/edge/tests/system/commission_recording.py"),
                   bind(here/"long_mission_world.py","/opt/edge/tests/system/long_mission_world.py")]}
    return common


def mark(out,phase,task_id,**fields):
    """Called by the single test driver after an observation or recorded intervention."""
    record=dict(phase=phase,task_id=task_id,host_wall_ns=time.time_ns(),**fields)
    with (Path(out)/"mission-markers.jsonl").open("a",encoding="utf-8") as f:
        f.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+"\n")


class LongMissionRecorder(Recorder):
    def missions(self):
        if self.clock<=0 or self.done():
            return
        markers=self.out/"mission-markers.jsonl"
        if not markers.exists():
            return
        for seq,line in enumerate(markers.read_bytes().splitlines(keepends=True)):
            if seq in self.seen or not line.endswith(b"\n"):
                continue
            record=json.loads(line)
            self.seen.add(seq)
            self.emit("mission",self.clock,**record)
            if record["phase"]=="finished":
                self.end_stamp=self.clock


def rows_at(out):
    path=Path(out)/"media"/"mission"/"timeline.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().splitlines(keepends=True) if line.endswith(b"\n")]


def await_recording(out,timeout=45):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        kinds={r["kind"] for r in rows_at(out)}
        if {"rgb","map","world_pose","fused_pose","clock"}<=kinds:
            return True
        time.sleep(.3)
    raise TimeoutError("missing Gazebo RGB or measured ROS grid/pose at recorder")


def finalize(out,timeout=10):
    """After the terminal marker, wait for physical samples and report synchronized measured coverage."""
    from collections import Counter
    out=Path(out)
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        rows=rows_at(out)
        stops=[r for r in rows if r["kind"]=="mission" and r["phase"]=="finished"]
        if stops and any(r["kind"]=="rgb" and r["stamp_ns"]>=stops[-1]["stamp_ns"]+1200000000 for r in rows):
            break
        time.sleep(.25)
    else:
        raise TimeoutError("single-epoch recording lacked post-terminal Gazebo image")
    counts=dict(Counter(r["kind"] for r in rows))
    assert all(counts.get(k,0)>1 for k in ("rgb","map","fused_pose","world_pose","twist","mission")),counts
    recordings=out/"media"/"mission"
    for row in rows:
        if row["kind"]=="rgb" and hashlib.sha256((recordings/row["file"]).read_bytes()).hexdigest()!=row["sha256"]:
            raise ValueError("invalid RGB artifact: "+row["file"])
    physical=[r for r in rows if r["kind"]=="world_pose"]
    delta=sum(math.dist(a["xy"],b["xy"]) for a,b in zip(physical,physical[1:]))
    summary={"counts":counts,"measured_world_path_m":delta,"start_xy":physical[0]["xy"],"end_xy":physical[-1]["xy"],
             "timeline":"media/mission/timeline.jsonl",
             "presentation":"camera mask cuts away roof and two near-facing walls; lidar visuals and physical collision retained",
             "clock":"one Gazebo simulation epoch, ROS message or latest received /clock time (ns)"}
    (out/"media"/"recording.json").write_text(json.dumps(summary,indent=2)+"\n",encoding="utf-8")
    return summary


def write_waypoint_audit(out):
    """Independently join each Core odometry receipt to measured world pose on simulation time."""
    out=Path(out)
    proof=json.loads((out/"acceptance.json").read_text(encoding="utf-8"))
    if not proof["passed"]:
        raise ValueError("only audit a completed, accepted task")
    rows=rows_at(out)
    truth=[r for r in rows if r["kind"]=="world_pose"]
    if any(a["stamp_ns"]>=b["stamp_ns"] for a,b in zip(truth,truth[1:])):
        raise ValueError("world-pose stream is not a single advancing simulation epoch")
    markers=[r for r in rows if r["kind"]=="mission" and r["task_id"]==proof["task_id"]]
    receipts=[r for r in markers if r["phase"]=="waypoint"]
    goals=proof["goals"]
    assert [r["index"] for r in receipts]==list(range(1,len(goals)+1))
    matches=[]
    for goal,receipt in zip(goals,receipts):
        stamp=receipt["odom_stamp_ns"]
        observed=min(truth,key=lambda r:abs(r["stamp_ns"]-stamp))
        skew=abs(observed["stamp_ns"]-stamp)/1e6
        error=math.dist(goal,observed["xy"])
        matches.append(dict(index=receipt["index"],operation_id=receipt["operation_id"],goal_xy=goal,
            odom_stamp_ns=stamp,world_stamp_ns=observed["stamp_ns"],stamp_delta_ms=skew,world_xy=observed["xy"],
            odom_xy=receipt["xy"],world_goal_error_m=error,odom_world_distance_m=math.dist(receipt["xy"],observed["xy"])))

    # The actual test world contains the two jambs. Derive the opening (x, y-limits)
    # from their poses and collision dimensions, independently of commanded waypoints.
    world=ET.parse(out/"world.sdf").getroot().find("world")
    edges={}
    for name,sign in (("door_wall_south",1),("door_wall_north",-1)):
        wall=world.find(f"./model[@name='{name}']")
        x,y,*_=map(float,wall.findtext("pose").split())
        size=list(map(float,wall.findtext("link/collision/geometry/box/size").split()))
        edges[name]=y+sign*size[1]/2
    low,high=edges["door_wall_south"],edges["door_wall_north"]
    crossings=[]
    for a,b in zip(truth,truth[1:]):
        ax,ay=a["xy"]
        bx,by=b["xy"]
        if (ax<x<=bx) or (bx<x<=ax):
            t=(x-ax)/(bx-ax)
            cy=ay+t*(by-ay)
            crossings.append(dict(direction="outbound" if bx>ax else "return",x=x,y=cy,
                stamp_ns=round(a["stamp_ns"]+t*(b["stamp_ns"]-a["stamp_ns"])),clearance_to_jamb_m=min(cy-low,high-cy)))
    end=truth[-1]
    rest=[r for r in truth if r["stamp_ns"]>=end["stamp_ns"]-800000000]
    drift=max(math.dist(r["xy"],end["xy"]) for r in rest)
    trailing=[r for r in rows if r["kind"]=="twist"]
    home_error=math.dist(goals[-1],end["xy"])
    good=(len(rest)>=10 and drift<.02 and home_error<.15 and len(crossings)==2
          and [c["direction"] for c in crossings]==["outbound","return"]
          and all(c["clearance_to_jamb_m"]>=.30 for c in crossings)
          and all(m["stamp_delta_ms"]<=100 and m["world_goal_error_m"]<.15 for m in matches)
          and max(abs(trailing[-1][k]) for k in ("linear","angular"))<.01)
    audit=dict(passed=good,task_id=proof["task_id"],trace_id=proof["trace_id"],image_id=proof["image_id"],
        source="Gazebo independent /ground_truth joined to Core odometry header stamps; odom/world aligned at spawn",
        timeline_sha256=hashlib.sha256((out/"media/mission/timeline.jsonl").read_bytes()).hexdigest(),
        max_pair_skew_allowed_ms=100,max_world_goal_error_allowed_m=.15,goals=matches,
        doorway=dict(x=x,y_min=low,y_max=high,required_center_clearance_m=.30,crossings=crossings),
        stop=dict(start_xy=truth[0]["xy"],end_xy=end["xy"],goal_error_m=home_error,
                  stationary_window_s=(end["stamp_ns"]-rest[0]["stamp_ns"])/1e9,
                  samples=len(rest),drift_m=drift,max_allowed_drift_m=.02,
                  final_command={k:trailing[-1][k] for k in ("linear","angular")}))
    (out/"waypoint-audit.json").write_text(json.dumps(audit,indent=2)+"\n",encoding="utf-8")
    if not good:
        raise ValueError("independent waypoint audit failed; see waypoint-audit.json")
    return audit


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    operation=cli.add_mutually_exclusive_group(required=True)
    operation.add_argument("--capture",type=Path)
    operation.add_argument("--audit",type=Path)
    args=cli.parse_args()
    if args.audit:
        write_waypoint_audit(args.audit)
        sys.exit(0)
    sys.path.insert(0,"/opt/edge")
    import rclpy
    rclpy.init()
    node=rclpy.create_node("single_mission_recorder")
    recorder=LongMissionRecorder(node,args.capture,"mission")
    rclpy.spin(node)
