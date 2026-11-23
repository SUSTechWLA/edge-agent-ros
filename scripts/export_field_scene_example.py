"""Rebuild a Gazebo comparison view from the centered task's recorded first scan."""
import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sys
import tarfile
from typing import Callable
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from perception.rover_preview.planner import plan

ARCHIVE = ROOT / "artifacts/verification/rover-maneuver-20261123/local/centered.tar.gz"
PROFILE = ROOT / "perception/rover_preview/lingao.json"
MODEL = "sdd056_center_box"


def local(x, y, pose):
    dx, dy = x-pose["xy"][0], y-pose["xy"][1]
    c, s = math.cos(pose["yaw"]), math.sin(pose["yaw"])
    return c*dx+s*dy, -s*dx+c*dy


def project_scan(scan,geometry,profile,calibration_ref):
    """Project a recorded Gazebo laser origin and its traversed distances into base_link."""
    ox,oy=geometry["laser_xy"]
    syaw = geometry["laser_yaw"]
    ranges=scan["ranges"]
    if abs(len(ranges)*scan["step"]-math.tau)>.05:
        raise ValueError("expected a full-turn Gazebo scan")
    def beam(v):
        if v=="+":
            return scan["range_max"],False
        if type(v) in (int,float) and scan["range_min"]<=v<=scan["range_max"]:
            return v,True
        # Negative infinity, invalid numerics and dropped beams do not observe free space.
        return 0.,False
    beams=[beam(r) for r in ranges]
    hits=[]
    for i,(extent,hit) in enumerate(beams):
        if hit:
            angle=syaw+scan["start"]+i*scan["step"]
            hits.append([ox+extent*math.cos(angle),oy+extent*math.sin(angle)])
    def observed(x,y):
        dx,dy=x-ox,y-oy
        angle=math.atan2(dy,dx)-syaw
        i=round((angle-scan["start"])/scan["step"])%len(beams)
        r=math.hypot(dx,dy)
        return scan["range_min"]<=r<min(beams[(i+n)%len(beams)][0] for n in (-1,0,1))
    def inside(x,y):
        return -profile["rear_m"]<=x<=profile["front_m"] and -profile["right_m"]<=y<=profile["left_m"]
    rays=[]
    for i in range(180):
        angle=i*math.tau/180
        free_to=0.
        for j in range(1,321):
            distance=j*.025
            x,y=distance*math.cos(angle),distance*math.sin(angle)
            if not inside(x,y) and not observed(x,y):
                break
            free_to=distance
        rays.append({"angle_rad":angle,"hit_m":None,"free_to_m":free_to})
    snap={"base_frame":"base_link","scan_stamp_ns":str(scan["stamp_ns"]),"source_id":"gazebo.rover.scan",
          "calibration_ref":calibration_ref,"rays":rays,"hits_body_m":hits}
    return snap,observed,inside


@dataclass
class RecordedScan:
    snapshot: dict
    profile: dict
    geometry: dict
    score: dict
    pose: dict
    scan: dict
    world_xml: bytes
    goal: tuple
    observed: Callable[[float, float], bool]
    inside: Callable[[float, float], bool]


def read_recorded_scan(archive, profile_path):
    """Read the first observation and its registered goal, without running a planner."""
    with tarfile.open(archive, "r:gz") as bundle:
        def read(name):
            return bundle.extractfile(name).read()
        geometry = json.loads(read("witness/geometry.json"))
        score = json.loads(read("witness/score.json"))
        world_xml = read("witness/world.sdf")
        site = json.loads(read("site/sim.json")) if "site/sim.json" in bundle.getnames() else {}
        scan = None
        poses = []
        for src in bundle.extractfile("witness/observations.jsonl"):
            row = json.loads(src)
            if row["kind"] == "scan" and scan is None:
                scan = row
            if row["kind"] == "truth":
                poses.append(row)
                if scan is not None and row["stamp_ns"] >= scan["stamp_ns"]:
                    break
    pose = min(poses,key=lambda p:abs(p["stamp_ns"]-scan["stamp_ns"]))
    if not score["passed"] or abs(pose["stamp_ns"]-scan["stamp_ns"])>50_000_000 or pose["frame"]!="world":
        raise ValueError("archive lacks a matching initial world pose")
    sdf_hash=hashlib.sha256(world_xml).hexdigest()
    if geometry["sdf_sha256"]!=sdf_hash:
        raise ValueError("archived collision geometry does not match SDF")
    goal_x,goal_y = local(*score["goal"], pose)
    if not .6 <= goal_x <= 5 or abs(goal_y)>.05:
        raise ValueError("archived goal is outside the forward planner horizon")
    profile = json.loads(profile_path.read_bytes())
    profile["horizon_m"] = goal_x
    snap,observed,inside=project_scan(scan,geometry,profile,site.get("report_calibration","sdf:"+sdf_hash))
    return RecordedScan(snap,profile,geometry,score,pose,scan,world_xml,(goal_x,goal_y),observed,inside)


def export(archive, profile_path):
    window=read_recorded_scan(archive,profile_path)
    snap,profile,geometry,score=window.snapshot,window.profile,window.geometry,window.score
    pose,scan,world_xml=window.pose,window.scan,window.world_xml
    goal_x,goal_y=window.goal
    sdf_hash=geometry["sdf_sha256"]
    observed,inside=window.observed,window.inside
    result=plan(snap,profile)
    if not result["path_body_m"] or all(abs(p[1])<.1 for p in result["path_body_m"]):
        raise ValueError("no computed lateral bypass in this archive")

    shape=next(o for o in geometry["obstacles"] if o["name"]==MODEL+"/collision")
    polygon=[local(x,y,pose) for x,y in shape["poly"]]
    x0,y0=map(min,zip(*polygon))
    x1,y1=map(max,zip(*polygon))
    sx,sy,sz = geometry["box_sizes"][MODEL]
    # This comparison is for one axis-aligned archived box. Never voxelize an unsupported shape.
    if abs((x1-x0)-sx)>.001 or abs((y1-y0)-sy)>.001 or shape["radius"]:
        raise ValueError("archived box orientation or geometry unsupported")
    model=ET.fromstring(world_xml).find(".//model[@name='"+MODEL+"']")
    z0=float(model.findtext("pose").split()[2])-.5*sz
    cell=.10
    voxels=[[round(x0+(i+.5)*cell,3),round(y0+(j+.5)*cell,3),round(z0+(k+.5)*cell,3),1]
            for i in range(round(sx/cell)) for j in range(round(sy/cell)) for k in range(round(sz/cell))]
    ground=.20
    observed_ground=[]
    for gx in range(-5,math.ceil((goal_x+.8)/ground)):
        for gy in range(-10,10):
            x,y=(gx+.5)*ground,(gy+.5)*ground
            if not inside(x,y) and not (x0-ground/2<=x<=x1+ground/2 and y0-ground/2<=y<=y1+ground/2) and all(
                    observed(x+dx,y+dy) for dx in (-.5*ground,.5*ground) for dy in (-.5*ground,.5*ground)):
                observed_ground.append([round(x,2),round(y,2)])
    source={"archive":archive.name,"archive_sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),
            "sdf_sha256":sdf_hash,"scan":"witness/observations.jsonl","scan_stamp_ns":str(scan["stamp_ns"]),
            "witness_pose_stamp_ns":str(pose["stamp_ns"]),"simulation_operation":score["operation"],
            "planner":"perception.rover_preview.planner.plan","profile":profile["profile_id"],
            "geometry":"witness/geometry.json","geometry_model":MODEL,"box_size_m":[sx,sy,sz],
            "box_color":"height","grid_source":"Gazebo collision truth for centered box",
            "ground_source":"archived first LaserScan traversed areas","goal_source":"witness/score.json goal in initial body frame",
            "archive_goal_in_base_m":[goal_x,goal_y],"plan_input":"archived first LaserScan only; simulation clock",
            "valid_finite_hits":len(snap["hits_body_m"]),"beams":len(scan["ranges"])}
    scene={"schema_version":1,"frame_id":"base_link","stamp_ns":str(scan["stamp_ns"]),"voxel_size_m":cell,
           "ground_cell_m":ground,"voxels":voxels,"observed_ground":observed_ground,
           "occupied_ground":sorted({(x,y) for x,y,_,_ in voxels}),
           "bounds_m":{"x":[-1,round(goal_x+.8,2)],"y":[-2,2],"z":[0,round(z0+sz,2)]},
           "texture":{"mode":"unavailable","reason":"simulation_collision_geometry"},
           "source_evidence":{"calibration_ref":snap["calibration_ref"],"sdf_sha256":sdf_hash}}
    result["source_evidence"]={"scan_source":"Gazebo archived planar LaserScan","scan_stamp_ns":str(scan["stamp_ns"])}
    return {"label":"Gazebo 仿真 · 中央箱体绕行","source":"Gazebo 仿真归档","scene":scene,"plan":result,"provenance":source}


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--archive",type=Path,default=ARCHIVE)
    cli.add_argument("--profile",type=Path,default=PROFILE)
    cli.add_argument("--output",type=Path,default=ROOT/"console/web/plan-example.json")
    args=cli.parse_args()
    data=export(args.archive,args.profile)
    args.output.write_text(json.dumps(data,ensure_ascii=False,separators=(",",":"),allow_nan=False)+"\n",encoding="utf-8")
