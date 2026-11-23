"""Replay the read-only geometry kernel, measuring computation separately from data acquisition.

The collision oracle uses Gazebo world collision outlines transformed into the initial body
frame. They never enter the planner. Export the inputs once with --save-inputs to benchmark
the same snapshots on another CPU using --inputs. This script does not connect to ROS or HTTP.
"""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from campus_route_geometry import poly_distance


def write(path, data):
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def canonical_digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def archive_input(path, profile):
    from scripts.export_field_scene_example import local, project_scan
    with tarfile.open(path) as archive:
        geometry = json.load(archive.extractfile("witness/geometry.json"))
        world = archive.extractfile("witness/world.sdf").read()
        score = json.load(archive.extractfile("witness/score.json"))
        if not score["passed"] or hashlib.sha256(world).hexdigest() != geometry["sdf_sha256"]:
            raise ValueError("archive truth or SDF integrity check failed")
        site = "site/sim.json"
        calib = json.load(archive.extractfile(site))["report_calibration"] if site in archive.getnames() else "sdf:"+geometry["sdf_sha256"]
        scan, poses = None, []
        for line in archive.extractfile("witness/observations.jsonl"):
            item = json.loads(line)
            if item["kind"] == "scan" and scan is None:
                scan = item
            if item["kind"] == "truth":
                poses.append(item)
                if scan and item["stamp_ns"] >= scan["stamp_ns"]:
                    break
    pose = min(poses, key=lambda p: abs(p["stamp_ns"] - scan["stamp_ns"]))
    if pose["frame"] != "world" or abs(pose["stamp_ns"] - scan["stamp_ns"]) > 50_000_000:
        raise ValueError("missing matching world truth pose")
    goal = local(*score["goal"], pose)
    if not .6 <= goal[0] <= 5 or abs(goal[1]) > .05:
        raise ValueError("unsupported local goal")
    tuned = {**profile, "horizon_m": goal[0]}
    snap, _, _ = project_scan(scan, geometry, tuned, calib)
    truth = [{**r, "poly":[local(*p, pose) for p in r["poly"]]} for r in geometry["obstacles"]]
    return {"snapshot":snap, "profile":tuned, "truth":truth, "goal":goal,
            "source":str(path.relative_to(ROOT)).replace("\\","/"),
            "source_sha256":hashlib.sha256(path.read_bytes()).hexdigest()}


def cases():
    from scripts.export_field_scene_example import PROFILE
    assets = ROOT/"artifacts/verification"
    profile = json.loads(PROFILE.read_bytes())
    clear = archive_input(assets/"lingao-footprint-20261123/local/footprint-clear.tar.gz", profile)
    center = archive_input(assets/"rover-maneuver-20261123/local/centered.tar.gz", profile)
    file = assets/"jetson-field-plan-20261123/verified/sensor-evidence/snapshot.json"
    raw=file.read_bytes()
    task=json.loads((file.parent.parent/"task.json").read_bytes())
    receipt=next(step["evidence"] for step in task["steps"] if step["tool"]=="preview_rover_obstacle_path")
    digest=hashlib.sha256(raw).hexdigest()
    if receipt["source_evidence"]["snapshot_ref"]!="sha256:"+digest:
        raise ValueError("field snapshot is not the one originally bound to the planning task")
    field = {"snapshot":json.loads(raw), "profile":profile,
             "source":str(file.relative_to(ROOT)).replace("\\","/"), "source_sha256":digest}
    stress = {**center["snapshot"], "source_id":"synthetic.load", "hits_body_m":[*center["snapshot"]["hits_body_m"],
              *[(i*.065-1.5, side*(2.0+j*.055)) for side in (-1,1) for i in range(60) for j in range(25)]]}
    overflow = {**center["snapshot"], "hits_body_m":[(.75+i*.025,.8+j*.025) for i in range(80) for j in range(80)]}
    lost = {**field["snapshot"],"rays":[],"hits_body_m":[]}
    return {"clear":clear, "centered":center, "field_hold":field,
            "unknown":{"snapshot":lost,"profile":profile,"source":"synthetic complete dropout"},
            "dense":{"snapshot":stress,"profile":center["profile"],"source":"distinct 3000 synthetic lateral returns plus recorded centered scan"},
            "over_budget":{"snapshot":overflow,"profile":profile,"source":"synthetic over-budget grid of 6400 returns"}}


def curve_samples(segments, radius):
    poses = []
    motion_bound = 0.
    curve_peak = 0.
    for seg in segments:
        x0,x1,y0,y1 = (seg[k] for k in ("x0_m","x1_m","y0_m","y1_m"))
        length,lat = x1-x0, y1-y0
        assert length > 0
        if poses:
            assert math.dist(poses[-1][:2], (x0,y0)) < 1e-6
        steps = max(1,math.ceil(length/.005))
        # max |smootherstep'| = 15/8, max |smootherstep''| = 10/sqrt(3).
        vmax = math.hypot(1., 1.875*lat/length)
        yaw_bound = abs(lat)*10/math.sqrt(3)/length**2
        motion_bound = max(motion_bound, .5*(vmax+radius*yaw_bound)*length/steps)
        for j in range(steps+1):
            t = j/steps
            y = y0+lat*t**3*(10+t*(-15+6*t))
            d1 = lat/length*30*t*t*(1-t)**2
            d2 = lat/length**2*60*t*(1-t)*(1-2*t)
            k = d2/(1+d1*d1)**1.5
            curve_peak=max(curve_peak,abs(k))
            poses.append((x0+t*length,y,math.atan(d1)))
    return poses,motion_bound,curve_peak


def legacy_samples(points, radius):
    if not points:
        return [],0.,None
    x,y = points[0]
    yaw = 0.
    poses = [(x,y,yaw)]
    for bx,by in [*points[1:],points[-1]]:
        length = math.hypot(bx-x,by-y)
        heading = math.atan2(by-y,bx-x) if length else 0.
        delta = math.remainder(heading-yaw,math.tau)
        n = max(1, math.ceil(abs(delta)/.01))
        poses.extend((x,y,yaw+delta*j/n) for j in range(1,n+1))
        n = max(1, math.ceil(length/.005))
        poses.extend((x+(bx-x)*j/n,y+(by-y)*j/n,heading) for j in range(1,n+1))
        x,y,yaw=bx,by,heading
    return poses, .5*max(.005,radius*.01), None


def bounds(poly):
    xs,ys=zip(*poly)
    return min(xs),min(ys),max(xs),max(ys)


def clearance(poses,foot,truth):
    corners=[(foot[k]*sx,foot[v]*sy) for k,sx,v,sy in
             (("front_m",1,"left_m",1),("front_m",1,"right_m",-1),("rear_m",-1,"right_m",-1),("rear_m",-1,"left_m",1))]
    shapes=[(s["poly"],s["radius"],bounds(s["poly"])) for s in truth]
    best=math.inf
    for x,y,angle in poses:
        c,s=math.cos(angle),math.sin(angle)
        body=[(x+bx*c-by*s,y+bx*s+by*c) for bx,by in corners]
        b=bounds(body)
        for poly,r,box in shapes:
            gap=math.hypot(max(0.,b[0]-box[2],box[0]-b[2]),max(0.,b[1]-box[3],box[1]-b[3]))-r
            if gap>=best:
                continue
            if len(poly)==1:
                d=poly_distance(poly[0],poly[0],body)
            else:
                # Check both containments as well as intersecting edges.
                d=min(min(poly_distance(a,z,poly) for a,z in zip(body,body[1:]+body[:1])),
                      poly_distance(poly[0],poly[0],body))
            best=min(best,d-r)
    return best


def describe(item,result):
    points = result["path_body_m"]
    traj=result.get("trajectory") or {}
    segments=traj.get("segments_body")
    foot=item["profile"]; radius=math.hypot(max(foot["front_m"],foot["rear_m"]),max(foot["left_m"],foot["right_m"]))
    report={k:result.get(k) for k in ("status","reason","minimum_clearance_m","present_min_clearance_m","coverage_fraction_on_path")}
    report.update(sample_points=len(points),path_length_m=sum(math.dist(a,b) for a,b in zip(points,points[1:])),
                  goal_error_m=math.dist(points[-1],item["goal"]) if points and "goal" in item else None)
    if not points:
        return report
    headings=[math.atan2(b[1]-a[1],b[0]-a[0]) for a,b in zip(points,points[1:]) if a!=b]
    turns=[abs(math.degrees(math.remainder(b-a,math.tau))) for a,b in zip([0.,*headings],[*headings,0.])]
    report["max_rendered_heading_step_deg"]=max(turns,default=0.)
    if "truth" in item:
        poses,bound,k=curve_samples(segments,radius) if segments else legacy_samples(points,radius)
        nearest=clearance(poses,foot,item["truth"])
        report.update(curvature_peak_sampled_inv_m=k,oracle_pose_samples=len(poses),
                      world_footprint_sample_min_m=nearest,world_continuous_clearance_lower_bound_m=nearest-bound,
                      world_between_sample_bound_m=bound,oracle_motion="quintic" if segments else "polyline pivots")
        if nearest-bound<foot["margin_m"]:
            raise AssertionError(f"Gazebo collision truth clearance {nearest-bound:.4f} below margin")
        if segments and (max(abs(poses[i][2]) for i in (0,-1))>1e-6 or
                         math.dist(poses[-1][:2],points[-1])>1e-5):
            raise AssertionError("analytic body poses disagree with output endpoints")
        if segments:
            bound_k = traj["max_curvature_bound_inv_m"]
            limit = traj["curvature_limit_inv_m"]
            if k > bound_k + 1e-6 or bound_k > limit + 1e-6:
                raise AssertionError("independent dense curvature exceeds certified bounds")
            for x,y in points:
                seg=next(s for s in segments if s["x0_m"]-1e-7<=x<=s["x1_m"]+1e-7)
                t=(x-seg["x0_m"])/(seg["x1_m"]-seg["x0_m"])
                expected=seg["y0_m"]+(seg["y1_m"]-seg["y0_m"])*t**3*(10+t*(-15+6*t))
                if abs(expected-y) > 1e-6:
                    raise AssertionError("published path sample differs from the audited polynomial")
            report.update(curvature_limit_inv_m=limit,certified_max_curvature_inv_m=bound_k)
    return report


def admission(item):
    """Exercise the actual service object with deterministic, injected archive and deadline clocks."""
    from perception.rover_preview.evaluator import EvaluationError, SnapshotEvaluator, VERSION
    snap=item["snapshot"]
    source=int(snap["scan_stamp_ns"])
    times=[source,0]
    def monotonic():
        times[1]+=10_000
        return times[1]
    service=SnapshotEvaluator(item["profile"],wall=lambda:times[0],monotonic=monotonic)
    body={"schema_version":1,"provider_version":VERSION,"clock_id":"unix","snapshot":snap,
          "objective":{"kind":"static_local_path","frame_id":snap["base_frame"]}}
    results={}
    def check(label, request, expected):
        try:
            service.evaluate(request)
        except EvaluationError as error:
            receipt=error.response()
            if (error.code!=expected or receipt["path_body_m"] or receipt["motion_authorized"]
                    or receipt["planned_endpoint"] is not None):
                raise AssertionError(f"unsafe admission on {label}: {error.code}") from error
            results[label]=error.code
        else:
            raise AssertionError(f"unexpected acceptance on {label}")
    check("dynamic_objective",{**body,"objective":{**body["objective"],"kind":"dynamic_local_path"}},"planning.unsupported")
    check("unrecognized_tracks",{**body,"snapshot":{**snap,"dynamic_tracks":[{"id":1}]}},"planning.unsupported")
    check("wrong_goal_frame",{**body,"objective":{**body["objective"],"frame_id":"map"}},"planning.frame_mismatch")
    check("wrong_provider",{**body,"provider_version":"expired-binding"},"planning.binding_mismatch")
    check("oversized_input",{**body,"snapshot":{**snap,"hits_body_m":[[4.,3.]]*4097}},"planning.input_budget")
    times[0]+=500_000_000
    check("stale",body,"planning.stale")
    times[0]=source
    with service.computation():
        check("contending_evaluation",body,"planning.busy")
    # Counterfeit only time in the local test clock, never the archived source stamp.
    ticks=iter((0,60_000_000))
    service.monotonic=lambda:next(ticks,60_000_000)
    check("expired_budget",body,"planning.deadline")
    return {"clock":"deterministic offline injection; original acquisition times retained","checks":results}


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--iterations",type=int,default=25)
    cli.add_argument("--inputs",type=Path)
    cli.add_argument("--save-inputs",type=Path)
    cli.add_argument("--output",type=Path,required=True)
    cli.add_argument("--planner-file",type=Path,help="Optional trusted legacy standalone planner for before/after comparison.")
    args=cli.parse_args()
    if args.iterations < 3:
        cli.error("at least 3 measurements per case")
    if args.planner_file:
        spec=importlib.util.spec_from_file_location("archived_rover_preview",args.planner_file)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        from perception.rover_preview import planner as module
    inputs=json.loads(args.inputs.read_bytes()) if args.inputs else cases()
    if args.save_inputs:
        write(args.save_inputs,inputs)
    result={"method":"warmed sequential kernel plan(snapshot,profile); acquisition, verification and transport excluded",
            "host":platform.platform(),"machine":platform.machine(),"python":platform.python_version(),
            "planner_sha256":hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
            "iterations":args.iterations,"inputs_sha256":canonical_digest(inputs),"cases":{}}
    if not args.planner_file:
        result["module_sha256"]={name:hashlib.sha256((Path(module.__file__).parent/name).read_bytes()).hexdigest()
                                for name in ("trajectory.py","evaluator.py")}
        result["admission"]=admission(inputs["centered"])
    for name,item in inputs.items():
        snap,profile=item["snapshot"],item["profile"]
        output=module.plan(snap,profile)
        times=[]
        for _ in range(args.iterations):
            start=time.perf_counter_ns()
            value=module.plan(snap,profile)
            times.append((time.perf_counter_ns()-start)/1e6)
            if value["status"]!=output["status"] or value["path_body_m"]!=output["path_body_m"]:
                raise AssertionError("nondeterministic stationary geometry")
        row=describe(item,output)
        if name in ("field_hold","unknown","over_budget") and (row["status"]!="blocked" or row["sample_points"]):
            raise AssertionError(f"hold regression on {name}")
        if name in ("clear","centered") and not row["sample_points"]:
            raise AssertionError(f"no path on known {name} benchmark")
        row.update(source=item["source"],source_sha256=item.get("source_sha256"),
                   input_raw_hits=len(snap["hits_body_m"]),rays=len(snap["rays"]),
                   p50_ms=statistics.median(times),p95_ms=sorted(times)[math.ceil(.95*len(times))-1],max_ms=max(times))
        result["cases"][name]=row
    write(args.output,result)
    for name,r in result["cases"].items():
        print(f"{name:<12} {r['status']:<8} p50={r['p50_ms']:.3f} p95={r['p95_ms']:.3f} ms")


if __name__=="__main__":
    main()
