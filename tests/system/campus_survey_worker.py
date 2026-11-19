"""Cold-start campus experiment. Consume online odometry-frame beliefs and command Core short legs."""
import argparse
from collections import deque
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time

from calibration.factory import status as calibration_status
from exploration.frontier import Grid, _masks, plan_frontier
from exploration.map_artifact import save_map
from campus_revisit import select_inspection_site

RADIUS = .30
MAX_STEP = .65
HOME_TOLERANCE = .13


def home_leg(raw, xy, home, bounds):
    """Use today's inflated free space to route to the observed start in short, straight segments."""
    g = Grid(raw)
    _, _, safe, _ = _masks(g, bounds, RADIUS)
    start, finish = g.index(xy), g.index(home)
    if min(start,finish)<0 or not (safe[start] and safe[finish]):
        raise ValueError("home_or_robot_without_clearance")
    if math.dist(home,xy) <= HOME_TOLERANCE:
        return None
    parents = {start:None}
    queue = deque([start])
    while queue and finish not in parents:
        i=queue.popleft()
        for j in g.neighbors(i):
            if safe[j] and j not in parents:
                parents[j]=i
                queue.append(j)
    if finish not in parents:
        raise ValueError("no_current_free_route_home")
    route=[finish]
    while route[-1]!=start:
        route.append(parents[route[-1]])
    route.reverse()
    # Short-cut only if the entire swept centre segment stays on the inflated free mask.
    # Dense sampling plus neighbouring cells for diagonal corner crossings avoids squeezing corners.
    for idx in reversed(route):
        candidate = list(home) if idx==finish else g.center(idx)
        distance=math.dist(xy,candidate)
        # A single 10 cm cell can be the necessary turn at the corner of an
        # inflated obstacle. The Gateway's arrival radius is 5.5 cm.
        if not .09 <= distance <= MAX_STEP:
            continue
        n=max(1,math.ceil(distance/(g.resolution/4)))
        previous=start
        for k in range(1,n+1):
            x,y=[a+(b-a)*k/n for a,b in zip(xy,candidate)]
            i=g.index((x,y))
            if i<0 or not safe[i]:
                break
            if previous%g.width!=i%g.width and previous//g.width!=i//g.width:
                if not safe[previous//g.width*g.width+i%g.width] or not safe[i//g.width*g.width+previous%g.width]:
                    break
            previous=i
        else:
            return candidate
    raise ValueError("no_executable_home_leg")


def measured(receipt, goal):
    evidence=[[p.x,p.y] for p in receipt.evidence]
    if (receipt.state!="SUCCEEDED" or receipt.verified_count!=1 or len(evidence)!=1 or
            not all(map(math.isfinite,evidence[0])) or math.dist(evidence[0],goal)>.10):
        raise RuntimeError("unverified_navigation: "+receipt.state+" "+receipt.detail)
    return evidence[0]


def fresh(samples, now):
    if not all(k in samples for k in ("clock","pose","map")):
        return False
    clock=samples["clock"][1]
    return all(now-arrival <= (2.9 if k=="map" else 1.0)
               and -.25e9 <= clock-ts <= (2.5e9 if k=="map" else 1.0e9)
               for k,(arrival,ts,_) in samples.items())


def save_report(path,value):
    tmp=path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    os.replace(tmp,path)


class Inputs:
    def __init__(self,node):
        from nav_msgs.msg import Odometry,OccupancyGrid
        from rosgraph_msgs.msg import Clock
        from rclpy.qos import qos_profile_sensor_data,QoSProfile,DurabilityPolicy
        self.samples={}
        self.invalid=""
        def receive(k,msg):
            t=msg.clock if k=="clock" else msg.header.stamp
            ts=t.sec*10**9+t.nanosec
            last=self.samples.get(k,(0,0,None))[1]
            if ts<last:
                self.invalid="clock_or_sensor_stamp_rewound"
            if ts<=last: return
            if k in ("pose","map") and msg.header.frame_id!="odom":
                self.invalid="frame_changed"
            if k=="pose" and msg.child_frame_id!="base_link":
                self.invalid="unexpected_base_frame"
            self.samples[k]=(time.monotonic(),ts,msg)
        node.create_subscription(Clock,"/clock",lambda m:receive("clock",m),qos_profile_sensor_data)
        node.create_subscription(Odometry,"/edge/perception/pose",lambda m:receive("pose",m),qos_profile_sensor_data)
        node.create_subscription(OccupancyGrid,"/edge/perception/map",lambda m:receive("map",m),
                                 QoSProfile(depth=1,durability=DurabilityPolicy.TRANSIENT_LOCAL))

    def valid(self):
        if self.invalid: raise RuntimeError(self.invalid)
        return fresh(self.samples,time.monotonic())

    def snapshot(self):
        msg=self.samples["map"][2]
        p=self.samples["pose"][2].pose.pose.position
        xy=[p.x,p.y]
        q=msg.info.origin.orientation
        if not all(map(math.isfinite,xy)) or abs(q.x)+abs(q.y)>1e-5 or abs(math.hypot(q.z,q.w)-1)>.001:
            raise ValueError("invalid_fused_pose_or_nonplanar_grid")
        raw=dict(frame=msg.header.frame_id,width=msg.info.width,height=msg.info.height,
                 resolution=msg.info.resolution,origin=[msg.info.origin.position.x,msg.info.origin.position.y,
                 2*math.atan2(q.z,q.w)],origin_z=msg.info.origin.position.z,cells=list(msg.data))
        return xy,raw,self.samples["map"][1]


def run(args):
    from client.mission import MissionClient,TERMINAL
    from edge_interfaces.srv import CancelMission,GetMission,SubmitMission
    from geometry_msgs.msg import Point
    from rclpy.signals import SignalHandlerOptions
    import rclpy
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,72}",args.operation)
            or not os.getenv("ROS_DOMAIN_ID") or os.getenv("EDGE_COMMISSION_MODE")!="isolated_gazebo"
            or not args.robot_id or args.robot_id!=os.getenv("EDGE_ROBOT_ID")):
        raise ValueError("isolated experiment, robot and single operation ID required")
    folder=args.output/args.operation
    folder.mkdir(parents=True,exist_ok=False)
    trail=folder/"events.jsonl"
    report=dict(operation_id=args.operation,trace_id=args.trace_id or args.operation,robot_id=args.robot_id,
                state="RUNNING",reason="cold_start",steps=[],survey_arrivals=0,home_arrivals=0,
                recovered_interruptions=0,gain_required=100,required_survey_arrivals=2)
    rclpy.init(args=[],signal_handler_options=SignalHandlerOptions.NO)
    node=MissionClient()
    inputs=Inputs(node)
    end=time.monotonic()+args.timeout
    interrupted=False
    def stop(*_):
        nonlocal interrupted
        interrupted=True
    handlers={s:signal.signal(s,stop) for s in (signal.SIGINT,signal.SIGTERM)}
    active=None
    sequence=0
    def emit(phase,**fields):
        nonlocal sequence
        sequence+=1
        record=dict(phase=phase,module="exploration.frontier",event_id=f"{args.operation}/event/{sequence}",
                    parent_event_id=f"{args.operation}/event/{sequence-1}" if sequence>1 else None,
                    operation_id=args.operation,trace_id=report["trace_id"],
                    stamp_ns=inputs.samples.get("clock",(0,0,None))[1],wall_ns=time.time_ns(),**fields)
        with trail.open("a",encoding="utf8") as stream:
            stream.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+"\n")
            stream.flush()
            os.fsync(stream.fileno())
        save_report(folder/"report.json",report)
    def check():
        if interrupted: raise RuntimeError("operator_canceled")
        if time.monotonic()>end: raise TimeoutError("time_budget")
    def spin():
        rclpy.spin_once(node,timeout_sec=.05)
    def acquire(after=0,minimum=1,monitor_gap=False):
        until=min(end,time.monotonic()+args.stream_timeout)
        stamps=[]
        lost=False
        while time.monotonic()<until:
            check()
            spin()
            if not inputs.valid():
                if monitor_gap and not lost:
                    emit("observation_lost",missing="fresh_pose_or_map",motion_in_flight=False)
                    lost=True
                stamps.clear()
                continue
            t=inputs.samples["map"][1]
            if t>after and (not stamps or t>stamps[-1]):
                stamps.append(t)
            if len(stamps)>=(max(3,minimum) if lost else minimum):
                if lost:
                    report["recovered_interruptions"]+=1
                    emit("observations_restored",map_stamps=stamps,motion_in_flight=False)
                return inputs.snapshot(),stamps
        raise TimeoutError("fresh_coherent_observations_unavailable: check map/pose publishers and clock")
    def query(mid):
        return node.call(node.query,GetMission.Request(mission_id=mid),time.monotonic()+4)
    def cancel():
        nonlocal active
        mid=active
        emit("cancel_requested",mission_id=mid)
        node.call(node.cancel,CancelMission.Request(mission_id=mid),time.monotonic()+5)
        until=time.monotonic()+12
        while time.monotonic()<until:
            s=query(mid)
            if s.found and s.state in TERMINAL:
                step=report["steps"][-1]
                step.update(core_state=s.state,detail=s.detail)
                if s.state=="CANCELED" and ("stop confirmed" in s.detail or "no moving action" in s.detail):
                    active=None
                    emit("stopped",mission_id=mid,detail=s.detail)
                    return False
                if s.state=="SUCCEEDED":
                    step["measured_xy"]=measured(s,step["goal"])
                    active=None
                    emit("receipt_before_cancel",mission_id=mid,xy=step["measured_xy"])
                    return True
                raise RuntimeError("unconfirmed_stop: "+s.state+" "+s.detail)
            spin()
        raise TimeoutError("stop_result_unknown")
    def drive(step):
        nonlocal active
        goal=step["goal"]
        active=step["mission_id"]
        req=SubmitMission.Request(mission_id=active,waypoints=[Point(x=float(goal[0]),y=float(goal[1]))])
        r=node.call(node.submit,req,time.monotonic()+5)
        if not r.accepted:
            active=None
            raise RuntimeError("core_rejected: "+r.detail)
        step["accepted"]=True
        emit("accepted",mission_id=active,index=step["index"],goal=goal,mode=step["mode"])
        until=min(end,time.monotonic()+55)
        while time.monotonic()<until:
            check()
            spin()
            s=query(active)
            if s.found and s.state in TERMINAL:
                step.update(core_state=s.state,detail=s.detail)
                step["measured_xy"]=measured(s,goal)
                active=None
                return True,False
            if not inputs.valid():
                emit("observation_lost",mission_id=active,missing="fresh_pose_or_map")
                return cancel(),True
            time.sleep(.035)
        raise TimeoutError("motion_timeout")
    try:
        emit("started")
        factory=calibration_status(args.calibration,args.robot_id,"simulation")
        if factory["status"]!="VERIFIED" or not factory.get("revision"):
            raise ValueError("simulation_factory_revision_required")
        revision=report["calibration_revision"]=factory["revision"]
        emit("waiting_for_observations",reason="no_motion_before_fresh_pose_and_map")
        (home,raw,ts),stamps=acquire(minimum=3)
        xy=home[:]
        bounds=[max(-4.,home[0]-3),max(-4.,home[1]-3),min(4.,home[0]+3),min(4.,home[1]+3)]
        history=[]
        report.update(home_xy=home,bounds=bounds)
        emit("scan_ready",xy=home,map_stamps=stamps)
        while len(report["steps"])<20:
            check()
            if calibration_status(args.calibration,args.robot_id,"simulation").get("revision")!=revision:
                raise ValueError("calibration_revision_changed")
            plan=plan_frontier(raw,xy,bounds=bounds,footprint_radius=RADIUS,min_step=.14,max_step=MAX_STEP,visited=history)
            known=plan["known_cells"]
            initial=report.setdefault("initial_known_cells",known)
            report.update(final_known_cells=known,known_gain=known-initial,total_cells=plan["total_cells"])
            if "map_file" not in report and report["survey_arrivals"]>=2 and known-initial>=100:
                manifest=save_map(folder/"map",raw,metadata=dict(robot_id=args.robot_id,calibration_id=revision,
                                  operation_id=args.operation,stamp_ns=ts,state="MAPPED",reason="survey_criteria_met"))
                report.update(map_file="map/map.yaml",saved_known_gain=known-initial,map_sha256=manifest["sha256"])
                emit("mapped",known=known,gain=known-initial,file=report["map_file"])
                saved=json.loads((folder/"map"/"occupancy.json").read_bytes())
                site=select_inspection_site(saved,xy,home,report["steps"],bounds)
                if site is None:
                    raise ValueError("no_verified_inspection_site_in_initial_map")
                report["inspection_site"]=site
                emit("inspection_selected",**site,initial_map_stamp_ns=ts)
            mode="survey"
            if "map_file" in report:
                mode="return" if "inspection_file" in report else "inspect"
            if mode in ("inspect","return"):
                destination=home if mode=="return" else report["inspection_site"]["target"]
                goal=home_leg(raw,xy,destination,bounds)
                if goal is None:
                    if mode=="inspect":
                        # Take a distinct observation after both the arrival receipt and settling.
                        (xy,raw,ts),_=acquire(after=ts,minimum=2,monitor_gap=True)
                        twist=inputs.samples["pose"][2].twist.twist
                        speed=math.hypot(twist.linear.x,twist.linear.y)
                        if (math.dist(xy,destination)>HOME_TOLERANCE or not math.isfinite(speed)
                                or speed>.03 or not math.isfinite(twist.angular.z) or abs(twist.angular.z)>.1):
                            raise ValueError("inspection_stop_unconfirmed")
                        grid=Grid(raw)
                        counts=dict(free=0,occupied=0,unknown=0,uncertain=0)
                        for i,value in enumerate(grid.cells):
                            if math.dist(grid.center(i),xy)<=1.0:
                                key="unknown" if value<0 else "free" if value==0 else "occupied" if value>=65 else "uncertain"
                                counts[key]+=1
                        observation=dict(xy=xy,**report["inspection_site"],map_stamp_ns=ts,radius_m=1.0,
                                         counts=counts,speed_mps=speed,yaw_rate_rad_s=twist.angular.z,
                                         initial_grid_sha256=report["map_sha256"]["occupancy.json"])
                        save_report(folder/"inspection.json",observation)
                        report.update(inspection_xy=xy,inspection_stamp_ns=ts,inspection_file="inspection.json")
                        emit("inspected",**observation)
                        continue
                    report.update(state="SUCCEEDED",reason="map_saved_inspection_complete_and_home_verified",final_xy=xy,
                                  home_error_m=math.dist(xy,home))
                    emit("home",xy=xy,goal=home,home_error_m=report["home_error_m"])
                    break
            else:
                if plan["state"]!="WAYPOINT":
                    raise ValueError("survey_incomplete: "+plan["reason"])
                goal=plan["goal"]
            idx=len(report["steps"])+1
            capture=gzip.compress(json.dumps(raw,separators=(",",":")).encode(),mtime=0)
            name=f"grid-{idx:02}.json.gz"
            (folder/name).write_bytes(capture)
            step=dict(index=idx,mode=mode,mission_id=f"{args.operation}/leg/{idx}",goal=goal,xy=xy,
                      map_stamp_ns=ts,pose_stamp_ns=inputs.samples["pose"][1],known=known,
                      grid_file=name,grid_sha256=hashlib.sha256(capture).hexdigest())
            if mode=="survey":
                step.update(frontier=plan["frontier"],potential_unknown_cells=plan["potential_unknown_cells"])
            report["steps"].append(step)
            emit("goal",**step)
            arrived,lost=drive(step)
            if arrived:
                measured_xy=step["measured_xy"]
                if math.dist(measured_xy,xy)<min(.065,math.dist(xy,goal)*.25):
                    raise ValueError("goal_reported_without_measurable_progress")
                if mode=="survey":
                    history.append(goal)
                    report["survey_arrivals"]+=1
                elif mode=="return":
                    report["home_arrivals"]+=1
                emit("waypoint",index=idx,mode=mode,xy=measured_xy,goal=goal,mission_id=step["mission_id"])
            if lost:
                last=inputs.samples.get("map",(0,ts,None))[1]
                (xy,raw,ts),stamps=acquire(after=last,minimum=3)
                report["recovered_interruptions"]+=1
                emit("observations_restored",xy=xy,map_stamps=stamps,mode=mode)
            else:
                after=max(ts,inputs.samples["pose"][1])
                (xy,raw,ts),_=acquire(after=after,monitor_gap=True)
        else:
            raise ValueError("leg_budget_exhausted")
    except Exception as exc:
        report.update(state="BLOCKED",reason=str(exc))
        if active:
            try:
                cancel()
            except Exception as stop_error:
                report.update(state="RECOVERY_REQUIRED",stop_error=str(stop_error),active_mission=active)
    finally:
        emit("finished",state=report["state"],reason=report["reason"])
        for s,old in handlers.items(): signal.signal(s,old)
        node.destroy_node()
        rclpy.try_shutdown()
    return report


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--output",type=Path,required=True)
    cli.add_argument("--operation",required=True)
    cli.add_argument("--trace-id")
    cli.add_argument("--robot-id",default=os.getenv("EDGE_ROBOT_ID"))
    cli.add_argument("--calibration",type=Path,default=Path(os.getenv("EDGE_CALIBRATION_DIR","/factory")))
    cli.add_argument("--timeout",type=float,default=240)
    cli.add_argument("--stream-timeout",type=float,default=35)
    result=run(cli.parse_args())
    print(json.dumps({k:result[k] for k in ("state","reason","operation_id")}))
    sys.exit(0 if result["state"]=="SUCCEEDED" else 2)
