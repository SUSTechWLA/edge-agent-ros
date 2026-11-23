"""Synthetic ROS graph exercise for the stationary rover input probe. Run inside an isolated ROS container."""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys


def publish(case):
    import rclpy
    from geometry_msgs.msg import TransformStamped
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import LaserScan
    from tf2_ros import StaticTransformBroadcaster, TransformBroadcaster
    from rclpy.qos import qos_profile_sensor_data

    rclpy.init()
    node=rclpy.create_node("synthetic_stationary_sensor")
    static=StaticTransformBroadcaster(node)
    dynamic=TransformBroadcaster(node)
    def transform(parent,child):
        tf=TransformStamped()
        tf.header.frame_id=parent
        tf.header.stamp=node.get_clock().now().to_msg()
        tf.child_frame_id=child
        tf.transform.rotation.w=1.
        return tf
    mount=transform("base_link","laser_link")
    mount.transform.translation.x=.25 if case=="bad-tf" else .08
    mount.transform.translation.z=.4
    static.sendTransform(mount)
    laser=node.create_publisher(LaserScan,"/scan",qos_profile_sensor_data)
    wheel=node.create_publisher(Odometry,"/odom",qos_profile_sensor_data)
    def sample():
        pose=transform("odom","base_link")
        dynamic.sendTransform(pose)
        odom=Odometry()
        odom.header=pose.header
        odom.child_frame_id="base_link"
        odom.pose.pose.orientation.w=1.
        wheel.publish(odom)
        scan=LaserScan()
        scan.header.stamp=pose.header.stamp
        scan.header.frame_id="laser_link"
        scan.angle_min=-math.pi
        scan.angle_increment=math.tau/360.
        scan.angle_max=scan.angle_min+359*scan.angle_increment
        scan.range_min=.10
        scan.range_max=8.
        scan.ranges=[float("inf")]*360
        laser.publish(scan)
    node.create_timer(.05,sample)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


def verify(out):
    out.mkdir(parents=True,exist_ok=True)
    probe=Path(__file__).resolve().parents[2]/"client/rover_input_probe.py"
    results={}
    for case,expected in (("nominal",set()),("bad-tf",{"tf.differs_from_site_calibration"}),
                          ("missing",{"odom.coverage_gap","scan.coverage_gap","tf.missing_at_scan_stamp"})):
        producer=None
        if case!="missing":
            producer=subprocess.Popen([sys.executable,"-B",__file__,"--publish",case],
                                      stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        try:
            response=subprocess.run([sys.executable,"-B",str(probe),"--scan-frame","laser_link",
                                     "--scan-xy-yaw",".08","0","0","--seconds","3"],timeout=15,capture_output=True,text=True)
            if not response.stdout.strip():
                raise RuntimeError(response.stderr)
            data=json.loads(response.stdout)
            (out/(case+".json")).write_text(json.dumps(data,indent=2))
            results[case]=(response.returncode==int(bool(expected)) and set(data["issues"])==expected and
                           not data["motion_authorized"] and data["visible_command_publishers"]==[] and
                           (producer is None or producer.poll() is None))
        finally:
            if producer is not None:
                producer.terminate()
                producer.wait(timeout=5)
    print(json.dumps(results))
    (out/"result.json").write_text(json.dumps({"source":"synthetic ROS inputs","results":results},indent=2))
    return 0 if all(results.values()) else 1


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    m=p.add_mutually_exclusive_group(required=True)
    m.add_argument("--publish",choices=("nominal","bad-tf"))
    m.add_argument("--out",type=Path)
    a=p.parse_args()
    if a.publish:publish(a.publish)
    else:sys.exit(verify(a.out))
