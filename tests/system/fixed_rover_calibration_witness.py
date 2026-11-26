"""Submit one natural-language task to the isolated fixed-board Gazebo station and audit it externally."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import time
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid
from xml.etree import ElementTree as ET
from zipfile import ZipFile


TASK = "请控制小车自主换位，标定前雷达和相机。标定板固定在地面，以独立留出姿态验证外参。"
TERMINAL = {"SUCCEEDED","BLOCKED","FAILED","REJECTED","CANCELED","RECOVERY_REQUIRED"}
WORLD_TOPIC = "/world/fixed_calibration/dynamic_pose/info"
SDF = "/tmp/fixed-calibration/world.sdf"


def docker(*args, timeout=15):
    return subprocess.run(["docker",*args],check=True,capture_output=True,text=True,encoding="utf-8",
                          errors="replace",timeout=timeout).stdout.strip()


def container(project, service):
    found = docker("ps","-q","--filter",f"label=com.docker.compose.project={project}",
                   "--filter",f"label=com.docker.compose.service={service}").splitlines()
    if len(found)!=1:
        raise ValueError(f"expected one running {project}/{service}")
    return found[0]


def ollama_inventory(agent,endpoint):
    script=("import os,json,sys,urllib.request as u; "
            "print(json.dumps(json.load(u.urlopen(os.environ['EDGE_OLLAMA_URL']+'/api/'+sys.argv[1],timeout=5))))")
    return json.loads(docker("exec",agent,"python3","-c",script,endpoint))


def read_json(origin, path, body=None):
    data = None if body is None else json.dumps(body,ensure_ascii=False).encode()
    req = Request(origin+path, data=data, headers={"Content-Type":"application/json"})
    with urlopen(req,timeout=10) as response:
        return json.load(response)


def save(path, value):
    tmp=path.with_suffix(".writing")
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    tmp.replace(path)


def world_pose(sim, staged_board=False):
    # Read Gazebo's scene broadcaster instead of echoing commanded velocity or wheel odometry.
    topic="/world/fixed_calibration/pose/info" if staged_board else WORLD_TOPIC
    raw=docker("exec",sim,"bash","-lc",
               "source /opt/ros/jazzy/setup.bash && exec timeout 6 gz topic -e --json-output -n 1 -t "+topic)
    messages=[json.loads(line) for line in raw.splitlines() if line.startswith("{")]
    if not messages:
        raise ValueError("Gazebo did not return a world pose message")
    message=messages[-1]
    rows=[pose for pose in message.get("pose",[]) if pose.get("name")=="lingao"]
    if len(rows)!=1:
        raise ValueError("Gazebo world message has no unique lingao model pose")
    p,q=rows[0].get("position",{}),rows[0].get("orientation",{})
    xyz=[float(p.get(k,0)) for k in "xyz"]
    rotation=[float(q.get(k,0)) for k in "xyzw"]
    if not all(map(math.isfinite,xyz+rotation)) or abs(math.hypot(*rotation)-1)>.002:
        raise ValueError("invalid world pose")
    stamp=message["header"]["stamp"]
    result=dict(world="fixed_calibration",model="lingao",source=topic,position_m=xyz,rotation_xyzw=rotation,
                stamp_ns=int(stamp.get("sec",0))*10**9+int(stamp.get("nsec",0)),received_unix_ns=time.time_ns())
    if staged_board:
        boards=[p for p in message["pose"] if p.get("name")=="calibration_board"]
        if len(boards)!=1:
            raise ValueError("expected one original calibration board")
        b=boards[0]
        result["board"]=dict(id=b["id"],position_m=[float(b.get("position",{}).get(k,0)) for k in "xyz"],
                             rotation_xyzw=[float(b.get("orientation",{}).get(k,0)) for k in "xyzw"])
    return result


def angle(a,b):
    dot=abs(sum(x*y for x,y in zip(a,b)) / (math.hypot(*a)*math.hypot(*b)))
    return math.degrees(2*math.acos(min(1.,dot)))


def to_parent(xyz, node):
    pose=node.find("pose")
    if pose is not None and pose.attrib:
        raise ValueError("geometry audit requires the SDF parent frame and radians")
    tx,ty,tz,r,p,y=map(float,(node.findtext("pose") or "0 0 0 0 0 0").split())
    cr,cp,cy=map(math.cos,(r,p,y))
    sr,sp,sy=map(math.sin,(r,p,y))
    x,b,z=xyz
    b,z=cr*b-sr*z,sr*b+cr*z
    x,z=cp*x+sp*z,-sp*x+cp*z
    return (tx+cy*x-sy*b,ty+sy*x+cy*b,tz+z)


def inspect_geometry(world_path, mesh_bytes, tolerance_m=.0001):
    world=ET.parse(world_path).find("world")
    board=world.find("model[@name='calibration_board']")
    rover=world.find("model[@name='lingao']")
    ground=world.find("model[@name='ground']")
    floor=ground.find("link[@name='floor']")
    surface=floor.find("collision[@name='floor']")
    dx,dy,dz=[float(v)/2 for v in surface.findtext("geometry/box/size").split()]
    top=[to_parent(to_parent(to_parent((x,y,dz),surface),floor),ground)[2] for x in (-dx,dx) for y in (-dy,dy)]
    if max(top)-min(top)>tolerance_m:
        raise ValueError("fixture ground is not level")
    meshes=[]
    for link in board.findall("link"):
        for visual in link.findall("visual"):
            mesh=visual.find("geometry/mesh")
            blob=mesh_bytes(mesh.findtext("uri"))
            count,=struct.unpack_from("<I",blob,80)
            if count<1 or len(blob)!=84+50*count:
                raise ValueError("invalid rendered binary STL")
            scale=[float(v) for v in (mesh.findtext("scale") or "1 1 1").split()]
            values=[]
            for tri in struct.iter_unpack("<12fH",blob[84:]):
                for i in (3,6,9):
                    point=[v*s for v,s in zip(tri[i:i+3],scale)]
                    values.append(to_parent(to_parent(to_parent(point,visual),link),board)[2])
            meshes.append(dict(visual=visual.get("name"),sha256=hashlib.sha256(blob).hexdigest(),
                               vertices=len(values),z_min_m=min(values),z_max_m=max(values)))
    bottom=min(m["z_min_m"] for m in meshes)
    drive=rover.find("plugin[@name='gz::sim::systems::DiffDrive']")
    sides={side:[j.text for j in drive.findall(side+"_joint")] for side in ("left","right")}
    tires={}
    for name in sides["left"]+sides["right"]:
        joint=rover.find(f"joint[@name='{name}']")
        link=rover.find(f"link[@name='{joint.findtext('child')}']")
        if joint.get("type")!="revolute" or joint.findtext("parent")!="base_link":
            raise ValueError("drive joint is not attached to the rover chassis")
        tires[name]=float(link.findtext("collision/geometry/cylinder/radius"))
    static=board.findtext("static","false").lower() in ("true","1")
    dynamic=rover.findtext("static","false").lower() in ("false","0")
    if (not static or not dynamic or len(tires)!=4 or min(tires.values())<=0 or
            any(len(s)!=2 for s in sides.values()) or abs(bottom-max(top))>tolerance_m):
        raise ValueError("fixed ground target or dynamic four-wheel drive did not match the loaded world")
    return dict(world=world.get("name"),world_sha256=hashlib.sha256(world_path.read_bytes()).hexdigest(),
                board_static=static,board_rendered_meshes=meshes,board_world_z_min_m=bottom,
                floor_surface_z_m=max(top),contact_tolerance_m=tolerance_m,rover_dynamic=dynamic,
                wheel_joints=sides,wheel_radius_m=tires)


def geometry_witness(sim,out):
    folder=out/"fixture-truth"
    folder.mkdir(exist_ok=True)
    docker("cp",sim+":"+SDF,str((folder/"world.sdf").resolve()))
    def mesh(uri):
        parts=urlsplit(uri)
        p=unquote(parts.path)
        if parts.scheme!="file" or parts.netloc or not re.fullmatch(r"/tmp/fixed-calibration/[a-z_]+\.stl",p):
            raise ValueError("unexpected asset location in the calibration fixture")
        path=folder/p.rsplit("/",1)[1]
        docker("cp",sim+":"+p,str(path.resolve()))
        return path.read_bytes()
    report=inspect_geometry(folder/"world.sdf",mesh)
    save(folder/"geometry.json",report)
    return report


def camera_transform_error(tf, truth):
    if tf["parent_frame"]!=truth["parent_frame"] or tf["child_frame"]!=truth["child_frame"]:
        raise ValueError("solver and SDF transform directions differ")
    x,y,z,w=tf["rotation_xyzw"]
    if abs(math.hypot(x,y,z,w)-1)>1.e-5:
        raise ValueError("invalid fitted quaternion")
    r=[[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
       [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
       [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]]
    trace=sum(a*b for u,v in zip(r,truth["rotation_matrix"]) for a,b in zip(u,v))
    rotation=math.degrees(math.acos(max(-1.,min(1.,(trace-1)/2))))
    translation=math.dist(tf["translation_m"],truth["translation_m"])
    return dict(translation_m=translation,rotation_deg=rotation,within_limits=translation<=.035 and rotation<=1.5)


def copied_captures(session,task,out):
    folder=out/"captures"
    folder.mkdir(exist_ok=True)
    docker("cp",session+":/var/lib/edge-calibration/captures/"+task+"/.",str(folder.resolve()),timeout=180)
    refs={}
    for path in folder.iterdir():
        if not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}",path.stem):
            raise ValueError("invalid capture artifact name")
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        if sha!=path.stem:
            raise ValueError("capture byte digest mismatch: "+path.name)
        refs[path.name]=path.stat().st_size
    return refs


def archive(session, task_id, out):
    state=json.loads(docker("exec",session,"cat","/var/lib/edge-calibration/sessions.json",timeout=35))
    selected=state.get("tasks",{}).get(task_id,{})
    poses=selected.get("poses",[])
    files=copied_captures(session,task_id,out) if poses else {}
    captures=[]
    used_frames=set()
    for row in poses:
        ref=row["sha256"]
        manifest=json.loads((out/"captures"/(ref+".json")).read_bytes())
        if (manifest["operation_id"]!=row["operation_id"] or manifest["video_sha256"]+".avi" not in files
                or any(f["sha256"]+".npz" not in files for f in manifest["frames"])):
            raise ValueError("accepted capture is incomplete")
        stamps=[]
        for frame in manifest["frames"]:
            ref_hash=frame["sha256"]
            if ref_hash in used_frames:
                raise ValueError("a camera and LiDAR frame was reused across accepted poses")
            used_frames.add(ref_hash)
            with ZipFile(out/"captures"/(ref_hash+".npz")) as bundle:
                if not {"rgb.npy","k.npy","cloud_front.npy","metadata.npy"} <= set(bundle.namelist()):
                    raise ValueError("accepted archive lacks a bound RGB or front LiDAR stream")
            stamps.append(int(frame["stamps_ns"]["camera"]))
        if int(row["obs"]["id"]) not in stamps or stamps!=sorted(set(stamps)):
            raise ValueError("sample and recorded-frame timestamps disagree")
        captures.append(dict(manifest=ref,operation=row["operation_id"],observation_id=row["obs"]["id"],
                             frame_count=len(manifest["frames"]),video_sha256=manifest["video_sha256"]))
    report=dict(captures=captures,files=files)
    rev=selected.get("revision")
    if rev:
        if not re.fullmatch(r"[0-9a-f]{64}",rev):
            raise ValueError("invalid spatial revision")
        blob=docker("exec",session,"cat","/var/lib/edge-calibration/revisions/"+rev+".json")
        if hashlib.sha256(blob.encode()).hexdigest()!=rev:
            raise ValueError("spatial revision digest mismatch")
        pub=json.loads(blob)
        if pub["environment"]!="simulation" or pub["task_id"]!=task_id:
            raise ValueError("revision belongs to another task or environment")
        train,held=set(pub["training_ids"]),set(pub["validation_ids"])
        ids={c["observation_id"] for c in captures}
        if (train & held or train|held!=ids or set(pub["samples"])!={c["manifest"] for c in captures}
                or len(train)<pub["method"]["min_fit"] or len(held)<pub["method"]["min_holdout"]):
            raise ValueError("accepted poses and holdout do not match")
        # The revision is the hash of the original bytes, including their serialization.
        target=out/"spatial-publication.json"
        tmp=target.with_suffix(".writing")
        tmp.write_bytes(blob.encode("utf-8"))
        tmp.replace(target)
        report.update(revision=rev,training_ids=sorted(train),holdout_ids=sorted(held),transforms=pub["report"]["transforms"])
    return report


def motion_evidence(task):
    rows=[]
    for index, step in enumerate(task["steps"],1):
        pin=step.get("physical_pin")
        if not pin:
            continue
        evidence=step.get("evidence") or {}
        proof=evidence.get("verification") or {}
        op=f"{task['id']}/step/{index}"
        passed=(step["status"]=="SUCCEEDED" and op==step.get("operation_id")==pin.get("reservation_id")
                and op==proof.get("operation_id") and pin["verifier_id"]!=pin["provider_id"]
                and evidence.get("verifier_id")==pin["verifier_id"] and proof.get("sample_count",0)>=3
                and proof.get("last_ns",0)-proof.get("first_ns",0)>=400_000_000
                and isinstance(proof.get("start_pose"),list) and isinstance(proof.get("stop_pose"),list))
        rows.append(dict(operation_id=op,verified=passed,provider=pin["provider_id"],verifier=pin["verifier_id"],
                         proof=proof))
    return rows


def owner_evidence(controller, task):
    """Read the original owner's journal without offering or settling any operation."""
    expected = {step["operation_id"]:step for step in task["steps"]
                if step.get("physical_pin") and step.get("operation_id")}
    if not expected:
        return []
    script = """import json,sqlite3,sys
db=sqlite3.connect('file:/state/physical.sqlite?mode=ro',uri=True)
db.execute('PRAGMA query_only=ON')
result=[]
for op in sys.argv[1:]:
    row=db.execute('SELECT pin,args,state,reason,proof,settled FROM physical_ops WHERE op=?',(op,)).fetchone()
    if row:
        result.append(dict(operation_id=op,pin=json.loads(row[0]),args=json.loads(row[1]),state=row[2],
                           reason=row[3],proof=json.loads(row[4]) if row[4] else None,settled=bool(row[5])))
print(json.dumps(result))
"""
    rows=json.loads(docker("exec",controller,"python3","-c",script,*expected))
    if len(rows)!=len(expected) or any(row["pin"]!=expected[row["operation_id"]]["physical_pin"] or
                                      row["args"]!=expected[row["operation_id"]]["args"] for row in rows):
        raise ValueError("physical operation absent or owner binding/arguments changed")
    return rows


def run(args):
    project=args.project
    sim,session,console,agent=[container(project,service) for service in ("fixed-sim","calibration-session","console","agent")]
    site=json.loads(docker("inspect",sim))[0]
    if ("GZ_PARTITION=edge_fixed_rover_197" not in site["Config"]["Env"] or
            "ROS_DOMAIN_ID=197" not in site["Config"]["Env"]):
        raise ValueError("unexpected simulation domain or partition")
    port=docker("port",console,"8080/tcp")
    if not re.fullmatch(r"127\.0\.0\.1:[0-9]+",port):
        raise ValueError("console must be bound to one loopback address")
    origin="http://"+port
    site_status=read_json(origin,"/api/calibration-status")
    site_identity=read_json(origin,"/api/state")
    if (site_identity.get("environment")!="simulation" or site_identity.get("robot_id")!="gazebo-lingao"
            or site_identity.get("calibration_status_available") is not True):
        raise ValueError("only simulation is supported by this witness")
    tags=ollama_inventory(agent,"tags")
    matches=[row for row in tags["models"] if row["name"]=="qwen3:4b-instruct"]
    if len(matches)!=1:
        raise ValueError("local station lacks qwen3:4b-instruct")
    out=args.out
    if args.resume:
        requested=json.loads((out/"request.json").read_bytes())
        prior=json.loads((out/"site.json").read_bytes())
        if ((args.task_text is not None and requested["text"]!=args.task_text) or not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,95}",requested["id"],re.ASCII)
                or args.task_id and args.task_id!=requested["id"]
                or prior["project"]!=project or prior["console"]!=origin or prior["model"]["digest"]!=matches[0]["digest"]):
            raise ValueError("saved evidence belongs to a different task or station")
        initial=prior["start_pose"]
    else:
        initial=world_pose(sim,args.staged_board)
        continuation=None
        if args.prior_run:
            previous_site=json.loads((args.prior_run/"site.json").read_bytes())
            previous=json.loads((args.prior_run/"summary.json").read_bytes())
            stopped=previous["stop_pose"]
            if (previous_site["simulator_id"]!=site["Id"] or previous_site["simulator_started_at"]!=site["State"]["StartedAt"]
                    or not previous["independent_final_stop"] or not stopped or stopped["stamp_ns"]>=initial["stamp_ns"]):
                raise ValueError("previous task lacks a confirmed stop in this Gazebo incarnation")
            offset=math.dist(initial["position_m"],stopped["position_m"])
            turn=angle(initial["rotation_xyzw"],stopped["rotation_xyzw"])
            if offset>.002 or turn>.3:
                raise ValueError("Gazebo rover moved between the tasks")
            continuation=dict(prior_task_id=previous["task_id"],prior_stop_world_ns=stopped["stamp_ns"],
                              translation_since_stop_m=offset,rotation_since_stop_deg=turn)
        out.mkdir(parents=True,exist_ok=False)
        identifier=args.task_id or "fixed-cal-"+uuid.uuid4().hex[:16]
        if not re.fullmatch(r"fixed-cal-[a-f0-9]{12,16}",identifier):
            raise ValueError("expected fixed-cal- followed by 12 to 16 lowercase hex digits")
        requested=dict(id=identifier,text=TASK if args.task_text is None else args.task_text)
        if not isinstance(requested["text"],str) or not 1<=len(requested["text"].strip())<=240:
            raise ValueError("expected a short natural-language task")
        if not args.observe_ui:
            save(out/"request.json",requested)
        save(out/"site.json",dict(project=project,model=matches[0],console=origin,start_pose=initial,
                                  simulator_id=site["Id"],simulator_started_at=site["State"]["StartedAt"],
                                  continuation=continuation,
                                  initial_station=site_status,utc=datetime.now(timezone.utc).isoformat()))
    task_id=requested["id"]
    if args.staged_board:
        asset=json.loads(docker("exec",sim,"cat","/fixture/profile.json"))["board_asset"]
        front=[*asset["front_center_xy_m"],asset["outline_m"][1]/2]
        if "board" not in initial or math.dist(initial["board"]["position_m"],front)<.5:
            raise ValueError("expected board to be staged outside its requested final position")
        prompt=out/"operator-prompt.json"
        placed=out/"operator-placement.json"
    if not args.resume:
        geometry_witness(sim,out)
    if args.observe_ui:
        if not args.resume:
            save(out/"witness-ready.json",dict(original_text=requested["text"],world_pose=initial))
            until=time.monotonic()+args.timeout
            receipt_file=out/"ui-receipt.json"
            while not receipt_file.exists() and time.monotonic()<until:
                time.sleep(.25)
            receipt=json.loads(receipt_file.read_bytes())
            if (receipt.get("text")!=requested["text"] or
                    not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,95}",receipt.get("id",""),re.ASCII)):
                raise ValueError("invalid original UI task receipt")
            task_id=receipt["id"]
            requested=dict(id=task_id,text=receipt["text"])
            save(out/"request.json",requested)
        accepted=read_json(origin,"/agent/tasks?"+urlencode({"id":task_id,"limit":1}))
    elif args.resume:
        try:
            accepted=read_json(origin,"/agent/tasks?"+urlencode({"id":task_id,"limit":1}))
        except OSError:
            # Preserve the original ID/payload even if the HTTP submission reply was lost.
            accepted=read_json(origin,"/agent/tasks",requested)
    else:
        accepted=read_json(origin,"/agent/tasks",requested)
    if (accepted["id"],accepted["text"])!=(task_id,requested["text"]):
        raise ValueError("submission identity mismatch")
    track_path=out/"world-poses.jsonl"
    poses=([json.loads(line) for line in track_path.read_text(encoding="utf-8").splitlines() if line]
           if args.resume and track_path.exists() else [])
    if not poses:
        poses=[initial]
    epoch_changed = (args.resume and (prior.get("simulator_id",site["Id"]) != site["Id"] or
                                       prior.get("simulator_started_at",site["State"]["StartedAt"]) != site["State"]["StartedAt"]))
    for i in range(1,len(poses)):
        if poses[i]["stamp_ns"] < poses[i-1]["stamp_ns"]:
            poses=poses[:i]
            epoch_changed=True
            break
    deadline=time.monotonic()+args.timeout
    event_path=out/"events.jsonl"
    cursor=(max((json.loads(line)["seq"] for line in event_path.read_text(encoding="utf-8").splitlines() if line),
                default=0) if args.resume and event_path.exists() else 0)
    last=None
    with track_path.open("a",encoding="utf-8",buffering=1) as track, \
            event_path.open("a",encoding="utf-8",buffering=1) as events:
        if track.tell()==0:
            track.write(json.dumps(initial)+"\n")
        while True:
            query=urlencode(dict(id=task_id,after=cursor,limit=200))
            task=read_json(origin,"/agent/tasks?"+query)
            for row in task.pop("events"):
                events.write(json.dumps(row,ensure_ascii=False)+"\n")
                cursor=row["seq"]
            pose=world_pose(sim,args.staged_board)
            if args.staged_board:
                board=pose["board"]
                if (board["id"]!=initial["board"]["id"] or
                        angle(board["rotation_xyzw"],initial["board"]["rotation_xyzw"])>.15):
                    raise ValueError("original physical target identity or orientation changed")
                at_front=math.dist(board["position_m"],front)<.002
                if not at_front:
                    if (math.dist(board["position_m"],initial["board"]["position_m"])>.002 or placed.exists()
                            or math.dist(pose["position_m"],initial["position_m"])>.002
                            or angle(pose["rotation_xyzw"],initial["rotation_xyzw"])>.25
                            or any(s.get("physical_pin") for s in task["steps"])):
                        raise ValueError("board or robot moved before the requested operator placement")
                    if not prompt.exists() and task["steps"]:
                        observed=read_json(origin,"/api/calibration-status")
                        if (observed.get("task_id")==task_id and observed.get("phase")=="WAITING_FOR_FRONT_BOARD"
                                and (observed.get("capture_feedback") or {}).get("condition")=="front_markers_required"
                                and pose["stamp_ns"]-initial["stamp_ns"]>=400_000_000):
                            save(prompt,dict(task_id=task_id,station=observed,world_pose=pose))
                elif not placed.exists():
                    if not prompt.exists():
                        raise ValueError("target arrived before the original station prompt")
                    save(placed,dict(task_id=task_id,original_board=initial["board"],world_pose=pose))
            if pose["stamp_ns"]<poses[-1]["stamp_ns"]:
                epoch_changed=True
            if not epoch_changed and pose["stamp_ns"]>poses[-1]["stamp_ns"]:
                poses.append(pose)
                track.write(json.dumps(pose)+"\n")
            state=(task["state"],task["cursor"],task["steps"][-1]["tool"] if task["steps"] else None)
            if state!=last:
                print(task_id,*state,task.get("detail",""),flush=True)
                save(out/"task.json",task)
                if task.get("provider") and not (out/"model-runtime.json").exists():
                    loaded=ollama_inventory(agent,"ps")
                    if any(m["digest"]==matches[0]["digest"] for m in loaded["models"]):
                        save(out/"model-runtime.json",loaded)
                last=state
            if task["state"] in TERMINAL and not task.get("events_has_more"):
                if epoch_changed and (out/"physical-owner.json").exists():
                    owners=json.loads((out/"physical-owner.json").read_bytes())
                elif any(s.get("physical_pin") and s.get("operation_id") for s in task["steps"]):
                    owners=owner_evidence(container(project,"view-controller"),task)
                else:
                    owners=[]
                save(out/"physical-owner.json",owners)
                if all(row["state"] in ("SUCCEEDED","FAILED","CANCELED","RESERVED") for row in owners):
                    break
            if time.monotonic()>deadline:
                raise TimeoutError("task still active, no cancellation sent: "+task_id)
            time.sleep(1.)
        # Measure final motion over a separate world-time window after the task owner's terminal state.
        final=stopped=poses[-1]
        for _ in range(12):
            if epoch_changed:
                break
            time.sleep(.4)
            stopped=world_pose(sim,args.staged_board)
            if stopped["stamp_ns"]<final["stamp_ns"]:
                epoch_changed=True
                break
            track.write(json.dumps(stopped)+"\n")
            if stopped["stamp_ns"]-final["stamp_ns"]>=400_000_000:
                break
    seconds=None if epoch_changed else (stopped["stamp_ns"]-final["stamp_ns"])/1e9
    quiet=(seconds is not None and seconds>=.4 and math.dist(stopped["position_m"],final["position_m"])/seconds<=.01 and
           angle(stopped["rotation_xyzw"],final["rotation_xyzw"])/seconds<=1.)
    moved=(max(math.dist(p["position_m"],initial["position_m"]) for p in poses)>.02 or
           max(angle(p["rotation_xyzw"],initial["rotation_xyzw"]) for p in poses)>2)
    captured=archive(session,task_id,out)
    errors={}
    if captured.get("transforms"):
        truth=json.loads(docker("exec",sim,"python3","/fixture/fixed_rover_world.py","truth","/tmp/fixed-calibration/world.sdf"))
        geometry_path=out/"fixture-truth"/"geometry.json"
        if geometry_path.exists() and truth["world_sha256"]!=json.loads(geometry_path.read_bytes())["world_sha256"]:
            raise ValueError("loaded mount truth and witnessed world geometry differ")
        save(out/"mount-truth.json",truth)
        errors={role:camera_transform_error(tf,truth) for role,tf in captured["transforms"].items()}
    save(out/"task.json",task)
    moves=motion_evidence(task)
    report=dict(task_id=task_id,terminal=task["state"],detail=task["detail"],model=task["provider"],
                actual_pose_changed=moved,independent_final_stop=quiet,start_pose=initial,
                stop_pose=None if epoch_changed else stopped,world_epoch_changed=epoch_changed,
                stop_world_window_s=seconds,source_model=matches[0]["digest"],capture=captured,mount_errors=errors,
                physical_steps=moves,physical_owner=owners)
    report["passed"]=(task["state"]=="SUCCEEDED" and task["provider"]=="ollama/qwen3:4b-instruct" and moved and quiet
                      and bool(errors) and all(e["within_limits"] for e in errors.values())
                      and bool(moves) and all(row["verified"] for row in moves))
    if args.staged_board:
        placed_receipts=[s for s in task["steps"] if (s.get("evidence") or {}).get("front_placement_confirmed") is True]
        report["board_placement_witnessed"]=bool(prompt.exists() and placed.exists() and len(placed_receipts)>=1
                                                 and math.dist(stopped["board"]["position_m"],front)<.002)
        report["passed"]=report["passed"] and report["board_placement_witnessed"]
    save(out/"summary.json",report)
    print(json.dumps({k:report[k] for k in ("task_id","terminal","model","passed")},ensure_ascii=False))
    return report["passed"]


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--project",default="edge-fixed-cal")
    cli.add_argument("--timeout",default=1600,type=int)
    cli.add_argument("--out",type=Path,required=True)
    cli.add_argument("--task-id",help="agreed recording correlation ID for a new submission")
    cli.add_argument("--task-text",help="original, unmodified natural language submitted through the public task interface")
    cli.add_argument("--staged-board",action="store_true",help="witness an operator placing the static board after the prompt")
    cli.add_argument("--observe-ui",action="store_true",help="only observe the actual browser submission identified in ui-receipt.json")
    cli.add_argument("--prior-run",type=Path,help="require this run to start at the prior verified stop in the same world")
    cli.add_argument("--resume",action="store_true",help="attach to the same saved task and append observations")
    raise SystemExit(0 if run(cli.parse_args()) else 1)
