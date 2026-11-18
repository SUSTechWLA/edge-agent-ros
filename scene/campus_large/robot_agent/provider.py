"""Bounded simulation fixtures controlled by independent instances of the shipped Edge Agent."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler

from geometry_msgs.msg import Twist
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.transport13 import Node as GazeboNode
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Imu, JointState
from std_msgs.msg import Float64, String
from catalog import ACTIONS, JOINTS, NAMES, ROBOTS


def failed(code, text):
    return {"state":"BLOCKED","detail":text,"error":{"code":code,"message":text}}


class Fleet(Node):
    def __init__(self):
        super().__init__("campus_robot_adapter")
        self.lock=threading.RLock()
        self.poses,self.joints,self.sensors={},{},{}
        self.commands={}
        self.active={}
        self.db=sqlite3.connect("/state/operations.sqlite",check_same_thread=False)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS receipts (op TEXT PRIMARY KEY, robot TEXT, action TEXT, result TEXT)")
            self.db.execute("UPDATE receipts SET result=? WHERE result IS NULL",
                (json.dumps(failed("robot.runtime_restarted","控制器已重启，需核对原操作。")),))
        self.scene=json.loads(Path("/campus/layout.json").read_bytes())["revision"]
        self.overview_port=int(os.getenv("EDGE_CAMPUS_OVERVIEW_PORT","0"))
        self.telemetry=self.create_publisher(String,"/edge/robot_states",2)
        self.gz=GazeboNode()
        self.gz_callback=self.on_poses
        if not self.gz.subscribe(Pose_V,"/world/edge_lab/dynamic_pose/info",self.gz_callback):
            raise RuntimeError("Gazebo robot pose topic unavailable")
        for robot in ACTIONS:
            self.create_subscription(Imu,f"/model/{robot}/imu",
                lambda msg,key=robot: self.on_imu(key,msg),qos_profile_sensor_data)
        for robot,joints in JOINTS.items():
            self.create_subscription(JointState,f"/model/{robot}/joint_state",
                lambda msg,key=robot: self.on_joints(key,msg),qos_profile_sensor_data)
            for joint in joints:
                self.commands[joint]=self.create_publisher(Float64,f"/model/{robot}/joint/{joint}/cmd_pos",2)
        self.flight=self.create_publisher(Twist,"/model/crazyflie/cmd_vel",2)
        self.create_timer(.08,self.tick)
        self.create_timer(.35,self.emit_states)

    def on_poses(self,message):
        t=message.header.stamp
        stamp=t.sec+t.nsec*1e-9
        with self.lock:
            for p in message.pose:
                if p.name not in ROBOTS:
                    continue
                pos,q=p.position,p.orientation
                self.poses[p.name]=(time.monotonic(),stamp,[pos.x,pos.y,pos.z],[q.x,q.y,q.z,q.w])

    def on_imu(self,key,msg):
        a=msg.linear_acceleration
        if all(math.isfinite(v) for v in (a.x,a.y,a.z)):
            with self.lock:
                self.sensors[key]=(time.monotonic(),msg.header.stamp.sec+msg.header.stamp.nanosec*1e-9)

    def on_joints(self,key,msg):
        values={n:p for n,p in zip(msg.name,msg.position) if math.isfinite(p)}
        if set(JOINTS[key])<=values.keys():
            with self.lock:
                self.joints[key]=(time.monotonic(),msg.header.stamp.sec+msg.header.stamp.nanosec*1e-9,values)

    def emit_states(self):
        rows=[]
        now=time.monotonic()
        with self.lock:
            for key,(label,capability) in ROBOTS.items():
                pose=self.poses.get(key)
                item=dict(id=key,model_name=key,label=label,capabilities=[capability],frame="odom",status="waiting")
                if pose:
                    item.update(xyz=pose[2],quaternion=pose[3],stamp_s=pose[1],
                                status="online" if now-pose[0]<2.5 else "stale")
                if key in self.joints and now-self.joints[key][0]<2.5:
                    item["joints"]=self.joints[key][2]
                if key in self.sensors:
                    item["imu_stamp_s"]=self.sensors[key][1]
                if self.overview_port:
                    item["camera_url"]=f"http://127.0.0.1:{self.overview_port}/"+("teaching_interior" if key=="edge_rover" else "robots")+".png"
                rows.append(item)
        self.telemetry.publish(String(data=json.dumps(dict(scene_revision=self.scene,robots=rows),allow_nan=False)))

    def readings(self,key):
        pose=self.poses.get(key)
        sensor=self.sensors.get(key)
        joint=self.joints.get(key)
        now=time.monotonic()
        # A fixed-base fixture may no longer appear in Gazebo's *dynamic* pose
        # stream once it settles. Its joint encoder and IMU are independent sensors.
        control = joint if key in JOINTS else pose
        if not control or not sensor or now-min(control[0],sensor[0])>3:
            return None
        if key=="crazyflie":
            return pose[1],[pose[2][2]],sensor[1]
        return joint[1],[joint[2][n] for n in JOINTS[key]],sensor[1]

    def execute(self,key,action,op):
        with self.lock:
            row=self.db.execute("SELECT robot,action,result FROM receipts WHERE op=?",(op,)).fetchone()
            if row:
                if row[:2]!=(key,action):
                    return failed("robot.operation_conflict","原操作已绑定其他机器人或动作")
                return json.loads(row[2]) if row[2] else {"state":"RUNNING"}
            feedback=self.readings(key)
            if not feedback:
                return failed("robot.feedback_missing","没有取得新鲜的机体位姿、板载 IMU 或关节反馈。")
            if key in self.active:
                return failed("robot.busy","当前机器人正在执行其他操作。")
            with self.db:
                self.db.execute("INSERT INTO receipts VALUES (?,?,?,NULL)",(op,key,action))
            self.active[key]=dict(op=op,action=action,start=time.monotonic(),target=ACTIONS[key][action],
                                  before=feedback[1],first_stamp=feedback[0],first_imu=feedback[2],
                                  last_stamp=feedback[0],steady=0)
            return {"state":"RUNNING"}

    def tick(self):
        with self.lock:
            for key,job in list(self.active.items()):
                reading=self.readings(key)
                result=None
                if reading is None:
                    if key=="crazyflie":
                        self.flight.publish(Twist())
                    missing=job.setdefault("missing_since",time.monotonic())
                    if time.monotonic()-missing>7:
                        result=failed("robot.feedback_missing",f"{key} 没有连续取得新鲜的执行反馈，动作中断。")
                elif time.monotonic()-job["start"]>50:
                    result=failed("robot.feedback_timeout",f"{key} 目标 {job['target']:.3f}，最后测量 {reading[1]}，稳定样本数 {job['steady']}。")
                else:
                    job.pop("missing_since",None)
                    stamp,values,imu=reading
                    target=job["target"]
                    if key=="crazyflie":
                        velocity=Twist()
                        velocity.linear.z=max(-.35,min(.35,2.*(target-values[0])))
                        self.flight.publish(velocity)
                    else:
                        for name in JOINTS[key]:
                            self.commands[name].publish(Float64(data=target))
                    if stamp>job["last_stamp"]:
                        settled=max(abs(v-target) for v in values)<(.045 if key=="crazyflie" else .08)
                        job["steady"]=job["steady"]+1 if settled and imu>job["first_imu"] else 0
                        job["last_stamp"]=stamp
                    if job["steady"]>=4:
                        result={"state":"SUCCEEDED","evidence":dict(robot_id=key,action=job["action"],target=target,
                            measured=values,before=job["before"],units="m" if key=="crazyflie" else "rad",
                            topic="/world/edge_lab/dynamic_pose/info" if key=="crazyflie" else f"/model/{key}/joint_state",
                            stamp_s=stamp,before_stamp_s=job["first_stamp"],imu_stamp_s=imu,settled_samples=job["steady"],
                            scene_revision=self.scene)}
                if result:
                    if key=="crazyflie":
                        self.flight.publish(Twist())
                    with self.db:
                        self.db.execute("UPDATE receipts SET result=? WHERE op=?",(json.dumps(result),job["op"]))
                    del self.active[key]


def delegate(robot,action,op,trace):
    key="dispatch-"+hashlib.sha256(op.encode()).hexdigest()[:32]
    url=f"http://{robot}-agent:8090/agent/tasks"
    http=build_opener(ProxyHandler({}))
    trigger=dict(kind="assignment",source="campus",id=key)
    for field,source in (("parent_task_id","task_id"),("trace_id","trace_id")):
        if isinstance(trace,dict) and re.fullmatch(r"[\w.:-]{1,128}",str(trace.get(source,""))):
            trigger[field]=trace[source]
    payload=dict(id=key,text=NAMES[robot][0]+action,trigger=trigger)
    # Child ID and trigger identity are stable across retries and process restarts.
    request=Request(url,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"})
    with http.open(request,timeout=2) as response:
        response.read()
    with http.open(url+"?id="+key,timeout=2) as response:
        task=json.load(response)
    if task["state"]=="SUCCEEDED":
        proof=task["steps"][0]["evidence"]
        return {"state":"SUCCEEDED","evidence":{**proof,"child_task_id":key,"child_trace_id":task["trace_id"]}}
    if task["state"] in {"FAILED","BLOCKED","CANCELED","RECOVERY_REQUIRED"}:
        return failed("robot.child_"+task["state"].lower(),str(task.get("detail") or task["state"]))
    return {"state":"RUNNING"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):
        pass
    def do_POST(self):
        route=re.fullmatch(r"/(delegate|execute)/(crazyflie|go1|g1)",self.path)
        if not route:
            return self.send_error(404)
        try:
            size=int(self.headers.get("Content-Length",0))
            if not 0<size<=16384:
                return self.send_error(400)
            req=json.loads(self.rfile.read(size))
            op=req["operation_id"]
            if not isinstance(op,str) or not re.fullmatch(r"[\w./:@-]{1,240}",op) or req["version"]!="1":
                return self.send_error(400)
            mode,key=route.groups()
            action=req["arguments"]["action"]
            if action not in ACTIONS[key]:
                result=failed("robot.unsupported_action","该机体支持："+ "、".join(ACTIONS[key]))
            elif mode=="delegate":
                result=delegate(key,action,op,req.get("trace"))
            else:
                result=self.server.fleet.execute(key,action,op)
        except (KeyError,TypeError,ValueError):
            return self.send_error(400)
        except OSError:
            return self.send_error(503,"robot agent unavailable")
        data=json.dumps({"operation_id":op,**result},ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__=="__main__":
    rclpy.init()
    fleet=Fleet()
    server=ThreadingHTTPServer(("0.0.0.0",8119),Handler)
    server.fleet=fleet
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        rclpy.spin(fleet)
    finally:
        server.shutdown()
        fleet.flight.publish(Twist())
