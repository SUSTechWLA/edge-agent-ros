"""Emit two distinct stamped pixel patterns for the optional operator recorder test."""
import time

from gz.msgs10.image_pb2 import Image as GzImage
from gz.transport13 import Node
import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image


def main():
    rclpy.init()
    ros=rclpy.create_node("recorder_camera_fixture")
    onboard=ros.create_publisher(Image,"/fixture/onboard",qos_profile_sensor_data)
    gz=Node()
    overview=gz.advertise("/fixture/overview",GzImage)
    i=0
    try:
        while rclpy.ok():
            i+=1
            ns=i*250_000_000
            sec,sub=divmod(ns,10**9)
            a=Image(width=2,height=1,encoding="rgb8",step=6,data=bytes([i%256,20,30]*2))
            a.header.stamp.sec,a.header.stamp.nanosec=sec,sub
            a.header.frame_id="onboard_optical"
            b=GzImage(width=2,height=1,step=6,pixel_format_type=3,data=bytes([10,i%256,210]*2))
            b.header.stamp.sec,b.header.stamp.nsec=sec,sub
            onboard.publish(a)
            overview.publish(b)
            time.sleep(.25)
    finally:
        ros.destroy_node()
        rclpy.try_shutdown()


if __name__=="__main__":
    main()
