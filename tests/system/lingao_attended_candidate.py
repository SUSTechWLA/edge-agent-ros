"""Run the installed Lingao candidate in a fresh localhost DDS domain; live MCU inputs are read-only."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid


def spin(node, duration):
    import rclpy
    end = time.monotonic()+duration
    while time.monotonic()<end:
        rclpy.spin_once(node, timeout_sec=.012)


def shadow(name):
    import rclpy
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    assert os.environ["ROS_LOCALHOST_ONLY"] == "1" and 110 <= int(os.environ["ROS_DOMAIN_ID"]) <= 170
    root = "/edge_shadow_" + uuid.UUID(name).hex
    velocity, odom = root+"/candidate", root+"/synthetic_odom"
    rclpy.init()
    node = rclpy.create_node("isolated_lingao_witness", enable_rosout=False, start_parameter_services=False)
    spin(node, 1.5)
    assert set(node.get_node_names()) == {"isolated_lingao_witness"}
    assert node.count_subscribers(velocity)==node.count_publishers(velocity)==0
    publisher = node.create_publisher(Odometry, odom, 10)
    observed = []
    node.create_subscription(Twist,velocity,lambda m:observed.append(
        [m.linear.x,m.linear.y,m.linear.z,m.angular.x,m.angular.y,m.angular.z]),10)
    spin(node,.5)
    assert [e.node_name for e in node.get_subscriptions_info_by_topic(velocity)] == ["isolated_lingao_witness"]
    executable=Path("/home/cas/humble_ws/install/lingao_base/lib/lingao_base/move_relative.py")
    sha=hashlib.sha256(executable.read_bytes()).hexdigest()
    assert sha=="9e90d676ef6ae9fc7df4b04cea6fac6c6577f066703810f4a51fa5fe5b5270bc"
    cmd=[sys.executable,str(executable),"--distance","0.05","--speed","0.1",
         "--cmd-vel-topic",velocity,"--odom-topic",odom]
    process=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    at=time.monotonic()
    position=0.
    try:
        while process.poll() is None and time.monotonic()-at<10:
            if observed and observed[-1][0]==.1:
                position=min(.052,position+.002)
            m=Odometry();m.header.stamp=node.get_clock().now().to_msg()
            m.header.frame_id="shadow_odom";m.child_frame_id="shadow_base";m.pose.pose.orientation.w=1.
            m.pose.pose.position.x=position
            publisher.publish(m)
            assert all(e.node_name=="isolated_lingao_witness" for e in node.get_subscriptions_info_by_topic(velocity))
            spin(node,.02)
        assert process.poll()==0
        spin(node,.12)
    finally:
        if process.poll() is None: process.terminate()
        stdout,_=process.communicate(timeout=3)
    node.destroy_node();rclpy.shutdown()
    positive=sum(r==[.1,0.,0.,0.,0.,0.] for r in observed)
    zeros=sum(r==[0.]*6 for r in observed)
    assert positive>0 and zeros>0 and positive+zeros==len(observed) and observed[-1]==[0.]*6
    return {"scope":"isolated","domain":int(os.environ["ROS_DOMAIN_ID"]), "namespace":root,
            "candidate_topic":velocity,"odom_topic":odom,"only_subscriber":"isolated_lingao_witness",
            "vendor_sha256":sha,"argv":cmd,"positive_candidates":positive,"zeros":zeros,"velocity_vectors":list(map(list,set(map(tuple,observed)))),
            "synthetic_final_x_m":position,"vendor_exit":0,"vendor_log_tail":stdout.splitlines()[-2:]}


def run():
    import rclpy
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data as qos
    assert os.environ["ROS_LOCALHOST_ONLY"]=="1" and os.environ["ROS_DOMAIN_ID"]=="61"
    rclpy.init()
    n=rclpy.create_node("live_lingao_readonly_witness",enable_rosout=False,start_parameter_services=False)
    rows=[];odom=[]
    def wheels(m):
        t=m.twist.twist; p=m.pose.pose.position
        odom.append((m.header.stamp.sec*10**9+m.header.stamp.nanosec,m.header.frame_id,m.child_frame_id,
                     p.x,p.y,math.hypot(t.linear.x,t.linear.y),abs(t.angular.z)))
    n.create_subscription(Odometry,"/lingao/odom",wheels,qos)
    n.create_subscription(Twist,"/cmd_vel_safe",lambda m:rows.append(max(abs(getattr(x,k)) for x in (m.linear,m.angular) for k in "xyz")),qos)
    spin(n,3)
    def owners(topic):
        return [e.node_name for e in n.get_publishers_info_by_topic(topic)]
    assert owners("/cmd_vel_safe")==["vehicle_web_remote"] and owners("/lingao/odom")==["lingao_base_driver"]
    assert len(rows)>5 and len(odom)>5 and not any(rows) and max(s[5] for s in odom)<.005
    begun=time.time_ns();op=str(uuid.uuid4());env=dict(os.environ,ROS_DOMAIN_ID=str(110+secrets.randbelow(61)))
    p=subprocess.Popen([sys.executable,__file__,"--shadow",op],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    start=len(odom);count=len(rows)
    try:
        until=time.monotonic()+18
        while p.poll() is None and time.monotonic()<until:spin(n,.05)
        assert p.poll()==0, (p.stderr.read()[-1200:] if p.poll() is not None else 'shadow timeout')
        out,_=p.communicate(); candidate=json.loads(out.splitlines()[-1])
        spin(n,.7)
    finally:
        if p.poll() is None:p.kill();p.wait()
    during=odom[start:];cmd=rows[count:]
    assert cmd and max(cmd)==0 and len(during)>20 and max(s[5] for s in during)<.005 and max(s[6] for s in during)<.02
    assert all(s[1:3]==("odom","base_foot") for s in during)
    assert len({s[0] for s in during})==len(during)
    displacement=max(math.dist(s[3:5],during[0][3:5]) for s in during)
    assert displacement<.003
    n.destroy_node();rclpy.shutdown()
    return {"schema":"edge.lingao.attended-candidate-isolation.v1","profile":"forward-50mm-100mmps",
            "started_ns":str(begun),"ended_ns":str(time.time_ns()),"operation_id":op,"candidate":candidate,
            "real_observation":{"domain":61,"safe_samples":len(cmd),"safe_abs_max":max(cmd),
            "odom_samples":len(during),"frame":"odom","child":"base_foot","peak_measured_linear_m_s":max(s[5] for s in during),
            "peak_measured_angular_rad_s":max(s[6] for s in during),"peak_observed_displacement_m":displacement}}


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--shadow");args=p.parse_args()
    print(json.dumps(shadow(args.shadow) if args.shadow else run(),ensure_ascii=False,allow_nan=False),flush=True)
