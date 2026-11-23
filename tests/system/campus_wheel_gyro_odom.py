"""Simulation skid-steer odometry using wheel speed, IMU gyro and optional scan translation."""
from collections import deque
import argparse
import json
import math

import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Imu, LaserScan
from std_msgs.msg import String
from campus_scan_translation import Scan,ScanCorrection


def stamp(message):
    t=message.header.stamp
    return t.sec*10**9+t.nanosec


class WheelGyroOdometry(Node):
    def __init__(self,use_scan):
        super().__init__("lingao_fixture_wheel_gyro_odometry")
        self.gyro=deque(maxlen=96)
        self.wheels=deque(maxlen=32)
        self.previous=None
        self.x=self.y=0.
        self.poses=deque(maxlen=64)
        self.pending=deque(maxlen=8)
        self.scan_update=ScanCorrection()
        self.result=self.create_publisher(Odometry,"/odom",10)
        self.create_subscription(Imu,"/imu",self.on_gyro,qos_profile_sensor_data)
        self.create_subscription(Odometry,"/lingao/wheel_odom",self.on_wheels,10)
        if use_scan:
            self.status=self.create_publisher(String,"/lingao/scan_correction",10)
            self.create_subscription(LaserScan,"/scan",self.on_scan,qos_profile_sensor_data)

    def on_scan(self,msg):
        if msg.header.frame_id=="laser_link":
            self.pending.append(msg)
            self.correct_scans()

    def correct_scans(self):
        while self.pending and len(self.poses)>=2:
            msg=self.pending[0]
            t=stamp(msg)
            if t>self.poses[-1][0]:
                return
            self.pending.popleft()
            if t<self.poses[0][0]:
                continue
            while len(self.poses)>2 and self.poses[1][0]<t:
                self.poses.popleft()
            a,b=self.poses[0],self.poses[1]
            f=(t-a[0])/(b[0]-a[0])
            pose=tuple(x+f*(y-x) for x,y in zip(a[1:],b[1:]))
            # Fixed extrinsic of the SDF's virtual horizontal lidar.
            scan=Scan(msg.ranges,msg.angle_min,msg.angle_increment,msg.range_min,msg.range_max,.08)
            reason=self.scan_update.update(t,scan,pose)
            note=String()
            note.data=json.dumps({"stamp_ns":t,"method":reason,"dx_m":self.scan_update.dx,"dy_m":self.scan_update.dy})
            self.status.publish(note)

    def on_gyro(self,msg):
        t=stamp(msg)
        rate=msg.angular_velocity.z
        if msg.header.frame_id!="imu_link" or not math.isfinite(rate):
            return
        if self.gyro and t<=self.gyro[-1][0]:
            return
        yaw=0.
        if self.gyro:
            last,last_rate,last_yaw=self.gyro[-1]
            # A discontinuity makes displacement since initialization unknowable.
            if t-last>150_000_000:
                return
            yaw=last_yaw+.5*(last_rate+rate)*(t-last)*1e-9
        self.gyro.append((t,rate,yaw))
        self.drain()

    def on_wheels(self,msg):
        if msg.header.frame_id!="odom" or msg.child_frame_id!="base_link" or not math.isfinite(msg.twist.twist.linear.x):
            return
        self.wheels.append(msg)
        self.drain()

    def drain(self):
        while self.wheels and len(self.gyro)>=2:
            msg=self.wheels[0]
            t=stamp(msg)
            if t>self.gyro[-1][0]:
                return
            self.wheels.popleft()
            if t<self.gyro[0][0]:
                continue
            while len(self.gyro)>2 and self.gyro[1][0]<t:
                self.gyro.popleft()
            a,b=self.gyro[0],self.gyro[1]
            fraction=(t-a[0])/(b[0]-a[0])
            rate=a[1]+fraction*(b[1]-a[1])
            yaw=a[2]+.5*(a[1]+rate)*(t-a[0])*1e-9
            speed=msg.twist.twist.linear.x
            if self.previous:
                old_t,old_yaw,old_speed=self.previous
                if t<=old_t or t-old_t>150_000_000:
                    continue
                distance=.5*(old_speed+speed)*(t-old_t)*1e-9
                direction=.5*(old_yaw+yaw)
                self.x+=distance*math.cos(direction)
                self.y+=distance*math.sin(direction)
            self.previous=t,yaw,speed
            self.poses.append((t,self.x,self.y,yaw))
            self.correct_scans()
            msg.pose.pose.position.x=self.x+self.scan_update.dx
            msg.pose.pose.position.y=self.y+self.scan_update.dy
            q=msg.pose.pose.orientation
            q.x=q.y=0.
            q.z,q.w=math.sin(yaw/2),math.cos(yaw/2)
            msg.twist.twist.angular.z=rate
            self.result.publish(msg)


if __name__=="__main__":
    cli=argparse.ArgumentParser()
    cli.add_argument("--scan",action="store_true")
    args=cli.parse_args()
    rclpy.init()
    node=WheelGyroOdometry(args.scan)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
