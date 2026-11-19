"""Gazebo adapter for the optional frontier planner. Only Core owns navigation."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from agent.module_journal import ModuleJournal
from calibration.factory import status as calibration_status
from exploration.frontier import plan_frontier
from exploration.map_artifact import save_map


def checkpoint(path, value):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def factory_state(args):
    state = calibration_status(args.calibration, args.robot_id, "simulation")
    if (state.get("status") != "VERIFIED" or not state.get("revision") or state.get("robot_id") != args.robot_id
            or state.get("environment") != "simulation"):
        raise ValueError("simulation calibration identity or revision unavailable")
    return state


def run(args):
    # This adapter is deliberately available only in a disposable, non-host ROS/Gazebo project.
    if (os.getenv("EDGE_COMMISSION_MODE") != "isolated_gazebo"
            or not re.fullmatch(r"commission_[a-f0-9]{8,32}", os.getenv("GZ_PARTITION", ""))
            or args.robot_id != os.getenv("EDGE_ROBOT_ID") or not os.getenv("ROS_DOMAIN_ID")):
        raise ValueError("the exploration motion adapter requires an isolated Gazebo deployment")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}", args.run_id):
        raise ValueError("invalid run ID")
    if (not 1 <= args.max_goals <= 100 or not 1 <= args.timeout <= 3600
            or not all(math.isfinite(x) and abs(x) <= 4 for x in args.bounds)):
        raise ValueError("exploration budget or ROI exceeds simulated gateway envelope")
    factory = factory_state(args)
    # All workers for one robot share its operation output volume in this deployment.
    # flock releases the lease on exit or crash; Core separately owns the physical action.
    import fcntl
    args.output_dir.mkdir(exist_ok=True, parents=True)
    with (args.output_dir / (args.robot_id+".exploration.lock")).open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("exploration already active for this robot") from error
        return run_admitted(args, factory)


def run_admitted(args, factory):
    revision = factory["revision"]
    from client.mission import MissionClient, TERMINAL
    from edge_interfaces.srv import SubmitMission, GetMission, CancelMission
    from geometry_msgs.msg import Point
    from nav_msgs.msg import OccupancyGrid, Odometry
    from rosgraph_msgs.msg import Clock
    from rclpy.qos import QoSProfile, DurabilityPolicy, qos_profile_sensor_data
    from rclpy.signals import SignalHandlerOptions
    import rclpy

    folder = args.output_dir / args.run_id
    folder.mkdir(parents=True, exist_ok=False)
    report = dict(run_id=args.run_id, robot_id=args.robot_id, environment="simulation", calibration_revision=revision,
                  bounds=args.bounds, max_goals=args.max_goals, state="STARTING", reason="waiting_for_map", steps=[])
    journal = ModuleJournal(folder / "module-events.sqlite")
    parent_event = factory["event_id"]
    def event(kind, detail, data, mission=None):
        nonlocal parent_event
        with journal.db:
            parent_event = journal.emit(dict(module="exploration.frontier", kind="exploration."+kind, code=kind,
                trace_id=args.run_id, operation_id=args.run_id, mission_id=mission,
                parent_event_id=parent_event, data=data, detail=detail))
        return parent_event
    def save():
        checkpoint(folder / "report.json", report)
    report["start_event_id"] = event("started","Simulation exploration admitted",{"calibration_revision": revision})
    save()
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = MissionClient()
    end = time.monotonic() + args.timeout
    interrupted = False
    inputs = {}
    previous = {}
    active = None
    latest = None
    visited = []
    invalid = None
    def stop(*_):
        nonlocal interrupted
        interrupted = True
    handlers = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}

    def stamp(t):
        return t.sec * 10**9 + t.nanosec
    def receive(kind, msg):
        nonlocal invalid
        ts = stamp(msg.clock if kind == "clock" else msg.header.stamp)
        if ts < previous.get(kind, 0):
            invalid = "clock_or_observation_rewind"
        if ts <= previous.get(kind, 0):
            return
        previous[kind] = ts
        inputs[kind] = (time.monotonic(), ts, msg)
    node.create_subscription(Clock, "/clock", lambda m: receive("clock", m), qos_profile_sensor_data)
    node.create_subscription(Odometry, "/edge/perception/pose", lambda m: receive("pose", m), qos_profile_sensor_data)
    node.create_subscription(OccupancyGrid, "/edge/perception/map", lambda m: receive("map", m),
                             QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))

    def check():
        if invalid or interrupted or time.monotonic() > end:
            raise RuntimeError(invalid or ("operator_cancel" if interrupted else "time_budget"))

    def current():
        if inputs.keys() < {"clock", "pose", "map"}:
            return None
        now = time.monotonic()
        clock = inputs["clock"][1]
        for key, (arrival, ts, msg) in inputs.items():
            if now-arrival > (3.5 if key == "map" else 1.5) or not -.25e9 <= clock-ts <= 2.5e9:
                return None
        pose, grid = inputs["pose"][2], inputs["map"][2]
        if not pose.header.frame_id == grid.header.frame_id == "odom" or pose.child_frame_id != "base_link":
            return None
        p = pose.pose.pose.position
        if not all(map(math.isfinite, (p.x,p.y))):
            return None
        return [p.x,p.y], grid

    def snapshot(after=0):
        limit = min(end, time.monotonic()+20)
        while time.monotonic() < limit:
            check()
            rclpy.spin_once(node, timeout_sec=.09)
            observation = current()
            if observation is None or inputs["map"][1] <= after:
                continue
            pose, msg = observation
            o = msg.info.origin
            if abs(o.orientation.x)+abs(o.orientation.y)>1e-5:
                raise ValueError("non-planar grid")
            return pose, dict(frame=msg.header.frame_id, width=msg.info.width, height=msg.info.height,
                              resolution=msg.info.resolution,
                              origin=[o.position.x, o.position.y, 2*math.atan2(o.orientation.z, o.orientation.w)],
                              cells=list(msg.data)), inputs["map"][1]
        raise RuntimeError("map_or_pose_unavailable")

    def query(operation, deadline):
        return node.call(node.query, GetMission.Request(mission_id=operation), deadline)

    def drive(goal, step):
        nonlocal active
        limit = min(end, time.monotonic()+55)
        active = step["mission_id"]
        response = node.call(node.submit, SubmitMission.Request(mission_id=active,
                        waypoints=[Point(x=float(goal[0]), y=float(goal[1]))]), limit)
        if not response.accepted:
            active = None
            raise RuntimeError("navigation_rejected: "+response.detail)
        step["accepted"] = True
        save()
        check_calibration_at = time.monotonic() + 1.
        while time.monotonic() < limit:
            check()
            rclpy.spin_once(node, timeout_sec=.05)
            status = query(active, limit)
            if status.found and status.state in TERMINAL:
                step.update(core_state=status.state, detail=status.detail, verified_count=status.verified_count,
                            measured_xy=[[p.x,p.y] for p in status.evidence])
                active = None
                if (status.state!="SUCCEEDED" or status.verified_count!=1 or len(status.evidence)!=1
                        or not all(math.isfinite(v) for v in step["measured_xy"][0])
                        or math.dist(goal,step["measured_xy"][0])>.10):
                    raise RuntimeError("navigation_not_verified: "+status.detail)
                return
            if current() is None:
                raise RuntimeError("observation_lost_during_motion")
            if time.monotonic() >= check_calibration_at:
                if factory_state(args)["revision"] != revision:
                    raise RuntimeError("calibration_revision_changed")
                check_calibration_at = time.monotonic() + 1.
            time.sleep(.055)
        raise RuntimeError("navigation_timeout")

    try:
        after = 0
        checked_sim = False
        while True:
            pose, latest, ts = snapshot(after)
            if not checked_sim:
                gz = subprocess.run(["gz","service","-l"],capture_output=True,text=True,timeout=8,check=True)
                if "/world/edge_lab/control" not in gz.stdout.splitlines():
                    raise RuntimeError("isolated_gazebo_world_unavailable")
                checked_sim = True
                after = ts
                continue
            plan = plan_frontier(latest, pose, bounds=args.bounds, footprint_radius=.30,
                                 min_step=.12, max_step=.65, visited=visited)
            report.setdefault("initial_known_cells", plan["known_cells"])
            report.update(final_known_cells=plan["known_cells"], total_cells=plan["total_cells"], map_stamp_ns=ts)
            if visited:
                last = report["steps"][-1]
                moved = math.dist(pose,last["pose_before"])
                advance = math.dist(last["goal"], last["pose_before"])
                if moved < min(.06, max(.025, advance/4)) and plan["known_cells"] <= last["known_cells"]:
                    report.update(state="PARTIAL",reason="no_observed_progress")
                    break
            if plan["state"]!="WAYPOINT" or len(visited)>=args.max_goals:
                exhausted = plan["state"]=="WAYPOINT"
                report.update(state="PARTIAL" if exhausted else plan["state"], reason="goal_budget" if exhausted else plan["reason"])
                break
            if factory_state(args)["revision"]!=revision:
                raise RuntimeError("calibration_revision_changed")
            step = dict(goal=plan["goal"], path=plan["path"], pose_before=pose, map_stamp_ns=ts,
                        known_cells=plan["known_cells"],mission_id=f"{args.run_id}/step/{len(visited)+1}")
            report["steps"].append(step)
            report.update(state="RUNNING",reason="core_dispatched")
            step["dispatch_event_id"] = event("dispatch","Request one reachable frontier leg",
                                               {"goal":plan["goal"],"known_cells":plan["known_cells"]},step["mission_id"])
            save()
            drive(plan["goal"],step)
            step["receipt_event_id"] = event("receipt",step["detail"], {"core_state":step["core_state"],
                                             "measured_xy":step["measured_xy"]},step["mission_id"])
            visited.append(plan["goal"])
            after = inputs["pose"][1]
            save()
    except Exception as exc:
        report.update(state="BLOCKED",reason=str(exc))
        if active is not None:
            # Even if submit timed out it might have taken effect. Reconcile only the original Core mission.
            report["state"]="RECOVERY_REQUIRED"
            deadline=time.monotonic()+12
            try:
                node.call(node.cancel,CancelMission.Request(mission_id=active),deadline)
                while time.monotonic()<deadline:
                    receipt=query(active,deadline)
                    if receipt.found and receipt.state in TERMINAL:
                        report["cancel"]={"mission_id":active,"state":receipt.state,"detail":receipt.detail}
                        if receipt.state=="CANCELED":
                            report["state"]="CANCELED"
                        break
                    time.sleep(.12)
            except Exception as error:
                report["cancel"]={"mission_id":active,"error":str(error)}
        elif report["steps"] and report["steps"][-1].get("core_state")=="RECOVERY_REQUIRED":
            report["state"]="RECOVERY_REQUIRED"
    finally:
        try:
            if latest is not None:
                try:
                    report["map"] = save_map(folder/"map", latest, metadata=dict(robot_id=args.robot_id,
                                 calibration_id=revision, operation_id=args.run_id, stamp_ns=ts,
                                 state=report["state"], reason=report["reason"]))
                except (OSError, ValueError) as error:
                    report.update(map_error=str(error))
                    if report["state"] in ("PARTIAL","COMPLETE"):
                        report.update(state="BLOCKED",reason="map_publication_failed")
            report["end_event_id"] = event("finished",report["reason"], {"state":report["state"],
                "map_stamp_ns":report.get("map_stamp_ns"),"cancel":report.get("cancel")})
            save()
        finally:
            journal.close()
            for sig,old in handlers.items():
                signal.signal(sig,old)
            node.destroy_node()
            rclpy.try_shutdown()
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration",type=Path,required=True,help="factory state directory")
    parser.add_argument("--robot-id",required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--run-id",required=True)
    parser.add_argument("--max-goals",type=int,default=4)
    parser.add_argument("--timeout",type=int,default=210)
    parser.add_argument("--bounds",nargs=4,type=float,default=[-3.,-3.,3.,3.])
    args=parser.parse_args()
    result=run(args)
    print(json.dumps({k:result[k] for k in ("run_id","state","reason")},ensure_ascii=False))
    sys.exit(0 if result["state"] in ("PARTIAL","COMPLETE") else 2)
