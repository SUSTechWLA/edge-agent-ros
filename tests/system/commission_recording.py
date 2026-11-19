"""Test-only recorder. Gazebo renders RGB; the observation timeline uses simulation nanoseconds."""
import json
import math
from pathlib import Path
import threading
import time
from xml.etree import ElementTree as ET


def add_camera(world):
    world.append(ET.fromstring('''<model name="commission_recording_camera"><static>true</static>
      <pose>0 -4 4.3 0 0.797 1.5707963268</pose><link name="mount">
      <sensor name="overview" type="camera"><always_on>true</always_on><update_rate>8</update_rate>
      <topic>/commission/overview</topic><camera><horizontal_fov>1.15</horizontal_fov>
      <image><width>768</width><height>512</height><format>R8G8B8</format></image>
      <clip><near>0.05</near><far>20</far></clip></camera></sensor></link></model>'''))
    # Painted metre marks sit below scan height and have no collision geometry.
    floor=world.find("./model[@name='floor']/link")
    for axis in (0,1):
        for i in range(-3,4):
            pos=(i,0) if axis==0 else (0,i)
            size=(.018,6.) if axis==0 else (6.,.018)
            ink=".14 .25 .29 1" if abs(i)<3 else ".9 .55 .16 1"
            floor.append(ET.fromstring(f'''<visual name="metre_{axis}_{i}"><pose>{pos[0]} {pos[1]} .003 0 0 0</pose>
                <geometry><box><size>{size[0]} {size[1]} .002</size></box></geometry>
                <material><diffuse>{ink}</diffuse><ambient>{ink}</ambient></material></visual>'''))
    face=world.find("./model[@name='commission_pillar']/link/visual")
    if face is not None:
        face.append(ET.fromstring("<material><diffuse>.85 .43 .13 1</diffuse><ambient>.65 .29 .06 1</ambient></material>"))


class Recorder:
    def __init__(self,node,output,epoch=None):
        from gz.transport13 import Node
        from gz.msgs10.image_pb2 import Image
        from nav_msgs.msg import OccupancyGrid, Odometry
        from geometry_msgs.msg import Twist
        from rosgraph_msgs.msg import Clock
        from std_msgs.msg import String
        from rclpy.qos import qos_profile_sensor_data, QoSProfile, DurabilityPolicy
        from scene.campus_large.capture import encode

        self.out=output
        self.root=output/"media"/(epoch or ("cancel" if (output/"media"/"explore").exists() else "explore"))
        self.root.mkdir(parents=True)
        (self.root/"rgb").mkdir()
        (self.root/"grid").mkdir()
        self.timeline=(self.root/"timeline.jsonl").open("x",encoding="utf-8",buffering=1)
        self.lock=threading.Lock()
        self.clock=0
        self.end_stamp=0
        self.seen=set()
        self.last_clock_written=0

        def ros_stamp(msg):
            return msg.header.stamp.sec*10**9+msg.header.stamp.nanosec
        def rgb(msg):
            if self.done():
                return
            try:
                png,meta=encode(msg)
            except ValueError:
                return
            ns=meta["stamp_ns"]
            file=f"rgb/{ns:015d}.png"
            (self.root/file).write_bytes(png)
            self.emit("rgb",ns,file=file,width=meta["width"],height=meta["height"],sha256=meta["sha256"])
        def clock(msg):
            self.clock=msg.clock.sec*10**9+msg.clock.nanosec
            if self.clock-self.last_clock_written>=250000000:
                self.last_clock_written=self.clock
                self.emit("clock",self.clock)
        def pose(kind,msg):
            p=msg.pose.pose.position
            q=msg.pose.pose.orientation
            yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
            self.emit(kind,ros_stamp(msg),xy=[p.x,p.y],yaw=yaw,frame=msg.header.frame_id)
        def grid(msg):
            if self.done():
                return
            o=msg.info.origin
            ns=ros_stamp(msg)
            file=f"grid/{ns:015d}.json"
            path=self.root/file
            if path.exists() or ns<=0:
                return
            cells=list(msg.data)
            body=dict(frame=msg.header.frame_id,width=msg.info.width,height=msg.info.height,
                    resolution=msg.info.resolution,origin=[o.position.x,o.position.y,2*math.atan2(o.orientation.z,o.orientation.w)],
                    cells=cells)
            path.write_text(json.dumps(body,separators=(",",":")),encoding="utf-8")
            self.emit("map",ns,file=file,frame=body["frame"],known=sum(v>=0 for v in cells))
        def bus(msg):
            try:
                event=json.loads(msg.data)
                self.emit("bus",self.clock,event=event)
            except ValueError:
                return
        self.gz=Node()
        if not self.gz.subscribe(Image,"/commission/overview",rgb):
            raise RuntimeError("Gazebo RGB subscription failed")
        node.create_subscription(Clock,"/clock",clock,qos_profile_sensor_data)
        node.create_subscription(Odometry,"/ground_truth",lambda msg:pose("world_pose",msg),qos_profile_sensor_data)
        node.create_subscription(Odometry,"/edge/perception/pose",lambda msg:pose("fused_pose",msg),qos_profile_sensor_data)
        node.create_subscription(Twist,"/cmd_vel",lambda msg:self.emit("twist",self.clock,linear=msg.linear.x,
                                 angular=msg.angular.z),qos_profile_sensor_data)
        node.create_subscription(OccupancyGrid,"/edge/perception/map",grid,
                                  QoSProfile(depth=1,durability=DurabilityPolicy.TRANSIENT_LOCAL))
        node.create_subscription(String,"/edge/events",bus,qos_profile_sensor_data)
        node.create_timer(.1,self.missions)

    def done(self):
        return self.end_stamp and self.clock>self.end_stamp+1800000000

    def emit(self,kind,stamp,**data):
        if stamp<=0 or self.done():
            return
        line=json.dumps(dict(kind=kind,stamp_ns=stamp,wall_ns=time.time_ns(),monotonic_ns=time.monotonic_ns(),**data),
                        separators=(",",":"),allow_nan=False)
        with self.lock:
            self.timeline.write(line+"\n")

    def missions(self):
        if self.done() or self.clock<=0:
            return
        for path in self.out.glob(self.root.name+"-*/report.json"):
            try:
                report=json.loads(path.read_text(encoding="utf-8"))
            except (OSError,ValueError):
                continue
            def once(phase,identifier,**data):
                key=(phase,identifier)
                if key not in self.seen:
                    self.seen.add(key)
                    self.emit("mission",self.clock,phase=phase,run_id=report["run_id"],mission_id=identifier,**data)
            for step in report["steps"]:
                mid=step["mission_id"]
                once("requested",mid,goal=step["goal"],pose_before=step["pose_before"],map_stamp_ns=step["map_stamp_ns"])
                if step.get("accepted"):
                    once("accepted",mid,goal=step["goal"])
                if "core_state" in step:
                    once("receipt",mid,goal=step["goal"],core_state=step["core_state"],measured_xy=step["measured_xy"])
            if report["state"] not in ("STARTING","RUNNING"):
                once("finished",report["run_id"],core_state=report["state"],reason=report["reason"],cancel=report.get("cancel"))
                self.end_stamp=self.end_stamp or self.clock
