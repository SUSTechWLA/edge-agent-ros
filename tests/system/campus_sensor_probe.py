"""Read-only ROS subscriber and independent assertions for body-mounted campus instruments."""
from collections import deque
import json
import math
from pathlib import Path
import struct
import sys
import zlib

PAIRS = ("edge_rover","g1")
TOPICS={}
for body in PAIRS:
    for k,suffix in (("color","image_raw"),("depth","depth/image_raw"),("info","camera_info")):
        TOPICS[body+"."+k]=(f"/model/{body}/rgbd/"+suffix,"CameraInfo" if k=="info" else "Image",body+"/rgbd_optical")
TOPICS.update({
    "crazyflie.color":("/model/crazyflie/front/image_raw","Image","crazyflie/front_optical"),
    "crazyflie.info":("/model/crazyflie/front/camera_info","CameraInfo","crazyflie/front_optical"),
    "crazyflie.down":("/model/crazyflie/down/scan","LaserScan","crazyflie/down_link"),
    "go1.cloud":("/model/go1/lidar/points","PointCloud2","go1/lidar_link"),
    "edge_rover.wheels":("/model/edge_rover/wheel_states","JointState",None),
    "edge_rover.scan":("/scan","LaserScan","laser_link"),
})
for body in ("edge_rover","crazyflie","go1","g1"):
    TOPICS[body+".imu"]=(("/imu" if body=="edge_rover" else f"/model/{body}/imu"),"Imu",
                         ("imu_link" if body=="edge_rover" else body+"/imu_link"))


def measure(msg,kind):
    import numpy as np
    if kind=="Image":
        if msg.encoding=="32FC1":
            depth=np.frombuffer(msg.data,dtype=">f4" if msg.is_bigendian else "<f4").reshape(msg.height,msg.step//4)[:,:msg.width]
            finite=depth[np.isfinite(depth)&(depth>0)]
            return [msg.width,msg.height,int(finite.size),round(float(np.median(finite)),3) if finite.size else None]
        assert msg.encoding.lower() in {"rgb8","bgr8"} and len(msg.data)==msg.step*msg.height
        rgb=np.frombuffer(msg.data,dtype=np.uint8)
        return [msg.width,msg.height,int(rgb.max())-int(rgb.min()),zlib.crc32(bytes(msg.data))]
    if kind=="CameraInfo":
        return [msg.width,msg.height,round(msg.k[0],3),round(msg.k[4],3)]
    if kind=="LaserScan":
        hits=[v for v in msg.ranges if math.isfinite(v) and msg.range_min<=v<=msg.range_max]
        return [len(msg.ranges),len(hits),round(min(hits),4) if hits else None]
    if kind=="PointCloud2":
        from sensor_msgs_py.point_cloud2 import read_points_numpy
        points=read_points_numpy(msg,field_names=("x","y","z")).reshape(-1,3)
        positions=points[np.isfinite(points).all(axis=1)]
        return [msg.width,msg.height,len(positions),round(float(np.ptp(positions[:,2])),3) if len(positions) else 0]
    if kind=="JointState":
        values=dict(zip(msg.name,msg.velocity))
        return [round(values[n],4) for n in ("left_axle","right_axle")]
    acc=msg.linear_acceleration
    gyro=msg.angular_velocity
    return [round(v,4) for v in (acc.x,acc.y,acc.z,gyro.x,gyro.y,gyro.z)]


def record(folder):
    import rclpy
    from rclpy.qos import qos_profile_sensor_data, QoSProfile, DurabilityPolicy
    import sensor_msgs.msg as messages
    from tf2_msgs.msg import TFMessage
    rclpy.init()
    node=rclpy.create_node("field_sensor_witness")
    rows={k:deque(maxlen=950) for k in TOPICS}
    counts={k:0 for k in TOPICS}
    errors={}
    frames={}
    def receive(key,msg):
        try:
            kind,frame=TOPICS[key][1:]
            if frame and frame!=msg.header.frame_id:
                raise ValueError(f"frame {msg.header.frame_id!r}, expected {frame!r}")
            stamp=msg.header.stamp.sec*10**9+msg.header.stamp.nanosec
            if rows[key] and stamp<=rows[key][-1][0]:
                return
            counts[key]+=1
            # Keep at least 2 samples/s for fast IMU/encoder sources, plus all camera and range data.
            if kind=="Imu" and rows[key] and stamp-rows[key][-1][0]<200_000_000:
                return
            rows[key].append([stamp,*measure(msg,kind)])
        except Exception as exc:
            errors[key]=str(exc) or type(exc).__name__
    def static(msg):
        for t in msg.transforms:
            p,q=t.transform.translation,t.transform.rotation
            frames[t.child_frame_id]=[t.header.frame_id,[p.x,p.y,p.z],[q.x,q.y,q.z,q.w]]
    node.create_subscription(TFMessage,"/tf_static",static,QoSProfile(depth=10,durability=DurabilityPolicy.TRANSIENT_LOCAL))
    for key,(topic,kind,_) in TOPICS.items():
        node.create_subscription(getattr(messages,kind),topic,lambda msg,key=key:receive(key,msg),qos_profile_sensor_data)
    def save():
        path=folder/"onboard.tmp"
        path.write_text(json.dumps({"counts":counts,"errors":errors,"tf":frames,"rows":{k:list(v) for k,v in rows.items()}},allow_nan=False))
        path.replace(folder/"onboard.json")
    node.create_timer(.4,save)
    rclpy.spin(node)


def wheel_measurement(snapshot,mission):
    times=mission["paths"]["truth"]
    drive=[r for r in snapshot["rows"]["edge_rover.wheels"] if times["start"][0]<=r[0]<=times["end"][0]]
    assert drive,("no encoder samples during navigation",times["start"][0],times["end"][0])
    peak=max(min(abs(v) for v in r[1:]) for r in drive)
    assert peak>1,("both wheel encoders must turn",peak)
    return {"task_id":mission["task_id"],"samples":len(drive),"both_wheels_peak_rad_s":peak,
            "start_ns":drive[0][0],"end_ns":drive[-1][0]}


def validate(snapshot,wheel,forms):
    rows=snapshot["rows"]
    assert not snapshot["errors"], snapshot["errors"]
    assert all(len(r)>=3 for r in rows.values())
    result={}
    for key,(topic,kind,frame) in TOPICS.items():
        data=rows[key]
        assert all(b[0]>a[0] for a,b in zip(data,data[1:])),key
        if frame:
            assert frame in snapshot["tf"],(key,frame)
        result[key]={"topic":topic,"type":kind,"frame":frame,"messages":snapshot["counts"][key],
                     "first_ns":data[0][0],"last_ns":data[-1][0],"last_value":data[-1][1:]}
    paired={}
    for body in PAIRS:
        color,depth,info=({r[0]:r[1:] for r in rows[body+"."+k]} for k in ("color","depth","info"))
        matches=sorted(color.keys() & depth.keys() & info.keys())
        assert len(matches)>=3,(body,"unpaired frames")
        for t in matches:
            assert color[t][:2]==depth[t][:2]==info[t][:2]==[160,120]
            assert min(info[t][2:])>0
        assert any(color[t][2]>10 for t in matches) and any(depth[t][2]>0 for t in matches),body
        paired[body]={"paired_frames":len(matches),"stamp_ns":matches[-1],"color":color[matches[-1]],
                      "depth":depth[matches[-1]],"intrinsics":info[matches[-1]]}
    assert max(r[3] for r in rows["crazyflie.color"])>12
    assert any(r[2]>=2 and r[3]>30 and r[4]>.2 for r in rows["go1.cloud"]),"degenerate multi-layer point cloud"
    assert max(abs(v) for v in rows["edge_rover.wheels"][-1][1:])<.15,"rover never settled"
    fly=next(t for t in forms["tasks"] if t["evidence"]["robot_id"]=="crazyflie" and t["evidence"]["action"]=="起飞")
    proof=fly["evidence"]
    high=[r for r in rows["crazyflie.down"] if abs(r[0]/1e9-proof["stamp_s"])<.7 and r[3] is not None]
    assert high and any(abs(r[3]-proof["measured"][0])<.18 for r in high),(proof,high)
    floor_change=[r[3] for r in rows["crazyflie.down"] if r[3] is not None]
    assert max(floor_change)-min(floor_change)>.55,"range did not follow flight"
    return {"sources":result,"rgbd_pairs":paired,"transforms":snapshot["tf"],"wheel_motion":wheel,
            "flight_task_id":fly["id"],"altitude_m":proof["measured"][0],"down_range_near_hover_m":min(high,key=lambda r:abs(r[0]/1e9-proof["stamp_s"]))[3],
            "down_range_span_m":[min(floor_change),max(floor_change)]}


if __name__=="__main__":
    record(Path(sys.argv[1]))
