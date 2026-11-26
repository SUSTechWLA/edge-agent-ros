"""Drive only Gazebo fixture actors and record independent navigation recovery evidence."""

from collections import deque
import json
import math
import os
import re
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from campus_truth_geometry import FOOTPRINT, clearance, odometry, rectangle, stamp_ns


STILL_LINEAR_MPS = .025
STILL_ANGULAR_RADPS = .05
MAX_TRUTH_SKEW_NS = 100_000_000


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n", encoding="utf-8")
    temporary.replace(path)


def inspect_mission(task_id):
    query = urlencode({"task_id":task_id})
    with urlopen(f"http://console:8080/api/exploration-mission?{query}", timeout=1) as response:
        state = json.load(response)
    if state.get("environment") != "simulation":
        raise RuntimeError("expected isolated Gazebo console")
    return state.get("mission")


def still(row):
    return row["linear_mps"] <= STILL_LINEAR_MPS and abs(row["angular_radps"]) <= STILL_ANGULAR_RADPS


def measured_actor_motion(states):
    speed = [math.dist(a["xy"],b["xy"])*1e9/(b["stamp_ns"]-a["stamp_ns"])
             for a,b in zip(states,states[1:]) if b["stamp_ns"]>a["stamp_ns"]]
    raw = max(states,key=lambda s:s["linear_mps"],default=None)
    return {"max_position_speed_mps":max(speed,default=0),
            "raw_twist_peak":({"mps":raw["linear_mps"],"stamp_ns":raw["stamp_ns"],
                               "is_first_sample":raw is states[0]} if raw else None)}


def output_stop(samples, stationary_from):
    quiet = [sample for sample in samples if sample["host_time_ns"] >= stationary_from]
    if len(quiet) < 2:
        return {}
    start, end = quiet[0], quiet[-1]
    return {
        "samples":len(quiet),
        "wall_seconds":(end["host_time_ns"]-start["host_time_ns"])/1e9,
        "gazebo_seconds":(end["rover"]["stamp_ns"]-start["rover"]["stamp_ns"])/1e9,
        "max_linear_mps":max(sample["rover"]["linear_mps"] for sample in quiet),
        "max_angular_radps":max(abs(sample["rover"]["angular_radps"]) for sample in quiet),
        "max_output_linear_mps":max(abs(sample["command"][0]) for sample in quiet),
        "max_output_angular_radps":max(abs(sample["command"][1]) for sample in quiet),
        "max_displacement_m":max(math.dist(sample["rover"]["xy"], start["rover"]["xy"]) for sample in quiet),
    }


def recovery_cycles(events, gateway, ref, operation, boot):
    """Match each hold to subsequent clearance for that operation before accepting another leg."""
    pending = {}
    issues, completed = [], []
    receipts = {entry["event_id"]: entry for entry in gateway if entry.get("event_id")}
    plans = []
    for entry in events:
        kind, data = entry["kind"], entry["data"]
        op = data.get("operation_id")
        if kind == "route_planned":
            plans.append(op)
            if pending:
                issues.append({"seq": entry["seq"], "reason": "dispatch_before_release", "held": list(pending)})
            continue
        if kind == "dynamic_hold":
            pending[op] = {"hold": entry["seq"], "clear": None}
            continue
        if kind not in ("dynamic_recheck", "dynamic_resumed", "dynamic_blocked"):
            continue
        cycle = pending.get(op)
        if cycle is None:
            issues.append({"seq": entry["seq"], "reason": "missing_matching_hold"})
            continue
        if kind == "dynamic_blocked":
            cycle["blocked"] = entry["seq"]
            continue
        required = data.get("required_samples", 0)
        proof = (data.get("result") == "clear" and data.get("map_ref") == ref
                 and data.get("task_operation_id") == operation and data.get("gateway_boot_id") == boot
                 and data.get("required_clearance_m") == .55 and data.get("clearance_m", 0) >= .55
                 and required > 0 and data.get("clear_samples", 0) >= required)
        if data.get("source"):
            native = receipts.get(data["source"], {})
            proof = (proof and native.get("operation_id") == op + "/waypoint/1"
                     and native.get("code") in {"navigation.dynamic_clear", "navigation.dynamic_recheck"})
        else:
            proof = proof and required >= 5 and data.get("clear_span_ms", 0) >= 650
        if kind == "dynamic_recheck":
            cycle["clear"] = entry["seq"] if proof else None
        elif proof and cycle["clear"] is not None:
            completed.append(dict(operation=op, **cycle, resume=entry["seq"]))
            del pending[op]
        else:
            issues.append({"seq": entry["seq"], "reason": "resume_without_matching_clear_observation"})
    starts = [entry["operation_id"] for entry in gateway if entry["kind"] == "action.started"]
    if starts != [op + "/waypoint/1" for op in plans]:
        issues.append({"reason": "native_dispatch_trace_mismatch"})
    return {"completed": completed, "pending": pending, "issues": issues}


def summarize(case, task_id, records, mission, *, cases, map_id, map_digest):
    samples = [row for row in records if row["type"] == "sample"]
    gate = [row for row in records if row["type"] == "gateway"]
    working = [row for row in gate if row["kind"] == "action.started"]
    terminal = [row for row in gate if row["kind"] == "action.result"]
    stops = [row for row in terminal if row.get("data",{}).get("stop_evidence",{}).get("quiet_samples",0) >= 3]
    contacts = [row for row in records if row["type"] == "contact"]
    guards = [row for row in records if row["type"] == "fixture.guard"]
    rewinds = [row for row in records if row["type"] == "fixture.clock_rewind"]
    starts = [row for row in records if row["type"] == "fixture.started"]
    depart = {row["actor"]:row for row in records if row["type"] == "fixture.arrived"}
    reboot = sorted({row["gateway_boot"] for row in gate if row.get("gateway_boot")})
    sources = cases[case]
    paths = {}
    for actor in sources:
        states = [row["actors"][actor.name] for row in samples]
        lateral_values = [state["xy"][1] for state in states]
        paths[actor.name] = {
            "travel_m":max(lateral_values)-min(lateral_values) if lateral_values else 0,
            "start_y_m":lateral_values[0] if lateral_values else None,
            "finish_y_m":lateral_values[-1] if lateral_values else None,
            **measured_actor_motion(states),
            "min_rover_clearance_m":min((row["gaps_m"][actor.name] for row in samples),default=None),
            "fixture_gate_completed":actor.name in depart,
        }
    crossing_windows = [(row["host_time_ns"], row["actor"]) for row in records if row["type"]=="fixture.entered"]
    # Actor occupancy is painted by independent Gazebo geometry. The policy
    # witnesses a whole threatened interval including the handoff between actors.
    lane = [s for s in samples if starts and s["host_time_ns"]>=starts[0]["host_time_ns"]
            and any(s["lane_threatened"].values())]
    # Starting actor motion can happen before the rover has reached the arrest point.
    if lane:
        held_samples = [s for s in lane if still(s["rover"])]
        last_threat = lane[-1]["host_time_ns"]
    else:
        held_samples = []
        last_threat = None
    overlap = [s for s in lane if all(s["lane_threatened"].values()) and all(
        actor["linear_mps"]>.05 for actor in s["actors"].values())]
    post_progress = bool(last_threat and any(s["host_time_ns"]>last_threat+200_000_000 and
                                              s["rover"]["linear_mps"]>.035 for s in samples))
    row = mission or {}
    task = row.get("id")
    operation = row.get("operation_id")
    ref = row.get("map_ref") or {}
    goal = row.get("goal")
    pose = samples[-1]["rover"] if samples else {}
    error = math.dist(goal,pose["xy"]) if goal and pose else None
    blocked_mark = depart["lane_barrier"]["host_time_ns"] + 1_000_000_000 if "lane_barrier" in depart else None
    block_window = output_stop(samples,blocked_mark) if blocked_mark is not None else {}
    session_events = {e["seq"]:e for e in (row.get("events") or [])}
    session_events.update({r["event"]["seq"]: r["event"] for r in records if r["type"] == "session.event"})
    decisions = [{"kind":event.get("kind"),"seq":event.get("seq"),"stamp":event.get("stamp"),"data":event.get("data",{})}
                 for _, event in sorted(session_events.items())]
    only_bound_task = task == task_id and operation == task_id+"/step/1" and bool(working) and all(
        native["operation_id"].startswith(task_id+"/") for native in working)
    known_map = ref.get("id")==map_id and ref.get("digest")==map_digest and ref.get("revision")==map_digest
    decisions_bound = [item for item in decisions if item["kind"] in
                       {"dynamic_hold","dynamic_recheck","dynamic_resumed","dynamic_blocked"}]
    resume = [item for item in decisions_bound if item["kind"]=="dynamic_resumed"]
    cycles = recovery_cycles(decisions, gate, ref, operation, reboot[0].removeprefix("gateway-") if len(reboot)==1 else None)
    checks = {
        "exact_frozen_published_map":known_map,
        "one_outer_task_and_operation":only_bound_task,
        "unchanged_single_gateway_boot":len(reboot)==1,
        "one_gazebo_clock_epoch":bool(samples) and not rewinds,
        "no_physical_contact_or_fixture_guard":not contacts and not guards and bool(samples) and all(
            result["min_rover_clearance_m"] > .04 for result in paths.values()),
        "existing_gateway_footprint":bool(working) and all(
            tuple(native.get("data",{}).get("vehicle_footprint",{}).get(field) for field in
                  ("front_m","rear_m","left_m","right_m")) == FOOTPRINT for native in working),
        "terminal_independent_rover_stationary":bool(pose) and still(pose),
    }
    if case != "doorway":
        checks["agent_recheck_bound_to_same_map_task_gateway_and_margin"] = bool(decisions_bound) and len(reboot)==1 and all(
            item["data"].get("map_ref")==ref and item["data"].get("task_operation_id")==operation and
            item["data"].get("required_clearance_m")==.55 and
            "gateway-"+str(item["data"].get("gateway_boot_id"))==reboot[0] for item in decisions_bound)
    if case == "doorway":
        moving = [i for i,s in enumerate(samples) if s["rover"]["linear_mps"] > .04]
        underway = samples[moving[0]:moving[-1]+1] if len(moving) >= 2 else []
        speeds = [s["rover"]["linear_mps"] for s in underway]
        checks.update(
            stationary_people_present_and_clear=bool(underway) and all(
                paths[a.name]["travel_m"] < .01 and abs(paths[a.name]["start_y_m"] - a.start_y) < .025
                and paths[a.name]["min_rover_clearance_m"] >= .55 for a in sources),
            no_spurious_yield_or_review=not decisions_bound and not any(
                r.get("code") == "navigation.dynamic_yield" for r in gate),
            one_continuous_action_from_hall_to_goal=len(working)==1 and len(stops)==1
                and not cycles["issues"] and bool(underway)
                and math.dist(underway[0]["rover"]["xy"],underway[-1]["rover"]["xy"]) > 3.,
            no_intermediate_stops=bool(speeds) and min(speeds) > STILL_LINEAR_MPS
                and goal is not None and all(s["command"][0] > .005 for s in underway
                                             if math.dist(s["rover"]["xy"], goal) > .17)
                and all((b["rover"]["stamp_ns"] - a["rover"]["stamp_ns"]) <= 350_000_000
                        for a,b in zip(underway,underway[1:])),
            independent_arrival_and_stop_witness=bool(error is not None and error<.17 and stops
                                                        and row.get("status")=="SUCCEEDED"))
    if case=="crossing":
        checks.update(
            two_different_independent_pedestrians_crossed=all(
                paths[actor.name]["travel_m"]>=2.5 and actor.name in depart for actor in sources)
                and {name for _,name in crossing_windows}=={actor.name for actor in sources},
            at_least_one_threatened_stop=bool(lane) and len(held_samples)>5,
            all_overlapping_moving_pedestrian_samples_stopped=len(overlap)>=8
                and all(still(s["rover"]) for s in overlap),
            continued_under_same_task_after_last_crossing=post_progress and row.get("status")=="SUCCEEDED",
            agent_rechecked_before_continuing=bool(working and cycles["completed"]
                and not cycles["issues"] and not cycles["pending"] and last_threat
                and any(item["operation_id"]==working[0]["operation_id"] for item in stops)
                and ((len(working)>=2 and working[1]["host_time_ns"]>last_threat) or
                     (len(working)==1 and any(item["code"]=="navigation.dynamic_clear"
                         and item["operation_id"]==working[0]["operation_id"]
                         and item["host_time_ns"]>last_threat for item in gate)))),
            independent_arrival_and_stop_witness=bool(error is not None and error<.17 and stops),
        )
    elif case=="blocked":
        checks.update(
            persistent_obstacle_entered_and_held="lane_barrier" in depart and paths["lane_barrier"]["travel_m"]>=1.5
                and bool(samples and samples[-1]["lane_threatened"].get("lane_barrier")),
            continuous_independent_parked_stop=block_window.get("wall_seconds",0)>=25
                and block_window["max_output_linear_mps"]<=.005 and block_window["max_output_angular_radps"]<=.005
                and block_window["max_linear_mps"]<=STILL_LINEAR_MPS
                and block_window["max_angular_radps"]<=STILL_ANGULAR_RADPS and block_window["max_displacement_m"]<.04,
            terminal_still_blocked_without_arrival=row.get("status")=="BLOCKED" and not resume
                and not any(entry.get("code")=="navigation.arrived" for entry in terminal
                            if blocked_mark and entry["host_time_ns"]>=blocked_mark),
            active_hold_resolved_as_blocked=bool(cycles["pending"]) and not cycles["issues"]
                and all("blocked" in hold for hold in cycles["pending"].values()),
        )
    return {
        "schema_version":1,"passed":all(checks.values()),"case":case,"checks":checks,"task_id":task_id,
        "operation_id":operation,"map_ref":ref,"status":row.get("status"),"phase":row.get("phase"),
        "source":"Gazebo world odometry, contact sensors and ROS motion commands",
        "gateway_boot_ids":reboot,"native_operations":[item["operation_id"] for item in working],
        "verified_gateway_stops":len(stops),"gateway_dynamic_yield_events":sum(
            item.get("code")=="navigation.dynamic_yield" for item in gate),
        "gateway_dynamic_clear_events":sum(item.get("code")=="navigation.dynamic_clear" for item in gate),
        "physical_contacts":len(contacts),"fixture_guards":len(guards),"actors":paths,
        "threatened_samples":len(lane),"threatened_and_stopped_samples":len(held_samples),
        "overlap_occupied_by_moving_pedestrians_samples":len(overlap) if case=="crossing" else None,
        "overlap_stationary_samples":sum(still(s["rover"]) for s in overlap) if case=="crossing" else None,
        "world_samples":len(samples),"final_pose":pose,"goal_error_m":error,"persistent_stop_window":block_window,
        "session_events":decisions,"recovery_cycles":cycles,
        **({"uninterrupted_travel": {"samples": len(speeds), "minimum_speed_mps": min(speeds,default=None),
             "maximum_speed_mps": max(speeds,default=None),
             "gazebo_seconds": ((underway[-1]["rover"]["stamp_ns"]-underway[0]["rover"]["stamp_ns"])/1e9
                                if underway else None),
             "interior_stop_samples":
             sum(v <= STILL_LINEAR_MPS for v in speeds)}} if case=="doorway" else {}),
    }


def record(case, task_id, output, timeout, *, cases, deployment,
           prefix, map_id, map_digest, lane_slope, save_scans):
    if (os.environ.get("ROS_DOMAIN_ID"), os.environ.get("GZ_PARTITION")) != deployment[case]:
        raise ValueError("Expected the isolated Gazebo actor fixture")
    if re.fullmatch(f"{prefix}{case}-[0-9]{{2}}",task_id) is None:
        raise ValueError("task identity must match the selected Gazebo case")

    from geometry_msgs.msg import Twist
    from gz.msgs10.double_pb2 import Double
    from gz.transport13 import Node as Gazebo
    from nav_msgs.msg import Odometry
    from ros_gz_interfaces.msg import Contacts
    from std_msgs.msg import String
    import rclpy
    from rclpy.qos import qos_profile_sensor_data

    output.mkdir(parents=True, exist_ok=False)
    rclpy.init()
    node = rclpy.create_node("sdd058_dynamic_witness")
    gz = Gazebo()
    actors = cases[case]
    def score():
        return summarize(case, task_id, records, current, cases=cases, map_id=map_id, map_digest=map_digest)
    actor_buffers = {actor.name:deque(maxlen=24) for actor in actors}
    drives = {actor.name:gz.advertise(actor.command,Double) for actor in actors}
    records = []
    dispatch_stamp = None
    sample_stamp = None
    latest_active = None
    rover_command = (0.,0.)
    current = None
    armed = False
    halted = False
    paired_after_start = 0
    begun = set()
    entered = set()
    parked = set()
    seen_session_events = set()

    with (output/"timeline.jsonl").open("x", encoding="utf-8",buffering=1) as log:
        # Keep the pre-dispatch static scene so tracking faults can be replayed
        # against unfiltered, timestamped sensor data independently of the UI.
        scan_log = None
        if save_scans:
            from sensor_msgs.msg import LaserScan
            scan_log = (output/"scan-odom.jsonl").open("x", encoding="utf-8", buffering=1)
            def record_scan(msg):
                values = [v if math.isfinite(v) else None for v in msg.ranges]
                frame = dict(frame=msg.header.frame_id,range_min=msg.range_min,range_max=msg.range_max,
                             angle_min=msg.angle_min,angle_max=msg.angle_max,angle_increment=msg.angle_increment,
                             ranges=values)
                scan_log.write(json.dumps(["scan",stamp_ns(msg.header.stamp),frame],separators=(",",":"))+"\n")
            def record_pose(msg):
                pose = odometry(msg)
                frame = dict(x=pose["xy"][0],y=pose["xy"][1],yaw=pose["yaw"],
                             v=msg.twist.twist.linear.x,w=msg.twist.twist.angular.z,frame=pose["frame"])
                scan_log.write(json.dumps(["odom",pose["stamp_ns"],frame],separators=(",",":"))+"\n")
            node.create_subscription(LaserScan,"/scan",record_scan,qos_profile_sensor_data)
            node.create_subscription(Odometry,"/odom",record_pose,qos_profile_sensor_data)
        def emit(event_type, **data):
            entry = dict(type=event_type,host_time_ns=time.time_ns(),**data)
            records.append(entry)
            log.write(json.dumps(entry,ensure_ascii=False,allow_nan=False)+"\n")
            return entry

        def move(actor, speed):
            drives[actor.name].publish(Double(data=speed))

        def stop_actors():
            for actor in actors:
                move(actor,0.)

        def on_gateway(message):
            nonlocal dispatch_stamp,latest_active
            try:
                event = json.loads(message.data)
                op = event["operation_id"]
                kind = event["kind"]
                code = event.get("code")
                identity = event["event_id"]
            except (ValueError,TypeError,KeyError):
                return
            if event.get("module") != "edge.gateway" or not op.startswith(task_id+"/"):
                return
            if kind not in ("action.started","action.result","navigation.dynamic_yield","navigation.dynamic_clear",
                            "navigation.local_plan") and (
                    "dynamic" not in str(code) and "clear" not in str(code) or kind.startswith("perception.")):
                return
            if kind=="action.started":
                latest_active = op
                if dispatch_stamp is None:
                    dispatch_stamp = event.get("data",{}).get("odom_stamp_ns") or sample_stamp
            elif kind=="action.result" and latest_active==op:
                latest_active = None
            emit("gateway",operation_id=op,kind=kind,code=code,detail=event.get("detail"),
                 data=event.get("data",{}),event_id=identity,gateway_boot=identity.rsplit("-",1)[0])

        def on_contact(message):
            for item in message.contacts:
                a,b=item.collision1.name,item.collision2.name
                if ("edge_rover" in a and b and "edge_rover" not in b and "floor" not in b or
                    "edge_rover" in b and a and "edge_rover" not in a and "floor" not in a):
                    emit("contact",pair=sorted((a,b)),stamp_ns=stamp_ns(message.header.stamp))

        def on_twist(message):
            nonlocal rover_command
            rover_command = message.linear.x,message.angular.z

        def on_rover(message):
            nonlocal sample_stamp,armed,halted,paired_after_start
            rover = odometry(message)
            now = rover["stamp_ns"]
            if sample_stamp is not None and now<sample_stamp-500_000_000:
                if not halted:
                    halted=True
                    stop_actors()
                    emit("fixture.clock_rewind",stamp_ns=now,previous_stamp_ns=sample_stamp)
                return
            if not all(actor_buffers.values()) or sample_stamp is not None and now-sample_stamp<75_000_000:
                return
            simultaneous = {name:min(buffer,key=lambda s:abs(s["stamp_ns"]-now))
                            for name,buffer in actor_buffers.items()}
            skew = max(abs(s["stamp_ns"]-now) for s in simultaneous.values())
            if rover["frame"]!="world" or any(s["frame"]!="world" for s in simultaneous.values()) or skew>MAX_TRUTH_SKEW_NS:
                return
            sample_stamp = now
            body = rectangle(*rover["xy"],rover["yaw"],*FOOTPRINT)
            gaps = {source.name:clearance(body,rectangle(*simultaneous[source.name]["xy"],
                    simultaneous[source.name]["yaw"],*(source.width/2,)*2,*(source.depth/2,)*2))
                    for source in actors}
            # The frozen route's endpoint lies north-east of the initial pose.
            # Use a conservative local band centered on the straight approach;
            # measured contact and swept gap are scored separately.
            lane = {a.name:abs(simultaneous[a.name]["xy"][1]-(lane_slope*a.x)) <=
                    .83+a.depth/2 for a in actors}
            emit("sample",rover=rover,actors=simultaneous,gaps_m=gaps,lane_threatened=lane,
                 pose_skew_ns=skew,command=rover_command,operation_id=latest_active)
            if not armed:
                if not still(rover) or abs(rover["xy"][0])>.05 or abs(rover["xy"][1])>.05:
                    raise ValueError("expected stationary rover at the known map origin")
                paired_after_start+=1
                if paired_after_start<12:
                    return
                stop_actors()
                armed=True
                write(output/"armed.json",{"case":case,"task_ids":[task_id],"map_id":map_id,"map_digest":map_digest,
                      "rover":rover,"actors":simultaneous,"paired_world_samples":paired_after_start,
                      "actor_topics":{a.name:a.command for a in actors}})
            if halted or dispatch_stamp is None:
                return
            for a in actors:
                if a.speed == 0.:
                    continue
                state = simultaneous[a.name]
                if a.name not in begun and now-dispatch_stamp>=a.delay*1e9:
                    move(a,a.speed)
                    begun.add(a.name)
                    emit("fixture.started",actor=a.name,stamp_ns=now,operation_id=latest_active)
                if a.name in begun and lane[a.name] and a.name not in entered:
                    entered.add(a.name)
                    emit("fixture.entered",actor=a.name,pose=state)
                if a.name in begun and a.name not in parked and a.direction*(state["xy"][1]-a.end_y)>=-.02:
                    move(a,0.)
                    parked.add(a.name)
                    emit("fixture.arrived",actor=a.name,pose=state)
            minimum = min(gaps.values())
            if minimum<.045:
                halted=True
                stop_actors()
                emit("fixture.guard",minimum_clearance_m=minimum,stamp_ns=now)

        for actor in actors:
            node.create_subscription(Odometry,actor.truth,lambda m,name=actor.name:actor_buffers[name].append(odometry(m)),50)
            node.create_subscription(Contacts,actor.contacts,on_contact,50)
        for link in ("base_link","left_wheel","right_wheel","rear_caster"):
            node.create_subscription(Contacts,"/sdd058/fixture/contacts/"+link,on_contact,50)
        node.create_subscription(Odometry,"/ground_truth",on_rover,50)
        node.create_subscription(Twist,"/cmd_vel",on_twist,qos_profile_sensor_data)
        node.create_subscription(String,"/edge/events",on_gateway,100)
        started = time.monotonic()
        checked = saved = 0.
        terminal_since = None
        try:
            while time.monotonic()-started<timeout:
                rclpy.spin_once(node,timeout_sec=.05)
                wall = time.monotonic()
                if wall-checked>1.25:
                    try:
                        observed = inspect_mission(task_id)
                    except OSError:
                        observed = None
                    if observed:
                        current = observed
                        for event in observed.get("events", []):
                            if event["seq"] not in seen_session_events:
                                seen_session_events.add(event["seq"])
                                emit("session.event", event=event)
                    checked=wall
                if wall-saved>1:
                    write(output/"summary.json",score())
                    saved=wall
                if (output/"done").exists():
                    break
                if current and current.get("status") in {"BLOCKED","RECOVERY_REQUIRED","SUCCEEDED"}:
                    terminal_since = terminal_since or wall
                    # Always hold a blocked fixture long enough to independently
                    # measure it, even if the task terminates early.
                    held = next((r["host_time_ns"] for r in records if r["type"]=="fixture.arrived"),None)
                    if wall-terminal_since>=2 and (case!="blocked" or held is not None and
                                                   time.time_ns()-held>=28_000_000_000):
                        break
        finally:
            stop_actors()
            if scan_log:
                scan_log.close()
            result = score()
            write(output/"summary.json",result)
            node.destroy_node()
            rclpy.try_shutdown()
    return int(not result["passed"])
