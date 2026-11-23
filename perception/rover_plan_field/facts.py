"""Read immutable Jetson sensor artifacts and expose body-frame observations for plan previews."""
import functools
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

if __package__:
    from .scene import build_scene, project_point as project
    from . import rgb_scene
else:
    from scene import build_scene, project_point as project
    import rgb_scene

ROOT=Path("/evidence")
HEX=re.compile(r"[0-9a-f]{64}")
SITE=Path(os.getenv("ROVER_SENSOR_SITE","/site/jetson-rover.json"))


def atomic_content(path,raw):
    if path.is_file():
        return
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        f.write(raw)
    os.replace(f.name,path)


def artifact(directory,kind,suffix,max_age,archive):
    index=json.loads((directory/"observations.json").read_bytes())
    sample=next(s for s in index["samples"] if s.get("source_id")==kind and s["clock_id"]=="unix")
    ref=sample["artifact_ref"].removeprefix("sha256:")
    age=(time.time_ns()-int(sample["stamp_ns"]))/1e9
    if not HEX.fullmatch(ref) or not -.1<=age<=max_age:
        raise ValueError("stale or unbound sensor: "+kind)
    raw=(directory/"artifacts"/(ref+suffix)).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=ref:
        raise ValueError("sensor content digest differs")
    if archive:
        atomic_content(ROOT/"artifacts"/(ref+suffix),raw)
    return sample,raw


@functools.lru_cache(maxsize=1)
def site():
    return json.loads(SITE.read_bytes())


@functools.lru_cache(maxsize=8)
def archived_scene(digest):
    if not HEX.fullmatch(digest):
        raise ValueError("invalid snapshot reference")
    raw=(ROOT/"snapshots"/(digest+".json")).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:
        raise ValueError("snapshot digest mismatch")
    def read_cloud(ref):
        name=ref.removeprefix("sha256:")
        if not HEX.fullmatch(name):
            raise ValueError("invalid cloud reference")
        return (ROOT/"artifacts"/(name+".json")).read_bytes()
    footprint=json.loads((Path(__file__).parent.parent/"rover_preview"/"lingao.json").read_bytes())
    scene=build_scene(json.loads(raw),site(),read_cloud,snapshot_ref="sha256:"+digest,footprint=footprint)
    return json.dumps(scene,separators=(",",":"),allow_nan=False).encode()


def rgbd_scene(digest):
    if digest=="latest":
        def save(ref,suffix,raw):
            if suffix not in (".json",".png") or not re.fullmatch(r"sha256:[0-9a-f]{64}",ref):
                raise ValueError("invalid RGB-D artifact")
            atomic_content(ROOT/"artifacts"/(ref[7:]+suffix),raw)
        mesh=rgb_scene.latest_scene(archive=save)
        raw=json.dumps(mesh,separators=(",",":"),allow_nan=False).encode()
        digest=hashlib.sha256(raw).hexdigest()
        atomic_content(ROOT/"rgbd-scenes"/(digest+".json"),raw)
    else:
        if not HEX.fullmatch(digest):
            raise ValueError("invalid RGB-D capture")
        raw=(ROOT/"rgbd-scenes"/(digest+".json")).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=digest:
            raise ValueError("RGB-D capture digest mismatch")
        mesh=json.loads(raw)
    return json.dumps({**mesh,"scene_ref":"sha256:"+digest},separators=(",",":"),allow_nan=False).encode()


def project_window(configuration,windows):
    # Traversed cells come from measured ray segments inside the reviewed height band.
    # Vertical and unobserved areas between sparse beams remain unknown.
    cell=.045
    free=set()
    occupied={}
    hit_points={}
    def grid(x,y):
        return math.floor(x/cell),math.floor(y/cell)
    for sid,sample,cloud in windows:
        cfg=configuration["sources"][sid]
        if (cloud["source_id"]!=sid or cloud["frame_id"]!=cfg["frame_id"] or
            int(cloud["last_receive_ns"])!=int(sample["stamp_ns"]) or cloud["type"]!="livox.mid360.preview"):
            raise ValueError("sensor source/frame mismatch: "+sid)
        ox,oy,oz=cfg["translation_m"]
        for point in cloud["points"]:
            if len(point)!=4 or not all(math.isfinite(v) for v in point):
                continue
            x,y,z=project(cfg,point)
            dx,dy,dz=x-ox,y-oy,z-oz
            length=math.hypot(dx,dy)
            if length<.05:
                continue
            if .055<=z<=.52 and math.hypot(x,y)<5 and not (-.24<=x<=.265 and -.17<=y<=.17):
                hit_points.setdefault((round(x/.025),round(y/.025)),[round(x,4),round(y,4)])
                i=round(math.atan2(y,x)*180/math.tau)%180
                r=math.hypot(x,y)
                occupied[i]=min(occupied.get(i,math.inf),r)
            steps=math.ceil(min(length,5)/(.5*cell))
            for j in range(steps):
                t=(j+.5)*(.5*cell)/length
                if t>=1: break
                if .055<=oz+t*dz<=.52:
                    free.add(grid(ox+t*dx,oy+t*dy))
    rays=[]
    for i in range(180):
        angle=i*math.tau/180
        co,si=math.cos(angle),math.sin(angle)
        last=0.
        # Inside the modeled body the occupied footprint itself supplies the starting boundary.
        for step in range(1,112):
            r=step*cell
            x,y=co*r,si*r
            inside= -.234<=x<=.259 and -.166<=y<=.166
            if not inside and grid(x,y) not in free:
                break
            last=r
        hit=occupied.get(i)
        rays.append({"angle_rad":math.atan2(si,co),"free_to_m":min(last,hit) if hit else last,"hit_m":hit})
    return rays,list(hit_points.values())


def camera(archive):
    s,raw=artifact(Path("/rgb"),"d435.color.image",".png",2.0,archive)
    return {"source_id":s["source_id"],"stamp_ns":str(s["stamp_ns"]),"ref":s["artifact_ref"],
            "url":"/camera/"+s["artifact_ref"][7:]+".png"},raw


def current_pose():
    try:
        pose=json.loads(Path("/pose/pose.json").read_bytes())
        now=time.time_ns()
        if (pose.get("valid") is True and
                all(-50_000_000<=now-int(pose[k])<=500_000_000 for k in ("wall_received_ns","stamp_ns"))):
            return pose
        return {"valid":False,"reason":"stale_or_invalid_odometry"}
    except (ValueError,OSError,KeyError,TypeError):
        return {"valid":False,"reason":"odometry_unavailable"}


def snapshot():
    cfg=site()
    windows=[]
    for sid in ("mid360.front","mid360.rear"):
        sample,raw=artifact(Path("/lidar")/sid,sid,".json",1.,True)
        windows.append((sid,sample,json.loads(raw)))
    times=[int(s["stamp_ns"]) for _,s,_ in windows]
    if max(times)-min(times)>300_000_000:
        raise ValueError("front and rear lidar receive times differ by more than 300 ms")
    rays,hits=project_window(cfg,windows)
    pose,map_anchor=None,None
    try:
        pose_data=json.loads(Path("/pose/pose.json").read_bytes())
        pose=pose_data.get("odom",pose_data.get("pose",pose_data))
        if (not pose or pose.get("valid") is not True or abs(max(times)-int(pose["stamp_ns"]))>350_000_000 or
                not -.05<=(time.time_ns()-int(pose["wall_received_ns"]))/1e9<=.5):
            pose=None
        if pose:
            map_anchor=pose_data.get("map_from_odom")
    except (ValueError,OSError,KeyError,TypeError):
        pass
    evidence={"projection":"measured ray segments within z=[0.055,0.52] m, 45 mm cells; unknown space retained",
        "calibration_basis":"nominal vehicle-installed URDF","lidars":[
            {"source_id":sid,"stamp_ns":str(s["stamp_ns"]),"artifact_ref":s["artifact_ref"],"frame_id":s["frame_id"],
             "sampled_points":cloud["sampled_point_count"],"stamp_basis":cloud["stamp_basis"]} for sid,s,cloud in windows]}
    try:
        evidence["camera"]=camera(True)[0]
    except (ValueError,StopIteration,OSError):
        evidence["camera"]=None
    snap={"base_frame":"base_foot","source_id":"mid360.front+mid360.rear","scan_stamp_ns":str(max(times)),
        "calibration_ref":cfg["model_evidence"]["digest"],"rays":rays,"hits_body_m":hits,"odom":pose,
        "map_anchor":map_anchor,"source_evidence":evidence}
    raw=json.dumps(snap,separators=(",",":"),allow_nan=False).encode()
    ref=hashlib.sha256(raw).hexdigest()
    atomic_content(ROOT/"snapshots"/(ref+".json"),raw)
    evidence["snapshot_ref"]="sha256:"+ref
    return snap


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=urlsplit(self.path).path
        mime="application/json"
        try:
            if path=="/pose/current":
                body=json.dumps(current_pose(),allow_nan=False).encode()
            elif re.fullmatch(r"/scene/rgbd/(?:latest|[0-9a-f]{64})\.json",path):
                body=rgbd_scene(path.split("/")[-1][:-5])
            elif re.fullmatch(r"/scene/[0-9a-f]{64}\.json",path):
                body=archived_scene(path[7:-5])
            elif path=="/snapshot":
                body=json.dumps(snapshot(),separators=(",",":"),allow_nan=False).encode()
            elif path=="/camera/latest.png":
                _,body=camera(False);mime="image/png"
            elif re.fullmatch(r"/camera/[0-9a-f]{64}\.png",path):
                body=(ROOT/"artifacts"/path.split("/")[-1]).read_bytes();mime="image/png"
            else:
                self.send_error(404);return
        except (OSError,ValueError,KeyError,TypeError,StopIteration) as e:
            self.send_error(503, str(e)[:180]);return
        self.send_response(200);self.send_header("Content-Type",mime);self.send_header("Content-Length",str(len(body)))
        self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(body)


if __name__=="__main__":
    ThreadingHTTPServer(("0.0.0.0",8150),Handler).serve_forever()
