"""Publish only synthetic RGB/CameraInfo/PointCloud2 into an isolated ROS domain.

The board holder reads operator feedback and moves only the generated target.
The solver gets no reference transforms, pose indices or topic describing truth.
"""
import json
import os
import time
from functools import lru_cache
from urllib.request import urlopen

import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, Image, PointCloud2, PointField

from four_hole_scene import HOLES, K, POSES, cloud, render


@lru_cache(maxsize=16)
def image_for(index, partial):
    axis, shift = POSES[index]
    return render(axis, shift, omit=(4,) if partial else ())


def cloud_message(points, stamp, role):
    msg = PointCloud2()
    msg.header.stamp = stamp
    msg.header.frame_id = "lidar_"+role
    msg.height, msg.width = 1, len(points)
    msg.point_step = 12
    msg.row_step = 12*len(points)
    msg.fields = [PointField(name=name, offset=4*i, datatype=PointField.FLOAT32, count=1)
                  for i,name in enumerate("xyz")]
    msg.is_dense = True
    msg.data = points.astype("<f4").tobytes()
    return msg


class BoardHolder:
    def __init__(self):
        self.task = None
        self.last_status = 0.
        self.status = {}
        self.first_seen = {}

    def state(self):
        now = time.monotonic()
        if now-self.last_status >= .35:
            try:
                with urlopen("http://calibration-session:8141/status",timeout=.8) as r:
                    self.status=json.load(r)
            except OSError:
                pass
            self.last_status=now
        if self.task != self.status.get("task_id"):
            self.task = self.status.get("task_id")
            self.first_seen.clear()
        count = self.status.get("samples",0)
        feedback = self.status.get("capture_feedback") or {}
        condition = feedback.get("condition")
        if condition and condition not in self.first_seen:
            self.first_seen[condition]=now
            print(json.dumps({"samples":count, "operator_received":condition},ensure_ascii=False))
        # The holder exposes each defect until the station has reported that exact problem.
        def awaiting(hint):
            return now - self.first_seen.get(hint, now) < 2
        partial = count==0 and awaiting("partial_markers")
        block_right = count==0 and awaiting("board_missing_in_lidar")
        repeat = count==1 and awaiting("repeated_pose")
        index = 0 if repeat else min(count,len(POSES)-1)
        return index, partial, block_right


def main():
    if os.getenv("ROS_DOMAIN_ID")!="171":
        raise ValueError("use the isolated synthetic fixture domain 171")
    rclpy.init()
    n = rclpy.create_node("synthetic_four_hole_test_rig")
    q = qos_profile_sensor_data
    camera = n.create_publisher(Image,"/calibration/rgbd/image",q)
    intrinsics = n.create_publisher(CameraInfo,"/calibration/rgbd/camera_info",q)
    lasers = {role:n.create_publisher(PointCloud2,"/calibration/lidar_"+role+"/points",q)
              for role in ("left","right")}
    sample=0
    holder=BoardHolder()
    while rclpy.ok():
        index,partial,blocked=holder.state()
        stamp=n.get_clock().now().to_msg()
        rgb=image_for(index,partial)
        color=Image()
        color.header.frame_id="rgbd_optical"
        color.header.stamp=stamp
        color.height,color.width=rgb.shape[:2]
        color.step=color.width*3
        color.encoding="rgb8"
        color.data=rgb.tobytes()
        info=CameraInfo()
        info.header=color.header
        info.width,info.height=color.width,color.height
        info.k=K.ravel().tolist()
        info.distortion_model="plumb_bob"
        intrinsics.publish(info)
        camera.publish(color)
        for i,(role,pub) in enumerate(lasers.items()):
            holes=HOLES[:3] if role=="right" and blocked else HOLES
            points=cloud(*POSES[index],role,holes=holes,seed=8929+sample*2+i)
            pub.publish(cloud_message(points,stamp,role))
        sample+=1
        rclpy.spin_once(n,timeout_sec=.25)


if __name__=="__main__":
    main()
