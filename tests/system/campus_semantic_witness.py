"""Observe the isolated campus rover from Gazebo independently of the navigation session."""
import argparse
from collections import deque
import json
import math
import os
from pathlib import Path
import time
from xml.etree import ElementTree

from campus_truth_geometry import FOOTPRINT, clearance, rectangle


def write(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def doorway_geometry(sdf):
    model = ElementTree.parse(sdf).find("./world/model[@name='teaching']")
    link = model.find("link[@name='site']")
    if any(any(float(v)!=0 for v in e.findtext("pose","").split()) for e in (model,link)):
        raise ValueError("Campus truth requires the teaching collision link in world coordinates")
    walls = {}
    for side in ("north","south"):
        item = link.find(f"collision[@name='portal_{side}_sill']")
        x,y,_,roll,pitch,yaw = map(float,item.findtext("pose").split())
        sx,sy,_ = map(float,item.findtext("geometry/box/size").split())
        if (roll,pitch,yaw)!=(0,0,0):
            raise ValueError("Expected axis-aligned portal in the surveyed campus world")
        walls[side] = rectangle(x,y,0,sx/2,sx/2,sy/2,sy/2)
    return walls


def audit_doorway(samples, walls):
    xs = [p[0] for wall in walls.values() for p in wall]
    plane=(min(xs)+max(xs))/2
    low=max(y for _,y in walls["south"])
    high=min(y for _,y in walls["north"])
    gaps={key:math.inf for key in walls}
    crossings=[]
    for a,b in zip(samples,samples[1:]):
        dx,dy=b["x"]-a["x"],b["y"]-a["y"]
        turn=math.remainder(b["yaw"]-a["yaw"],2*math.pi)
        steps=max(1,math.ceil(math.hypot(dx,dy)/.01),math.ceil(abs(turn)/math.radians(1)))
        for i in range(steps+1):
            t=i/steps
            rover=rectangle(a["x"]+t*dx,a["y"]+t*dy,a["yaw"]+t*turn,*FOOTPRINT)
            for side,wall in walls.items():
                gaps[side]=min(gaps[side],clearance(rover,wall))
        if (a["x"] < plane <= b["x"]) or (b["x"] <= plane < a["x"]):
            crossings.append(a["y"]+(plane-a["x"])*dy/dx)
    return dict(door_x_m=plane,aperture_y_m=[low,high],crossing_y_m=crossings,min_body_to_jamb_m=gaps,
                passed=bool(crossings) and all(low<y<high for y in crossings) and min(gaps.values())>=.55)


def record(directory):
    from campus_dynamic_fixture import CASES
    from campus_truth_geometry import odometry
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from ros_gz_interfaces.msg import Contacts
    from sensor_msgs.msg import LaserScan
    from std_msgs.msg import String
    import rclpy
    from rclpy.qos import qos_profile_sensor_data

    if (os.getenv("ROS_DOMAIN_ID"), os.getenv("GZ_PARTITION")) != ("191", "sdd058_campus_phase1"):
        raise ValueError("Use the phase1 campus simulation")
    directory.mkdir(parents=True, exist_ok=True)
    stage = None
    samples, contacts, starts, commands = [], [], [], []
    terminal_stamp = None
    walls = doorway_geometry("/campus/world.sdf")
    people = CASES["phase1"]
    observed = {a.name:deque(maxlen=16) for a in people}
    recent_scans, recent_plans = deque(maxlen=30), deque(maxlen=35)
    contact_topics = ["/sdd058/fixture/contacts/" + link for link in
                      ("base_link", "left_wheel", "right_wheel", "rear_caster")]
    rclpy.init()
    node = rclpy.create_node("campus_semantic_truth")

    def on_odom(msg):
        if not stage or msg.header.frame_id != "world":
            return
        rover = odometry(msg)
        stamp = rover["stamp_ns"]
        if samples and stamp - samples[-1]["stamp_ns"] < 75_000_000:
            return
        vehicle = rectangle(*rover["xy"],rover["yaw"],*FOOTPRINT)
        gaps = {}
        for person in people:
            if not observed[person.name]:
                return
            actor = min(observed[person.name],key=lambda p:abs(p["stamp_ns"]-stamp))
            if actor["frame"]!="world" or abs(actor["stamp_ns"]-stamp)>100_000_000:
                return
            box = rectangle(*actor["xy"],actor["yaw"],person.width/2,person.width/2,person.depth/2,person.depth/2)
            gaps[person.name] = clearance(vehicle,box)
        samples.append(dict(stamp_ns=stamp,x=rover["xy"][0],y=rover["xy"][1],frame=rover["frame"],yaw=rover["yaw"],
                            v=rover["linear_mps"],w=rover["angular_radps"],person_gaps_m=gaps))

    def on_contact(msg):
        if stage:
            for hit in msg.contacts:
                pair = (hit.collision1.name, hit.collision2.name)
                other = [s for s in pair if "edge_rover" not in s]
                if len(other)==1 and "floor" not in other[0]:
                    contacts.append(pair)

    def on_event(msg):
        if stage:
            e = json.loads(msg.data)
            if e.get("module")!="edge.gateway":
                return
            if e.get("kind")=="action.started":
                starts.append(e["operation_id"])
            if not str(e.get("operation_id","")).startswith(stage["id"]+"/"):
                return
            if e.get("kind")=="navigation.local_plan":
                recent_plans.append(e)
            if e.get("kind")=="action.result" and e.get("code")!="navigation.arrived":
                write(directory/f"{stage['id']}.refusal-{len(starts)}.json",dict(
                    event=e,scans=list(recent_scans),plans=list(recent_plans),world=samples[-30:]))

    def on_scan(msg):
        if stage:
            recent_scans.append(dict(frame=msg.header.frame_id,stamp_ns=msg.header.stamp.sec*10**9+msg.header.stamp.nanosec,
                                     range_min=msg.range_min,range_max=msg.range_max,angle_min=msg.angle_min,
                                     angle_max=msg.angle_max,angle_increment=msg.angle_increment,
                                     ranges=[r if math.isfinite(r) else None for r in msg.ranges]))

    def on_cmd(msg):
        if stage:
            commands.append((msg.linear.x, msg.angular.z))

    for topic in contact_topics:
        node.create_subscription(Contacts, topic, on_contact, 50)
    for person in people:
        node.create_subscription(Odometry,person.truth,lambda msg,name=person.name:observed[name].append(odometry(msg)),50)
    node.create_subscription(Odometry, "/ground_truth", on_odom, 50)
    node.create_subscription(LaserScan,"/scan",on_scan,qos_profile_sensor_data)
    node.create_subscription(String, "/edge/events", on_event, 100)
    node.create_subscription(Twist, "/cmd_vel", on_cmd, qos_profile_sensor_data)
    expire = time.monotonic() + 900
    checked = 0.
    while time.monotonic() < expire:
        rclpy.spin_once(node, timeout_sec=.05)
        now = time.monotonic()
        if now - checked < .12:
            continue
        checked = now
        if (directory/"complete").is_file():
            break
        trigger = directory/"stage.json"
        if not trigger.is_file():
            continue
        job = json.loads(trigger.read_bytes())
        if job != stage:
            samples, contacts, starts, commands = [], [], [], []
            recent_scans.clear()
            recent_plans.clear()
            terminal_stamp = None
            stage = job
        task = job["id"]
        if len(samples)<8 or (directory/f"{task}.world.json").exists():
            continue
        armed = directory/f"{task}.armed.json"
        if not armed.exists():
            publishers = {t:node.count_publishers(t) for t in [*contact_topics, "/ground_truth", "/edge/events", "/cmd_vel"]}
            if all(publishers.values()):
                write(armed, dict(task=task, pose=samples[-1], publishers=publishers))
            continue
        if not (directory/f"{task}.terminal.json").is_file():
            continue
        if terminal_stamp is None:
            terminal_stamp=samples[-1]["stamp_ns"]
        if samples[-1]["stamp_ns"]-terminal_stamp < 1_000_000_000:
            continue
        a,b = samples[0],samples[-1]
        tail = [s for s in samples if b["stamp_ns"]-s["stamp_ns"]<=900_000_000]
        path_length = sum(math.hypot(s["x"]-t["x"],s["y"]-t["y"]) for s,t in zip(samples,samples[1:]))
        goal = job.get("goal")
        error = math.hypot(goal[0]-b["x"],goal[1]-b["y"]) if goal else None
        travel = max(math.hypot(s["x"]-a["x"],s["y"]-a["y"]) for s in samples)
        peak = max(s["v"] for s in samples)
        current_starts = [op for op in starts if op.startswith(task+"/")]
        min_gap = min(gap for s in samples for gap in s["person_gaps_m"].values())
        portal = audit_doorway(samples,walls) if goal else None
        checks = dict(world_samples=len(samples)>=12,
                      stopped=max(s["v"] for s in tail)<=.025 and max(abs(s["w"]) for s in tail)<=.05,
                      zero_contacts=not contacts,person_clearance=min_gap>=.55)
        if goal:
            checks.update(verified_motion=bool(current_starts) and path_length>2,
                          traversed_aperture=portal["passed"], reached=error<=.13)
        else:
            checks.update(no_action=not starts, stationary=travel<.02 and peak<.025,
                          no_nonzero_command=not any(abs(v)>.001 or abs(w)>.001 for v,w in commands))
        write(directory/f"{task}.trajectory.json", samples)
        write(directory/f"{task}.world.json", dict(source="/ground_truth Gazebo world odometry, contact sensors and /edge/events",
              passed=all(checks.values()),checks=checks,id=task,initial=a,final=b,target_world_xy=goal,
              goal_error_m=error,path_length_m=path_length,peak_speed_mps=peak,max_displacement_m=travel,
              velocity_commands=len(commands),contacts=contacts,actions=starts,samples=len(samples),min_person_clearance_m=min_gap,
              doorway=portal))
    else:
        raise TimeoutError("Campus verifier did not receive completion")
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",required=True,type=Path)
    record(parser.parse_args().out)
