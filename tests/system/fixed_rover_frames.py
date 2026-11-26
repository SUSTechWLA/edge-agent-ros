"""Simulator adapter for the Lingao odometry name and the known, non-calibrated static frames."""
from copy import deepcopy
import json
import os
from pathlib import Path

import rclpy
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from tf2_ros.static_transform_broadcaster import StaticTransformBroadcaster


class Frames(Node):
    def __init__(self,profile):
        super().__init__("fixed_rover_frames")
        self.out=self.create_publisher(Odometry,"/lingao/odom",10)
        self.create_subscription(Odometry,"/fixed/odom",self.copy_odom,10)
        self.static=StaticTransformBroadcaster(self)
        camera=profile["extrinsics"]["rgbd"]
        imu=profile["extrinsics"]["imu"]
        frames=[]
        for parent,child,xyz,q in [
            ("base_link","base_foot",[0.,0.,0.],[0.,0.,0.,1.]),
            (camera["parent_frame"],camera["child_frame"],camera["translation_m"],camera["rotation_xyzw"]),
            (imu["parent_frame"],imu["child_frame"],imu["translation_m"],imu["rotation_xyzw"]),
            ("base_link","fixture_clearance",[0.,0.,.32],[0.,0.,0.,1.]),
        ]:
            tf=TransformStamped()
            tf.header.frame_id=parent
            tf.child_frame_id=child
            t,r=tf.transform.translation,tf.transform.rotation
            t.x,t.y,t.z=map(float,xyz)
            r.x,r.y,r.z,r.w=map(float,q)
            frames.append(tf)
        self.static.sendTransform(frames)

    def copy_odom(self,msg):
        if msg.header.frame_id=="odom" and msg.child_frame_id=="base_link":
            result=deepcopy(msg)
            result.child_frame_id="base_foot"
            self.out.publish(result)


if __name__=="__main__":
    rclpy.init()
    node=Frames(json.loads(Path(os.environ["EDGE_CALIBRATION_PROFILE"]).read_bytes()))
    try:
        rclpy.spin(node)
    except (ExternalShutdownException, KeyboardInterrupt):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
