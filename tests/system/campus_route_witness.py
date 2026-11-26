"""Read-only Gazebo ground-truth and contact witness for a submitted campus mission."""
import argparse
from collections import deque
import json
import math
from pathlib import Path
import time
from threading import Thread
from urllib.error import URLError

from campus_dynamic_witness import inspect_mission, still
from campus_truth_geometry import odometry, stamp_ns


def main():
    import rclpy
    from nav_msgs.msg import Odometry
    from ros_gz_interfaces.msg import Contacts

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("directory",type=Path)
    parser.add_argument("--goal",type=float,nargs=2,required=True)
    args=parser.parse_args()
    args.directory.mkdir(exist_ok=False)
    rclpy.init()
    node=rclpy.create_node("campus_ground_truth_witness")
    before=deque(maxlen=15)
    samples=[]
    collisions=[]
    contacts_seen=set()
    state={"mission":None,"done":False}

    def poll():
        while not state["done"]:
            try:
                row=inspect_mission(args.task_id)
                if row and row.get("id")==args.task_id:
                    state["mission"]=row
            except (OSError,URLError,ValueError):
                pass
            time.sleep(.7)

    def ground(msg):
        pose=odometry(msg)
        active=state["mission"] is not None
        history=samples if active else before
        if active and not samples:
            samples.extend(before)
        if not history or pose["stamp_ns"]-history[-1]["stamp_ns"] >= 90_000_000:
            history.append(pose)

    def contacts(msg,topic):
        contacts_seen.add(topic)
        if not state["mission"]:
            return
        for contact in msg.contacts:
            names=(contact.collision1.name,contact.collision2.name)
            if (sum("edge_rover" in x for x in names)==1
                    and not any("floor" in x for x in names)):
                collisions.append({"stamp_ns":stamp_ns(msg.header.stamp),"pair":names})

    node.create_subscription(Odometry,"/ground_truth",ground,50)
    topics=["/sdd058/fixture/contacts/"+name for name in ("base_link","left_wheel","right_wheel","rear_caster")]
    for topic in topics:
        node.create_subscription(Contacts,topic,lambda msg,t=topic:contacts(msg,t),50)
    Thread(target=poll,daemon=True).start()
    terminal_at=None
    timeout=time.monotonic()+1200
    while time.monotonic()<timeout:
        rclpy.spin_once(node,timeout_sec=.05)
        row=state["mission"] or {}
        if len(before)>=5 and not (args.directory/"armed").exists():
            (args.directory/"armed").write_text(str(before[-1]["xy"]))
        if row.get("status") in ("SUCCEEDED","BLOCKED","FAILED","CANCELED","RECOVERY_REQUIRED"):
            terminal_at=terminal_at or time.monotonic()
            if time.monotonic()-terminal_at>=1.0 and len(samples)>2:
                break
    state["done"]=True
    first,last=samples[0],samples[-1]
    error=math.dist(last["xy"],args.goal)
    travel=sum(math.dist(a["xy"],b["xy"]) for a,b in zip(samples,samples[1:]))
    quiet=[p for p in samples if last["stamp_ns"]-p["stamp_ns"]<=500_000_000]
    checks=dict(agent_session_arrived=row.get("phase")=="ARRIVED" and row.get("status")=="SUCCEEDED",
                registered_goal=row.get("goal")==args.goal,world_error_within_12cm=error<=.12,
                world_stopped=len(quiet)>=4 and all(still(p) for p in quiet),
                contact_topics_present=all(node.count_publishers(t)>0 for t in topics),
                floor_contact_observed=bool(contacts_seen),no_nonfloor_contacts=not collisions)
    result=dict(task_id=args.task_id,passed=all(checks.values()),checks=checks,source="Gazebo /ground_truth and collision sensors",
                initial_pose=first,final_pose=last,goal=args.goal,goal_error_m=error,
                distance_m=travel,peak_speed_mps=max(p["linear_mps"] for p in samples),
                duration_sim_s=(last["stamp_ns"]-first["stamp_ns"])/1e9,
                contact_count=len(collisions),contact_topics_with_messages=sorted(contacts_seen),
                sample_count=len(samples),map_ref=row.get("map_ref"),contacts=collisions[:50])
    (args.directory/"trajectory.json").write_text(json.dumps(samples,separators=(",",":")))
    (args.directory/"world.json").write_text(json.dumps(result,indent=2))
    node.destroy_node()
    rclpy.shutdown()


if __name__=="__main__":
    main()
