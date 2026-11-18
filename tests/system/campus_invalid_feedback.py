"""SDD-033. Feed the real Gazebo/ROS interfaces inside an isolated container."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener

import rclpy
from geometry_msgs.msg import Twist
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.transport13 import Node as GzNode
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Imu, JointState
from std_msgs.msg import String


def exercise():
    if os.getenv("GZ_PARTITION") != "edge_invalid_feedback_fixture" or os.getenv("ROS_DOMAIN_ID") != "171":
        raise RuntimeError("requires the isolated fixture partition and ROS domain")
    root=Path("/opt/edge/scene/campus_large/robot_agent")
    sys.path.insert(0,str(root))
    from catalog import JOINTS

    rclpy.init()
    node=rclpy.create_node("feedback_witness")
    gz=GzNode()
    pose_pub=gz.advertise("/world/edge_lab/dynamic_pose/info",Pose_V)
    keys=("crazyflie","go1","g1")
    imu={k:node.create_publisher(Imu,f"/model/{k}/imu",qos_profile_sensor_data) for k in keys}
    joints={k:node.create_publisher(JointState,f"/model/{k}/joint_state",qos_profile_sensor_data) for k in JOINTS}
    xyz_z=.1
    leg_angle=.15
    acceleration=0.
    velocity=[]
    states={}
    def update(msg):
        states.update({r["id"]:r for r in json.loads(msg.data)["robots"]})
    node.create_subscription(String,"/edge/robot_states",update,5)
    node.create_subscription(Twist,"/model/crazyflie/cmd_vel",lambda m: velocity.append(m.linear.z),10)
    def wait(check,context,timeout=15):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            stamp=node.get_clock().now().to_msg()
            poses=Pose_V()
            poses.header.stamp.sec=stamp.sec
            poses.header.stamp.nsec=stamp.nanosec
            for k in keys:
                body=poses.pose.add()
                body.name=k
                body.position.z=xyz_z if k=="crazyflie" else .4
                body.orientation.w=1.
                measure=Imu()
                measure.header.stamp=stamp
                measure.linear_acceleration.x=acceleration if k=="g1" else 0.
                imu[k].publish(measure)
                if k in joints:
                    angles=JointState()
                    angles.header.stamp=stamp
                    angles.name=JOINTS[k]
                    angles.position=[leg_angle if k=="go1" else 0.]*len(angles.name)
                    joints[k].publish(angles)
            pose_pub.publish(poses)
            rclpy.spin_once(node,timeout_sec=.03)
            if result:=check():
                return result
            time.sleep(.025)
        raise TimeoutError(context)

    http=build_opener(ProxyHandler({}))
    def action(k,act,op):
        data={"version":"1","operation_id":op,"arguments":{"action":act}}
        req=Request("http://127.0.0.1:8119/execute/"+k,data=json.dumps(data).encode(),headers={"Content-Type":"application/json"})
        with http.open(req,timeout=3) as reply:
            return json.load(reply)

    provider=subprocess.Popen([sys.executable,"-B",str(root/"provider.py")])
    try:
        wait(lambda:all(states.get(k,{}).get("status")=="online" and states[k].get("imu_stamp_s") for k in keys),"fresh telemetry")
        assert action("crazyflie","起飞","nan-flight")["state"]=="RUNNING"
        before=wait(lambda:velocity and velocity[-1]>0 and velocity[-1],"initial climb velocity")
        xyz_z=leg_angle=acceleration=math.nan
        wait(lambda:states["crazyflie"]["status"]=="waiting" and
             "joints" not in states["go1"] and "imu_stamp_s" not in states["g1"] and
             velocity[-1]==0.,"zero velocity and per-body invalidation")
        blocked={k:action(k,act,"invalid-"+k) for k,act in (("go1","伸腿"),("g1","放下左臂"))}
        for k,source in (("go1","joints"),("g1","imu")):
            assert blocked[k]["state"]=="BLOCKED" and source in blocked[k]["error"]["invalid_sources"]
        def stopped():
            response=action("crazyflie","起飞","nan-flight")
            return response if response["state"]=="BLOCKED" else None
        failure=wait(stopped,"persisted flight receipt",15)
        assert "pose" in failure["error"]["invalid_sources"]
        sku={k:states[k]["status"] for k in keys}
        xyz_z=.1
        leg_angle=acceleration=0.
        wait(lambda:all(states[k]["status"]=="online" and states[k].get("imu_stamp_s") for k in keys)
             and states["go1"].get("joints"),"fresh data on every body")
        assert action("crazyflie","起飞","nan-flight")==failure
        assert action("crazyflie","起飞","shutdown-flight")["state"]=="RUNNING"
        wait(lambda:velocity and velocity[-1]>0,"flight before shutdown")
        after=len(velocity)
        provider.terminate()
        wait(lambda:any(value==0. for value in velocity[after:]),"new stop command on SIGTERM",5)
        exit_code=provider.wait(timeout=5)
        assert exit_code==0
        return {"passed":True,"fixture":"Gazebo Transport + ROS 2 input; external ROS velocity witness; no simulated plant",
                "before_vertical_mps":before,"last_vertical_mps":velocity[-1],"bad_frame_status":sku,
                "denied":blocked,"flight_receipt":failure,"same_receipt_after_recovery":True,
                "sigterm_exit_code":exit_code,"stop_after_sigterm":True}
    finally:
        provider.terminate()
        provider.wait(timeout=5)
        node.destroy_node()
        rclpy.shutdown()


if __name__=="__main__":
    print(json.dumps(exercise(),ensure_ascii=False,indent=2))
