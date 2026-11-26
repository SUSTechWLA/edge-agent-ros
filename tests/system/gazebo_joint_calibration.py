"""Observe a browser-submitted joint task and respond only to its attended board prompts."""
from argparse import ArgumentParser
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlencode

from fixed_rover_calibration_witness import (
    camera_transform_error, container, docker, motion_evidence, owner_evidence, read_json, save, world_pose,
)
from hashlib import sha256


TERMINAL = {"SUCCEEDED","FAILED","BLOCKED","REJECTED","CANCELED","RECOVERY_REQUIRED"}


def acceptance(task, session, sim, output, placements, start, end, controller=None):
    result = dict(task_id=task["id"],terminal=task["state"],provider=task.get("provider"),
                  steps=len(task["steps"]),rover_start=start,rover_stop=end,operator_poses=len(placements),passed=False)
    if task["state"]!="SUCCEEDED":
        return result
    last = task["steps"][-1]
    verified = last.get("evidence") or {}
    revision = verified.get("revision")
    if (last["tool"]!="verify_camera_intrinsics" or verified.get("intrinsics_verified") is not True
            or verified.get("extrinsics_verified") is not True or not re.fullmatch("[a-f0-9]{64}",str(revision))):
        raise ValueError("task did not prove both requested products")
    raw = docker("exec",session,"cat",f"/var/lib/edge-calibration/camera-revisions/{revision}.json")
    if sha256(raw.encode()).hexdigest()!=revision:
        raise ValueError("calibration revision digest differs")
    (output/"publication.json").write_bytes(raw.encode())
    publication = json.loads(raw)
    camera, validation = publication["camera"],publication["validation"]
    train, held = set(camera["fit_ids"]),set(validation["validation_ids"])
    if (publication["task_id"]!=task["id"] or publication["environment"]!="simulation"
            or len(train)<7 or len(held)<3 or train&held):
        raise ValueError("invalid independent split")
    samples = publication["samples"]
    if ({s["id"] for s in samples if s["role"]=="fit"}!=train or
            {s["id"] for s in samples if s["role"]=="holdout"}!=held):
        raise ValueError("sample roles differ from the solver's actual inputs")
    captures = output/"captures"
    captures.mkdir(exist_ok=True)
    for row in samples:
        digest = row["archive_sha256"]
        if not re.fullmatch("[a-f0-9]{64}",digest):
            raise ValueError("bad source archive reference")
        file = captures/(digest+".npz")
        docker("cp",f"{session}:/var/lib/edge-calibration/camera-captures/{task['id']}/{file.name}",str(file))
        if sha256(file.read_bytes()).hexdigest()!=digest:
            raise ValueError("source archive digest differs")
    truth = json.loads(docker("exec",sim,"bash","-lc",
                       "source /opt/ros/jazzy/setup.bash && python3 /fixture/fixed_rover_world.py truth /tmp/fixed-calibration/world.sdf"))
    save(output/"mount-truth.json",truth)
    error = camera_transform_error(validation["extrinsics"]["transforms"]["front"],truth)
    coverage = publication["coverage"]
    if not controller and not {"common","bottom","holdout"} <= {p["aim"] for p in placements}:
        raise ValueError("recording lacks observed coverage, collection and a held-out operator action")
    if not any(p["aim"].startswith("pitch") for p in placements) or not any(p["aim"].startswith("roll") for p in placements):
        raise ValueError("no prompted pitch or roll evidence")
    result.update(revision=revision,training=len(train),holdout=len(held),rgb=coverage,extrinsic_mount_error=error,
                  intrinsic_rms_px=validation["holdout_rms_px"],intrinsic_max_px=validation["holdout_max_px"],
                  move_commands=[s["tool"] for s in task["steps"] if s.get("physical_pin")])
    if controller:
        motions = motion_evidence(task)
        owner = owner_evidence(controller,task)
        save(output/"physical-owner.json",owner)
        stops = {row["operation_id"]: row["proof"]["sensor_stamp_ns"] for row in motions
                 if row["verified"] and row["proof"].get("sensor_clock_id")=="gazebo_sim"}
        assisted = [row for row in samples if row.get("source")=="operator_tilt"]
        automatic = [row for row in samples if row.get("source")=="stationary_view" and row.get("motion_operation")]
        baseline = any(int(row["operation_id"].rpartition("/")[2]) < int(assisted[0]["operation_id"].rpartition("/")[2])
                       for row in automatic) if assisted else False
        operations = {step.get("operation_id"):step for step in task["steps"]}
        plans = [operations[p["assessment_operation_id"]]["evidence"] for p in placements if p["aim"]!="common"]
        valid = (bool(motions) and len(stops)==len(motions)==len(owner) and assisted and baseline
                 and all(row["state"]=="SUCCEEDED" and row["settled"] for row in owner)
                 and all(int(row["id"])>stops.get(row["motion_operation"],math.inf) for row in automatic+assisted)
                 and all(p["camera_operator_needed"] and p["capture_reachability"]["useful_safe_views"]==0 for p in plans))
        result.update(verified_stops=motions,mobile_samples=len(automatic),assisted_samples=len(assisted),
                      initial_placements=sum(p["aim"]=="common" for p in placements))
        valid = valid and result["initial_placements"]==1
    else:
        valid = not result["move_commands"] and math.dist(start,end)<.002
    result["passed"] = bool(error["within_limits"] and valid and
                           {"bottom_left","bottom_center","bottom_right"}<=set(coverage["covered_cells"]))
    return result


def run(origin, project, output, expected_task, resume=False, mobile=False):
    sim = container(project,"fixed-sim")
    session = container(project,"calibration-session")
    controller = container(project,"view-controller") if mobile else None
    site = read_json(origin,"/api/state")
    if site["environment"]!="simulation" or site["robot_id"]!="gazebo-lingao":
        raise ValueError("fixture operates on local Gazebo only")
    ready = output/"witness-ready.json"
    history = output/"operator-placements.json"
    if resume:
        initial = json.loads(ready.read_bytes())
        placements = json.loads(history.read_bytes()) if history.is_file() else []
        pose = world_pose(sim,True)
        if (not mobile and math.dist(pose["position_m"],initial["position_m"])>.002) or pose["board"]["id"]!=initial["board"]["id"]:
            raise ValueError("cannot resume with a different Gazebo pose or target")
    else:
        output.mkdir(parents=True,exist_ok=False)
        initial = world_pose(sim,True)
        placements = []
    if math.dist(initial["board"]["position_m"],[0,4,.5])>.01:
        raise ValueError("operator exercise must start with an empty camera view")
    if not resume:
        save(ready,initial)
    receipt = output/"ui-receipt.json"
    expiry = time.monotonic()+1100
    while not receipt.exists() and time.monotonic()<expiry:
        time.sleep(.35)
    source = json.loads(receipt.read_bytes())
    task_id = source["id"]
    if source["text"]!=expected_task or not re.fullmatch("[a-zA-Z0-9_.-]{1,96}",task_id):
        raise ValueError("unexpected frontend task")
    prompts = {}
    captured = [p["xyz_rpy"] for p in placements]
    last = None
    while time.monotonic()<expiry:
        task = read_json(origin,"/agent/tasks?"+urlencode(dict(id=task_id,limit=1)))
        if task.get("text")!=expected_task:
            raise ValueError("task does not match workbench submission")
        key = task["state"],task["cursor"],len(task["steps"])
        if key!=last:
            print(task_id,*key,task.get("detail",""),flush=True)
            save(output/"task.json",task)
            last=key
        if task["state"] in TERMINAL:
            end = world_pose(sim,True)["position_m"]
            summary = acceptance(task,session,sim,output,placements,initial["position_m"],end,controller)
            save(output/"summary.json",summary)
            print(json.dumps(summary,ensure_ascii=False),flush=True)
            return summary["passed"]
        status = read_json(origin,"/api/calibration-status")
        feedback = status.get("capture_feedback") or {}
        op = feedback.get("capture_operation")
        if mobile and status.get("task_id")==task_id and status.get("phase")=="WAITING_FOR_FRONT_BOARD" and not placements:
            op = status["active_operation"]
            first_seen,_ = prompts.setdefault(op,(time.monotonic(),0.))
            if time.monotonic()-first_seen>=3.:
                docker("exec",sim,"bash","-lc","source /opt/ros/jazzy/setup.bash && python3 /fixture/fixed_rover_world.py "
                       "place-front /tmp/fixed-calibration/world.sdf --profile /fixture/profile.json")
                pose = world_pose(sim,True)
                action = dict(aim="common",xyz_rpy=[*pose["board"]["position_m"],0.,0.,0.],
                              operation_id=op,world_pose=pose)
                save(history,[action])
                placements.append(action)
                captured.append(action["xyz_rpy"])
                print(op,"initial board placed",flush=True)
        if status.get("task_id")==task_id and status.get("phase")=="WAITING_FOR_TILT" and op==status.get("active_operation"):
            first_seen, last_move = prompts.setdefault(op,(time.monotonic(),0.))
            now = time.monotonic()
            if now-first_seen>=3.0 and now-last_move>=11. and (not mobile or not any(p["operation_id"]==op for p in placements)):
                pose = world_pose(sim,True)
                if not mobile and math.dist(pose["position_m"],initial["position_m"])>.002:
                    raise ValueError("robot moved before operator placement")
                if pose["board"]["id"]!=initial["board"]["id"]:
                    raise ValueError("target identity changed")
                extra = []
                if mobile:
                    running = task["steps"][-1]
                    assessments = [s for s in task["steps"] if (s.get("evidence") or {}).get("camera_operator_needed") is True]
                    if (running["tool"]!="adjust_camera_calibration_board" or running["operation_id"]!=op
                            or not assessments or not motion_evidence(task)[-1]["verified"]):
                        raise ValueError("no independent motion stop and current missing-coverage decision")
                    assessment_op = assessments[-1]["operation_id"]
                    extra = ["--fixed-robot",json.dumps(pose,separators=(",",":")),
                             "--fixed-xy",json.dumps(placements[0]["xyz_rpy"][:2],separators=(",",":"))]
                action = json.loads(docker("exec",sim,"bash","-lc",'source /opt/ros/jazzy/setup.bash && exec python3 "$@"',
                    "joint-board","/tmp/intrinsic_board_operator.py",feedback["next_pose"],"--missing",
                    json.dumps(feedback["missing_cells"],separators=(",",":")),"--previous",
                    json.dumps(captured,separators=(",",":")),*extra,timeout=25))
                if mobile:
                    action.update(assessment_operation_id=assessment_op,world_pose=world_pose(sim,True))
                captured.append(action["xyz_rpy"])
                placements.append(dict(action,operation_id=op,guidance=feedback["guidance"],missing=feedback["missing_cells"]))
                save(output/"operator-placements.json",placements)
                print(op,action,flush=True)
                prompts[op] = first_seen,time.monotonic()
        time.sleep(.65)
    raise TimeoutError("joint task did not reach a terminal state")


if __name__=="__main__":
    parser=ArgumentParser(description=__doc__)
    parser.add_argument("--origin",default="http://127.0.0.1:8916",choices=["http://127.0.0.1:8916"])
    parser.add_argument("--project",default="edge-fixed-cal",choices=["edge-fixed-cal"])
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--task",required=True)
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--mobile",action="store_true")
    args=parser.parse_args()
    raise SystemExit(0 if run(args.origin,args.project,args.output,args.task,args.resume,args.mobile) else 1)
