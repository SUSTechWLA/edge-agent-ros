"""Build the observer's copy of the full campus. The exploration client gets no site files."""
import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import subprocess
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
SOURCE_VOLUME = "edge-agent-campus_campus-world"
ASSETS = ROOT/"artifacts/sim-assets"
SCAN_RANGE_M = 2.1


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+"\n",encoding="utf8")


def prepare_world(out, image, assets=ASSETS):
    """Snapshot generated meshes and world into out/site, then add the independent camera."""
    out=Path(out).resolve()
    site=out/"site"
    site.mkdir(parents=True,exist_ok=False)
    if not (assets/"models/Pine Tree/model.sdf").is_file():
        raise FileNotFoundError("campus assets missing; run scene/campus_large/fetch_assets.py")
    prefix=["docker","run","--rm","--network","none","--mount",f"type=bind,src={site},dst=/site",
            "--mount",f"type=bind,src={assets},dst=/assets,readonly"]
    if subprocess.run(["docker","volume","inspect",SOURCE_VOLUME],capture_output=True).returncode==0:
        subprocess.run(prefix+["--mount",f"type=volume,src={SOURCE_VOLUME},dst=/from,readonly",
                 "--entrypoint","sh",image,"-ec",
                 "cp -r /from/meshes /from/robots /from/real_site /site/ && cp /from/world.sdf /site/world.sdf"],check=True)
        source=SOURCE_VOLUME
    else:
        subprocess.run(prefix+[image,"python3","-B","/opt/edge/scene/campus_large/build.py",
                      "--assets","/assets","--base","/opt/edge/sim/world.sdf","--output","/site"],check=True)
        source="scene/campus_large/build.py"
    original=(site/"world.sdf").read_bytes()
    sdf=ET.ElementTree(ET.fromstring(original))
    world=sdf.find("world")
    ids=[m.get("name") for m in world.findall("model")]
    if not {"edge_rover","teaching","delft_surveyed_site","track","woods","road","lawn"} <= set(ids):
        raise ValueError("missing full campus geometry in generated world")
    collisions=[ET.tostring(c) for c in world.iter("collision")]
    scan=world.find("./model[@name='edge_rover']//sensor[@name='horizontal_lidar']/lidar/range/max")
    original_range=float(scan.text)
    scan.text=str(SCAN_RANGE_M)
    # Subtract upper storeys from the observer's camera mask; physical collisions
    # and the rover lidar's default all-bits visibility still include these parts.
    masked=[]
    for visual in world.findall("./model[@name='teaching']/link/visual"):
        pos=list(map(float,visual.findtext("pose","0 0 0 0 0 0").split()))
        if pos[2]>2.3:
            ET.SubElement(visual,"visibility_flags").text="2"
            masked.append(visual.get("name"))
    world.append(ET.fromstring('''<model name="unknown_campus_observer"><static>true</static>
      <pose>0 -3.4 7.2 0 1.14 1.5707963268</pose><link name="mount">
      <sensor type="camera" name="overview"><topic>/commission/overview</topic><always_on>true</always_on>
      <update_rate>6</update_rate><camera><visibility_mask>1</visibility_mask><horizontal_fov>1.08</horizontal_fov>
      <image><width>768</width><height>512</height><format>R8G8B8</format></image>
      <clip><near>0.05</near><far>35</far></clip></camera></sensor></link></model>'''))
    if [ET.tostring(c) for c in world.iter("collision")]!=collisions:
        raise ValueError("fixture altered the campus collision geometry")
    output=site/"observed-campus.sdf"
    sdf.write(output,encoding="utf8",xml_declaration=True)
    descriptor=dict(source=source, source_sha256=hashlib.sha256(original).hexdigest(),
                    world_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),original_models=ids,
                    collision_elements=len(collisions), teaching_collisions=len(world.findall("./model[@name='teaching']//collision")),
                    pine_includes=sum(e.findtext("name","").startswith("pine_") for e in world.findall("include")),
                    original_scan_range_m=original_range,test_scan_range_m=SCAN_RANGE_M,
                    masked_for_observer=len(masked),observation_topic="/commission/overview",
                    map_source="/edge/perception/map", pose_source="/edge/perception/pose",
                    scope="full campus physical world, operation confined to teaching entrance local odometry")
    write_json(out/"campus-world.json",descriptor)
    return output


def services(image, domain, partition, out, assets=ASSETS):
    """Compose overrides for the simulation only; all scene mounts stay in sim."""
    site=Path(out).resolve()/"site"
    if not (site/"observed-campus.sdf").is_file():
        raise FileNotFoundError(site/"observed-campus.sdf")
    env={"GZ_PARTITION":partition,"ROS_DOMAIN_ID":str(domain)}
    common={name:{"environment":dict(env)} for name in ("sim","bridge","witness","perception")}
    common["sim"].update(command=["gz","sim","-s","-r","--headless-rendering","/campus/observed-campus.sdf"],
                         volumes=[{"type":"bind","source":str(src),"target":dst,"read_only":True}
                                  for src,dst in ((site,"/campus"),(assets,"/assets"))])
    common["sim"]["environment"].update(GZ_SIM_RESOURCE_PATH="/assets/models:/assets:/campus",LP_NUM_THREADS="6")
    return common


def grid_audit(raw, start, goal, radius):
    """Replay a conservative swept square against retained cells without calling the planner."""
    w,h,res=raw["width"],raw["height"],raw["resolution"]
    ox,oy,yaw=raw["origin"]
    pad=math.ceil(radius/res)
    slices=max(1,math.ceil(math.dist(start,goal)/(res/10)))
    required=set()
    for j in range(slices+1):
        x,y=[a+(b-a)*j/slices for a,b in zip(start,goal)]
        dx,dy=x-ox,y-oy
        ix=math.floor((math.cos(yaw)*dx+math.sin(yaw)*dy)/res)
        iy=math.floor((-math.sin(yaw)*dx+math.cos(yaw)*dy)/res)
        required.update((cx,cy) for cx in range(ix-pad,ix+pad+1) for cy in range(iy-pad,iy+pad+1))
    bad=sum(1 for cx,cy in required if not (0<=cx<w and 0<=cy<h and raw["cells"][cy*w+cx]==0))
    return {"checked_cells":len(required),"nonfree_cells":bad,"length_m":math.dist(start,goal)}


def audit(out,recording_id):
    """Check the successful survey against the independent ROS map, Gazebo pose and stop stream."""
    out=Path(out)
    media=out/"session"/recording_id/"media/mission"
    events=[json.loads(line) for line in (media/"timeline.jsonl").read_bytes().splitlines() if line.strip()]
    reports=[(p,json.loads(p.read_bytes())) for p in (out/"work").glob("*/report.json")]
    matches=[(p,v) for p,v in reports if recording_id in (v.get("trace_id"),v.get("operation_id"))]
    if len(matches)!=1:
        raise ValueError("no unique recorded worker report for "+recording_id)
    path,report=matches[0]
    raw_events=[json.loads(l) for l in (path.parent/"events.jsonl").read_bytes().splitlines()]
    truth=sorted((r for r in events if r["kind"]=="world_pose"),key=lambda r:r["stamp_ns"])
    twists=sorted((r for r in events if r["kind"]=="twist"),key=lambda r:r["stamp_ns"])
    maps={r["stamp_ns"]:r for r in events if r["kind"]=="map"}
    if not truth or not maps or not twists:
        raise ValueError("incomplete independent Gazebo/ROS witness")
    steps=[]
    issues=[]
    def demand(ok,issue):
        if not ok:
            issues.append(issue)
    def closest(row,field):
        return min(truth,key=lambda s:abs(s[field]-row[field]))
    for s in report["steps"]:
        data=(path.parent/s["grid_file"]).read_bytes()
        grid=json.loads(gzip.decompress(data))
        measured_map=maps.get(s["map_stamp_ns"])
        matches_wire=None
        if measured_map is not None:
            wire=json.loads((media/measured_map["file"]).read_bytes())
            matches_wire=all(wire[k]==grid[k] for k in ("frame","cells","width","height","origin","resolution"))
            demand(matches_wire,"subscriber grid disagrees at leg "+str(s["index"]))
        check=grid_audit(grid,s["xy"],s["goal"],.30)
        check.update(index=s["index"],mode=s["mode"],goal=s["goal"],map_stamp_ns=s["map_stamp_ns"],
                     known=s["known"],subscriber_map_matches=matches_wire,core_state=s.get("core_state"))
        demand(check["nonfree_cells"]==0 and check["length_m"]<=.651,"insufficient map clearance at "+s["mission_id"])
        demand(hashlib.sha256(data).hexdigest()==s["grid_sha256"],"grid hash mismatch")
        if s.get("core_state")=="SUCCEEDED":
            event=next(e for e in raw_events if e["phase"]=="waypoint" and e["mission_id"]==s["mission_id"])
            # Use the receipt observer's clock. This may follow the actual arrival
            # by one or more publish cycles; report the pairing delta explicitly.
            observed=closest(event,"stamp_ns")
            error=math.dist(observed["xy"],s["goal"])
            check.update(receipt_world_xy=observed["xy"],goal_error_m=error,
                         arrival_pair_delta_ms=abs(event["stamp_ns"]-observed["stamp_ns"])/1e6)
            demand(error<.20 and check["arrival_pair_delta_ms"]<250,"arrival not independently verified")
        steps.append(check)
    demand(bool(steps) and all(s["subscriber_map_matches"] for s in steps),
           "decision grid missing from the independent map recording")
    demand(report["state"]=="SUCCEEDED" and report["survey_arrivals"]>=2 and report["saved_known_gain"]>=100,
           "survey success and map growth not established")
    for name,digest in report.get("map_sha256",{}).items():
        demand(hashlib.sha256((path.parent/"map"/name).read_bytes()).hexdigest()==digest,"final map hash mismatch: "+name)
    inspection=None
    if "inspection_site" in report:
        station=report["inspection_site"]
        target=station["target"]
        selected=[r for r in raw_events if r["phase"]=="inspection_selected"]
        inspected=[r for r in raw_events if r["phase"]=="inspected"]
        frames=[r for r in raw_events if r["phase"]=="mapped"]
        receipts=[s for s in report["steps"] if s["mode"]=="survey"
                  and s["index"]==station["source_step"] and s.get("core_state")=="SUCCEEDED"]
        source_measured=receipts[0].get("measured_xy") if len(receipts)==1 else None
        demand(source_measured==target and math.dist(target,report["home_xy"])>=.25,
               "inspection goal lacks a distinct reached survey receipt")
        stages=[s for s in report["steps"] if s["mode"]=="inspect" and s.get("core_state")=="SUCCEEDED"]
        demand(bool(stages) and len(inspected)==len(selected)==len(frames)==1,
               "missing inspection and map evidence")
        if inspected and selected and frames:
            arrival=inspected[0]["stamp_ns"]
            demand(frames[0]["stamp_ns"]<=selected[0]["stamp_ns"]<arrival,
                   "inspection did not follow map and station selection")
            matches=[e for e in raw_events if e["phase"]=="waypoint" and e.get("mode")=="inspect"
                     and selected[0]["stamp_ns"]<e["stamp_ns"]<=arrival]
            demand(bool(matches),"station check has no separately confirmed approach")
            grid=json.loads((path.parent/"map"/"occupancy.json").read_bytes())
            parking=grid_audit(grid,target,target,.30)
            pose=closest(inspected[0],"stamp_ns")
            delta=abs(pose["stamp_ns"]-arrival)/1e6
            distance_to_station=math.dist(pose["xy"],target)
            near=[r for r in truth if arrival-250_000_000<=r["stamp_ns"]<=arrival+100_000_000]
            drift_at_site=max((math.dist(r["xy"],pose["xy"]) for r in near),default=None)
            demand(parking["nonfree_cells"]==0 and station["clearance_m"]>=.35,
                   "saved map does not support parking at inspection site")
            demand(distance_to_station<=.15 and delta<=250 and len(near)>=2
                   and drift_at_site is not None and drift_at_site<=.03,
                   "world witness does not confirm a distinct station stop")
            demand(bool(report.get("inspection_file")) and (path.parent/report["inspection_file"]).is_file(),
                   "missing station inspection artifact")
            observed_counts=None
            inspected_map=maps.get(inspected[0]["map_stamp_ns"])
            demand(inspected_map is not None,"station map missing from the independent recording")
            if inspected_map:
                station_map=json.loads((media/inspected_map["file"]).read_bytes())
                ox,oy,yaw=station_map["origin"]
                c,s=math.cos(yaw),math.sin(yaw)
                resolution=station_map["resolution"]
                observed_counts=dict(free=0,occupied=0,uncertain=0,unknown=0)
                for idx,value in enumerate(station_map["cells"]):
                    row,col=divmod(idx,station_map["width"])
                    gx,gy=(col+.5)*resolution,(row+.5)*resolution
                    cell=(ox+c*gx-s*gy,oy+s*gx+c*gy)
                    if math.dist(cell,inspected[0]["xy"])<=inspected[0]["radius_m"]:
                        label="unknown" if value<0 else "free" if value==0 else "occupied" if value>=65 else "uncertain"
                        observed_counts[label]+=1
                demand(observed_counts==inspected[0]["counts"],"independent station observation differs")
            if report.get("inspection_file") and (path.parent/report["inspection_file"]).is_file():
                artifact=json.loads((path.parent/report["inspection_file"]).read_bytes())
                demand(artifact["counts"]==inspected[0]["counts"] and artifact["map_stamp_ns"]==inspected[0]["map_stamp_ns"]
                       and artifact["initial_grid_sha256"]==report["map_sha256"]["occupancy.json"],
                       "unmatched station artifact or initial grid")
            inspection=dict(source_step=station["source_step"],target_xy=target,
                            measured_source_xy=source_measured,arrival_world_xy=pose["xy"],
                            arrival_pair_delta_ms=delta,station_error_m=distance_to_station,
                            parking_cell_check=parking,window_pose_samples=len(near),drift_m=drift_at_site,
                            independent_observation_counts=observed_counts)
    holds=[]
    for stopped in (e for e in raw_events if e["phase"]=="stopped"):
        restored=next((e for e in raw_events if e["phase"]=="observations_restored" and e["stamp_ns"]>stopped["stamp_ns"]),None)
        if not restored:
            issues.append("stopped with no recovery markers")
            continue
        # Receipt and recovered samples delimit the interval in which the controller must hold position.
        lo,hi=stopped["stamp_ns"],restored["stamp_ns"]
        poses=[r for r in truth if lo<=r["stamp_ns"]<=hi]
        cmd=[r for r in twists if lo<=r["stamp_ns"]<=hi]
        dispatched=[r["mission_id"] for r in raw_events if r["phase"]=="goal" and lo<r["stamp_ns"]<hi]
        max_cmd=max([abs(q[k]) for q in cmd for k in ("linear","angular")],default=0)
        drift=max((math.dist(r["xy"],poses[0]["xy"]) for r in poses),default=None)
        accepted=next((e for e in raw_events if e["phase"]=="accepted" and e["mission_id"]==stopped["mission_id"]),None)
        motion=None
        if accepted:
            pre=[p for p in truth if accepted["stamp_ns"]<=p["stamp_ns"]<=lo]
            pre_cmd=[c for c in twists if accepted["stamp_ns"]<=c["stamp_ns"]<=lo]
            if pre and pre_cmd:
                motion=dict(samples=len(pre),translation_m=max(math.dist(p["xy"],pre[0]["xy"]) for p in pre),
                            yaw_change_rad=sum(math.remainder(b["yaw"]-a["yaw"],2*math.pi) for a,b in zip(pre,pre[1:])),
                            peak_linear_command_mps=max(abs(c["linear"]) for c in pre_cmd),
                            peak_angular_command_rad_s=max(abs(c["angular"]) for c in pre_cmd))
        holds.append(dict(mission_id=stopped["mission_id"],samples=len(poses),drift_m=drift,twist_samples=len(cmd),
                          max_observed_command=max_cmd,goals_while_stopped=dispatched,
                          interval_sim_s=(hi-lo)/1e9,motion_before_stop=motion))
        demand(len(poses)>=4 and drift is not None and drift<=.025 and cmd and max_cmd<.02 and not dispatched,
               "no confirmed stationary hold")
    demand(bool(holds),"active-motion interruption not observed")
    end=truth[-1]
    tail=[r for r in truth if r["stamp_ns"]>=end["stamp_ns"]-650_000_000]
    drift=max(math.dist(r["xy"],end["xy"]) for r in tail)
    home_error=math.dist(end["xy"],truth[0]["xy"])
    distance=sum(math.dist(a["xy"],b["xy"]) for a,b in zip(truth,truth[1:]))
    demand(len(tail)>=4 and drift<.015 and max(abs(twists[-1][k]) for k in ("linear","angular"))<.01,
           "final vehicle stop not established")
    demand(home_error<=.20 and distance>.6,"world-frame excursion or return to start not established")
    result=dict(passed=not issues,issues=issues,recording_id=recording_id,operation_id=report["operation_id"],
                world_truth_frame=truth[0]["frame"],pose_samples=len(truth),world_start_xy=truth[0]["xy"],
                world_end_xy=end["xy"],world_path_m=distance,world_home_error_m=home_error,
                stopped_final_drift_m=drift,stopped_final_samples=len(tail),spatial_checks=steps,holds=holds,
                saved_known_gain=report.get("saved_known_gain"),map_sha256=report.get("map_sha256"),
                inspection=inspection)
    write_json(out/"campus-audit.json",result)
    if issues: raise ValueError("campus audit: "+"; ".join(issues))
    return result


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("out",type=Path)
    cli.add_argument("--image",default="edge-agent-ros:commission-0977a54")
    cli.add_argument("--audit-recording")
    args=cli.parse_args()
    print(audit(args.out,args.audit_recording) if args.audit_recording else prepare_world(args.out,args.image))
