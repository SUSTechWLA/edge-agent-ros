"""Passive ROS and Gazebo acceptance evidence for one local fixed-rover Agent task."""
import argparse
from collections import Counter, deque
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("profile",type=Path)
    parser.add_argument("output",type=Path)
    parser.add_argument("--owner",type=Path,required=True)
    parser.add_argument("--sensors",type=Path,required=True)
    parser.add_argument("--ui-receipt",type=Path,help="Bind the task ID from the live workbench submission.")
    parser.add_argument("--tracking",action="store_true",help="Also audit active movement against the live RGB target stream.")
    args=parser.parse_args()
    if (args.task=="-") != bool(args.ui_receipt):
        parser.error("use task '-' with a new UI receipt, or pass the existing task ID")
    if args.ui_receipt and args.ui_receipt.exists():
        parser.error("UI receipt predates the witness")
    if (os.getenv("ROS_DOMAIN_ID"),os.getenv("GZ_PARTITION")) != ("197","edge_fixed_rover_197"):
        raise RuntimeError("local fixed-rover witness scope mismatch")
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import Imu
    from std_msgs.msg import String
    from gz.transport13 import Node as Gazebo
    from gz.msgs10.pose_v_pb2 import Pose_V
    profile=json.loads(args.profile.read_bytes())
    sensors=json.loads(args.sensors.read_bytes())
    reverse_mps = sensors.get("view_tracking",{}).get("reverse_limit_mps",0) if args.tracking else 0
    imu=sensors["imu"]
    x,y,z,w=sensors["extrinsics"]["imu"]["rotation_xyzw"]
    body_z=(2*(x*z-w*y),2*(y*z+w*x),1-2*(x*x+y*y))
    ledger=sqlite3.connect(args.owner.as_uri()+"?mode=ro",uri=True,timeout=2)
    ledger.row_factory=sqlite3.Row
    args.output.mkdir(parents=True,exist_ok=False)
    client=build_opener(ProxyHandler({}))
    rclpy.init()
    node=rclpy.create_node("stock_profile_acceptance_witness")
    gz=Gazebo()
    lock=threading.Lock()
    counts=Counter()
    peaks={key:[0.,0.] for key in ("command","selected","safe","wheel")}
    bad=[]
    ends={}
    recent={k:deque(maxlen=3000) for k in ("world","wheel","safe")}
    operator_receipts=[]
    confirmed_stop=False
    visibility = {}
    path_length=0.
    state=None
    last_check=0.
    final_at=None
    deadline=time.monotonic()+3600
    stream=gzip.open(args.output/"trajectory.jsonl.gz","xt",encoding="utf-8",compresslevel=3)
    def emit(key,values,stamp=None):
        nonlocal path_length
        with lock:
            received=time.monotonic()
            counts[key]+=1
            if key in recent:
                recent[key].append((received,stamp,values))
            if key in peaks:
                v,w=values[-2:]
                peaks[key]=[max(peaks[key][0],abs(v)),max(peaks[key][1],abs(w))]
                if key!="wheel" and (v < -reverse_mps-1e-9 or v>profile["command_limits"]["forward_mps"]+1e-9
                                      or abs(w)>profile["command_limits"]["yaw_radps"]+1e-9) and key not in bad:
                    bad.append(key)
            if key=="world":
                prior=ends.get("latest",values)
                path_length+=math.dist(prior[:2],values[:2])
                ends.setdefault("initial",values)
                ends["latest"]=values
            stream.write(json.dumps([time.monotonic_ns(),key,stamp,values],separators=(",",":"))+"\n")
    def planar(p):
        q=p.orientation
        return [p.position.x,p.position.y,math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))]
    def wheel(m):
        frame=profile["odometry"]
        if m.header.frame_id!=frame["parent_frame"] or m.child_frame_id!=frame["child_frame"]:
            return
        stamp=m.header.stamp
        emit("wheel",planar(m.pose.pose)+[m.twist.twist.linear.x,m.twist.twist.angular.z],
             stamp.sec*10**9+stamp.nanosec)
    def world(m):
        item=next((p for p in m.pose if p.name=="lingao"),None)
        if item is not None:
            emit("world",planar(item),m.header.stamp.sec*10**9+m.header.stamp.nsec)
    def gyro(m):
        if m.header.frame_id==imu["frame"] and m.angular_velocity_covariance[0]>=0:
            a=m.angular_velocity
            xyz=(a.x,a.y,a.z)
            emit("imu_radps",[*xyz,sum(c*r for c,r in zip(body_z,xyz))],
                 m.header.stamp.sec*10**9+m.header.stamp.nanosec)
    def stopped(since,now):
        # Confirm continuous post-terminal quiescence in wheel and independent world motion.
        quiet=profile["stationary_seconds"]
        with lock:
            for key,history in recent.items():
                rows=[r for r in history if r[0]>=max(since,now-quiet-1.)]
                if len(rows)<3 or now-rows[-1][0]>.5 or rows[-1][0]-rows[0][0]<quiet:
                    return False
                if any(b[0]-a[0]>.5 for a,b in zip(rows,rows[1:])):
                    return False
                if key=="safe":
                    if any(max(abs(v) for v in r[2])>.001 for r in rows):
                        return False
                elif rows[-1][1]-rows[0][1] < quiet*1e9:
                    return False
                elif key=="wheel":
                    if any(abs(r[2][-2])>=.025 or abs(r[2][-1])>=.05 for r in rows):
                        return False
                else:
                    for a,b in zip(rows,rows[1:]):
                        dt=(b[1]-a[1])*1e-9
                        turn=math.remainder(b[2][2]-a[2][2],2*math.pi)
                        if dt<=0 or math.dist(a[2][:2],b[2][:2])/dt>=.025 or abs(turn)/dt>=.05:
                            return False
        return True
    def watch(key):
        def read(m):
            emit(key,[m.linear.x,m.angular.z])
            if args.tracking and key == "safe" and max(abs(m.linear.x),abs(m.angular.z))>.001:
                counts["tracking_motion_samples"] += 1
                box = visibility.get("box")
                if box is not None and time.monotonic()-visibility["received"] < .6:
                    counts["tracking_visible_motion_samples"] += 1
                    margin = min(box[0],box[1],1-box[2],1-box[3])
                    visibility["minimum_motion_margin"] = min(visibility.get("minimum_motion_margin",1.), margin)
        return read
    for key in ("command","selected","safe"):
        node.create_subscription(Twist,profile[key]["topic"],watch(key),qos_profile_sensor_data)
    node.create_subscription(Odometry,profile["odometry"]["topic"],wheel,qos_profile_sensor_data)
    node.create_subscription(Imu,imu["topic"],gyro,qos_profile_sensor_data)
    node.create_subscription(String,"/fixed/motion_guard",lambda m:emit("guard",m.data),qos_profile_sensor_data)
    if args.tracking:
        track=sensors["view_tracking"]
        def tracked(message):
            row=json.loads(message.data)
            if row["frame"]==sensors["camera"]["frame"] and row["target"]==track["target"]:
                visibility.update(box=row["box"],received=time.monotonic())
                emit("rgb_target",row,row["stamp_ns"])
        node.create_subscription(String,track["topic"],tracked,qos_profile_sensor_data)
    topic="/world/fixed_calibration/dynamic_pose/info"
    gz.subscribe(Pose_V,topic,world)
    task_id=None if args.ui_receipt else args.task
    try:
        while time.monotonic()<deadline and not confirmed_stop:
            rclpy.spin_once(node,timeout_sec=.015)
            now=time.monotonic()
            if now-last_check < 1:
                continue
            last_check=now
            try:
                if not task_id:
                    receipt=json.loads(args.ui_receipt.read_bytes())
                    identifier=receipt.get("id")
                    if not isinstance(identifier,str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}",identifier):
                        continue
                    task_id=identifier
                    emit("ui_receipt",{"task_id":task_id,"text":receipt.get("text")})
                url="http://agent:8090/agent/tasks?"+urlencode({"id":task_id,"limit":1})
                with client.open(url,timeout=1) as response:
                    task=json.load(response)
            except (OSError, ValueError):
                continue
            if "state" not in task or task.get("id")!=task_id:
                continue
            key=(task["state"],task["cursor"])
            if key!=state:
                emit("task",key)
                state=key
            if task["state"] in ("SUCCEEDED","FAILED","BLOCKED","RECOVERY_REQUIRED","CANCELED"):
                # Agent may first enter recovery while the original executor is still working.
                rows=ledger.execute("SELECT op,state,proof FROM physical_ops WHERE op LIKE ? ORDER BY rowid",
                                    (task_id+"/step/%",)).fetchall()
                if any(r["state"] not in ("FAILED","CANCELED","SUCCEEDED") for r in rows):
                    final_at=None
                    continue
                operator_receipts=[dict(operation=r["op"],state=r["state"],proof=json.loads(r["proof"]) if r["proof"] else None)
                                   for r in rows]
                final_at=final_at or now
                confirmed_stop=stopped(final_at,now)
            else:
                final_at=None
    finally:
        gz.unsubscribe(topic)
        with lock:
            stream.close()
            summary=dict(task_id=task_id,terminal=state,clock="host_monotonic_ns; sensor stamps in Gazebo time",
                         profile_sha256=hashlib.sha256(args.profile.read_bytes()).hexdigest(),counts=dict(counts),
                         limits=profile["command_limits"],peaks=peaks,command_limit_violations=bad,
                         independent_world_frame="fixed_calibration/world",pose_format=["x_m","y_m","yaw_rad"],
                         initial_and_stop={"initial":ends.get("initial"),"stop":ends.get("latest") if confirmed_stop else None},
                         stationary_verified=confirmed_stop,path_length_m=path_length,physical_operations=operator_receipts,
                         imu_columns=["sensor_x","sensor_y","sensor_z","body_z"],imu_unit="rad/s")
            if args.tracking:
                summary["tracking"] = {k:counts[k] for k in ("rgb_target","tracking_motion_samples","tracking_visible_motion_samples")}
                summary["tracking"]["minimum_motion_margin_fraction"] = visibility.get("minimum_motion_margin")
                summary["limits"]["reverse_mps"] = reverse_mps
            (args.output/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
            print(json.dumps(summary),flush=True)
        node.destroy_node()
        ledger.close()
        rclpy.shutdown()


if __name__=="__main__":
    main()
