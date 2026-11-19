"""Acquire a stationary ROS 2 hardware window, then pass it to the factory verifier."""
import argparse
import json
import math
from pathlib import Path
import sys
import time
import uuid

import numpy as np


def stamp(msg):
    return msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec


def cloud_counts(msg):
    fields = {f.name: f for f in msg.fields}
    if (not 0 < msg.width * msg.height <= 2_000_000 or msg.point_step < 12 or
            msg.row_step < msg.width * msg.point_step or len(msg.data) != msg.row_step * msg.height):
        raise ValueError("invalid PointCloud2 layout")
    finite = None
    for axis in "xyz":
        f = fields.get(axis)
        if f is None or f.datatype != 7 or f.count != 1 or f.offset + 4 > msg.point_step:
            raise ValueError("expected float32 XYZ fields")
        values = np.ndarray((msg.height,msg.width), dtype=">f4" if msg.is_bigendian else "<f4",
                            buffer=msg.data, offset=f.offset, strides=(msg.row_step,msg.point_step))
        valid = np.isfinite(values)
        finite = valid if finite is None else finite & valid
    return {"point_count":msg.width*msg.height,"finite_xyz_count":int(finite.sum())}


def depth_count(msg):
    kinds = {"16UC1": "u2", "32FC1": "f4"}
    dtype = np.dtype((">" if msg.is_bigendian else "<") + kinds[msg.encoding])
    if (not 0 < msg.width*msg.height <= 8_500_000 or msg.step < msg.width*dtype.itemsize
            or len(msg.data) != msg.step*msg.height):
        raise ValueError("invalid aligned depth layout")
    values = np.ndarray((msg.height,msg.width),dtype=dtype,buffer=msg.data,strides=(msg.step,dtype.itemsize))
    return {"depth_valid_count":int((np.isfinite(values)&(values>0)).sum())}


def acquire(node, bindings, duration):
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import CameraInfo, Image, Imu, PointCloud2

    sources = bindings["sensors"]
    samples = {k:[] for k in ("lidar","rgbd","imu","color","info")}
    errors = {}
    frames = {}
    def receive(key,msg):
        rows = samples[key]
        frame = msg.header.frame_id
        if key in errors:
            return
        try:
            if len(rows) >= 4096 or frames.setdefault(key,frame) != frame:
                raise ValueError("capture overflow or changing sensor frame")
            row = dict(stamp_ns=stamp(msg),received_ns=node.get_clock().now().nanoseconds)
            if key == "lidar":
                row.update(cloud_counts(msg))
            elif key == "imu":
                for name,v in (("accel_mps2",msg.linear_acceleration),("gyro_rad_s",msg.angular_velocity)):
                    vector = [v.x,v.y,v.z]
                    if not all(map(math.isfinite,vector)):
                        raise ValueError("nonfinite IMU axis")
                    row[name] = vector
            else:
                row.update(width=msg.width,height=msg.height)
                if key == "rgbd":
                    row.update(depth_count(msg))
                elif key == "info":
                    row["k"] = [float(x) for x in msg.k]
                    if not all(map(math.isfinite, row["k"])):
                        raise ValueError("nonfinite CameraInfo intrinsics")
                elif msg.step <= 0 or len(msg.data) != msg.height*msg.step:
                    raise ValueError("invalid color payload")
            rows.append(row)
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            errors[key] = str(exc)
    channels = [(k,t,sources[k]["topic"]) for k,t in (("imu",Imu),("lidar",PointCloud2),("rgbd",Image))]
    channels += [("color",Image,sources["rgbd"]["color_topic"]),("info",CameraInfo,sources["rgbd"]["info_topic"])]
    for key,kind,topic in channels:
        node.create_subscription(kind,topic,lambda msg,k=key:receive(k,msg),qos_profile_sensor_data)
    end = time.monotonic() + duration
    while time.monotonic() < end:
        rclpy.spin_once(node,timeout_sec=.1)
    # Match by acquisition time after the window closes so callback arrival order is immaterial.
    # DDS can deliver the first/last member of a triple just outside the window.
    # At most 200 ms of unmatched boundary data is discarded per side. Longer
    # absences and holes inside the window still reach the skew check.
    aligned = []
    for row in samples["rgbd"]:
        trim = False
        for side in ("color","info"):
            if not samples[side] or frames.get(side) != frames.get("rgbd"):
                errors["rgbd"] = "missing or different optical frame: "+side
                break
            other = min(samples[side],key=lambda v:abs(v["stamp_ns"]-row["stamp_ns"]))
            t = row["stamp_ns"]
            if (50_000_000 < abs(other["stamp_ns"]-t) <= 200_000_000 and
                    (t < samples[side][0]["stamp_ns"] or t > samples[side][-1]["stamp_ns"])):
                trim = True
            if (other["width"],other["height"]) != (row["width"],row["height"]):
                errors["rgbd"] = "aligned stream dimensions differ"
                break
            row[side+"_stamp_ns"] = other["stamp_ns"]
            if side=="info":
                row["k"] = other["k"]
        if not trim:
            aligned.append(row)
    trimmed_depth_samples = len(samples["rgbd"])-len(aligned)
    samples["rgbd"] = aligned
    inputs = {}
    for k in ("lidar","rgbd","imu"):
        bad = k in errors or (k=="rgbd" and ("info" in errors or "color" in errors))
        inputs[k] = dict(serial=sources[k]["serial"],topic=sources[k]["topic"],
                         frame=frames.get(k),samples=[] if bad else samples[k])
    return dict(schema_version=1,robot_id=bindings["robot_id"],environment="hardware",
                stationary_reference=bindings["stationary_reference"],clock_domain=bindings["clock_domain"],
                sensors=inputs, acquisition_errors=errors, unmatched_boundary_depth=trimmed_depth_samples)


def main():
    import rclpy
    from .factory import read_input, verify
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--profile",required=True,type=Path)
    cli.add_argument("--identity",required=True,type=Path)
    cli.add_argument("--robot-id",required=True)
    cli.add_argument("--state",required=True,type=Path)
    cli.add_argument("--seconds",type=int,choices=range(3,31),default=8,metavar="3..30")
    args=cli.parse_args()
    operation="factory-"+uuid.uuid4().hex
    # Keep a rejected attempt in the requested robot's history even if its input
    # files are missing or belong to a different vehicle.
    profile={"schema_version":1, "robot_id":args.robot_id, "environment":"hardware"}
    try:
        candidate=read_input(args.profile)
        bind=read_input(args.identity)
        if (not isinstance(candidate,dict) or not isinstance(bind,dict) or
                args.robot_id != candidate.get("robot_id") or args.robot_id != bind.get("robot_id")
                or candidate.get("environment") != "hardware" or not bind.get("stationary_reference")):
            raise ValueError("station identity / fixed fixture record / hardware profile mismatch")
        profile=candidate
        rclpy.init(args=[])
        node=rclpy.create_node("edge_factory_capture")
        try:
            capture=acquire(node,bind,args.seconds)
        finally:
            node.destroy_node()
            rclpy.try_shutdown()
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        capture={"schema_version":1,"robot_id":args.robot_id,"environment":"hardware","acquisition_errors":{"station":str(exc)}}
    result=verify(profile,capture,args.state,operation)
    if capture["acquisition_errors"]:
        result={**result,"acquisition_errors":capture["acquisition_errors"]}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return 0 if result["status"]=="VERIFIED" else 2


if __name__=="__main__":
    sys.exit(main())
