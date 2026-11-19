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


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--capture",type=Path,required=True)
    args=cli.parse_args()
    sys.path.insert(0,"/opt/edge")
    import rclpy
    rclpy.init()
    node=rclpy.create_node("single_mission_recorder")
    recorder=LongMissionRecorder(node,args.capture,"mission")
    rclpy.spin(node)
