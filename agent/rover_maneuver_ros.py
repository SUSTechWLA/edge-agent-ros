"""DSH rover adapter roles. The executor owns Core admission; the witness has a read-only ROS interface."""
import argparse
from collections import deque
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import socket
import time

from edge_interfaces.srv import CancelMission, GetMission, SubmitMission
from geometry_msgs.msg import Point, TwistStamped
from nav_msgs.msg import Odometry
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import String

from rover_maneuver import finite, relative_intent
from physical_maneuver import approved_site
from soft_client import request_at


@dataclass(frozen=True)
class Sample:
    stamp: int
    wall: float
    pose: tuple
    v: float
    w: float


def sample(msg, frame):
    if msg.header.frame_id != frame:
        return None
    p,q=msg.pose.pose.position,msg.pose.pose.orientation
    tw=msg.twist.twist
    if not all(map(finite,[p.x,p.y,q.x,q.y,q.z,q.w,tw.linear.x,tw.linear.y,tw.angular.z])):
        return None
    if abs(math.hypot(q.x,q.y,q.z,q.w)-1)>.01:
        return None
    yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
    stamp=msg.header.stamp.sec*10**9+msg.header.stamp.nanosec
    return Sample(stamp,time.monotonic(),(p.x,p.y,yaw),math.hypot(tw.linear.x,tw.linear.y),abs(tw.angular.z))


def wait_call(node,client,request):
    if not client.service_is_ready():
        raise RuntimeError("navigation.core_unavailable")
    future=client.call_async(request)
    rclpy.spin_until_future_complete(node,future,timeout_sec=1.5)
    if not future.done():
        client.remove_pending_request(future)
        raise RuntimeError("navigation.core_outcome_unknown")
    return future.result()


class Role(Node):
    def __init__(self,name,site,control_socket):
        super().__init__(name)
        self.cfg=site["limits"]
        self.scope=site["pin"]["sensor"]
        self.topics=self.cfg["ros"]
        keys=["odom","report","witness","submit","query","cancel"]
        if "stop_witness" in self.topics:
            keys.append("stop_witness")
        if any(self.resolve_topic_name(self.topics[k])!=self.topics[k] for k in keys):
            raise ValueError("ROS graph names must match the approved site profile")
        self.sock=control_socket
        self.instance=socket.gethostname()
        self.domain=self.context.get_domain_id()
        if self.domain!=self.scope["ros_domain_id"]:
            raise ValueError("runtime ROS domain differs from the approved deployment")
        self.query=self.create_client(GetMission,self.topics["query"])

    def control(self,action,**payload):
        return request_at(self.sock,action,timeout=1,instance_id=self.instance,**payload)

    def next_job(self):
        return self.control("next",**self.scope)

    def core(self,op):
        return wait_call(self,self.query,GetMission.Request(mission_id=op))


class Executor(Role):
    def __init__(self,site,sock):
        super().__init__("rover_maneuver_adapter",site,sock)
        self.submit=self.create_client(SubmitMission,self.topics["submit"])
        self.cancel=self.create_client(CancelMission,self.topics["cancel"])
        self.odom=None
        self.report=None
        self.report_wall=0
        self.active=None
        self.create_subscription(Odometry,self.topics["odom"],self.on_odom,qos_profile_sensor_data)
        self.create_subscription(String,self.topics["report"],self.on_report,10)

    def on_odom(self,msg):
        item=sample(msg,self.topics["odom_frame"])
        if not item or msg.child_frame_id!=self.topics["base_frame"]:
            return
        if self.odom and item.stamp<=self.odom.stamp:
            if item.stamp<self.odom.stamp and self.active:
                self.active["cancel_reason"]="navigation.odometry_clock_reset"
            return
        if self.active and self.odom:
            self.active["path"]+=math.dist(item.pose[:2],self.odom.pose[:2])
        self.odom=item

    def on_report(self,msg):
        try:
            self.report=json.loads(msg.data)
        except (TypeError,ValueError):
            self.report=None
        self.report_wall=time.monotonic()

    def fresh(self):
        now=time.monotonic()
        return (self.odom and now-self.odom.wall<1 and isinstance(self.report,dict) and now-self.report_wall<1)

    def tick(self):
        if self.active:
            self.advance()
        elif self.fresh() and all(c.service_is_ready() for c in (self.submit,self.query,self.cancel)):
            item=self.next_job()
            if item and item["kind"]=="maneuver":
                self.active={"lease":item["lease"],"intent":None,"prepared":False,"sent":False,
                             "path":0.,"started":time.monotonic(),"cancel_reason":None,"cancel_sent":None}
                self.advance()

    def advance(self):
        job=self.active
        lease=job["lease"]
        op=lease["operation_id"]
        if job.get("pending"):
            action,fields=job["pending"]
            self.control(action,lease=lease,**fields)
            self.active=None
            return
        try:
            # Maintains provider health and observes the original live lease immediately before dispatch.
            self.next_job()
            self.control("check",lease=lease)
            if not job["prepared"]:
                if not self.fresh() or self.odom.v>.025 or self.odom.w>.06:
                    raise ValueError("navigation.initial_observation_unavailable")
                if not job["intent"]:
                    job["intent"]=relative_intent(self.cfg,self.odom.pose,self.odom.stamp,self.report)
                self.control("prepare",lease=lease,intent=job["intent"])
                job["prepared"]=True
            goal=job["intent"]["odom_goal"]
            if goal is None:
                self.finish("NOT_DISPATCHED",0,"clear in the admitted sensor observation")
                return
            if not job["sent"]:
                # One native submit, then query only. A lost response is never permission to redispatch.
                if self.core(op).found:
                    raise RuntimeError("navigation.core_operation_exists")
                self.control("check",lease=lease)
                job["sent"]=True
                reply=wait_call(self,self.submit,SubmitMission.Request(mission_id=op,waypoints=[Point(x=goal[0],y=goal[1])]))
                if not reply.accepted:
                    if self.core(op).found:
                        raise RuntimeError("navigation.core_admission_conflict")
                    self.finish("REJECTED",0,reply.detail)
                return
            result=self.core(op)
            if not result.found:
                raise RuntimeError("navigation.core_outcome_unknown")
            if result.state=="RECOVERY_REQUIRED":
                raise RuntimeError("navigation.core_outcome_unknown")
            if result.state in ("SUCCEEDED","FAILED","BLOCKED","CANCELED"):
                self.finish(result.state,result.verified_count,result.detail)
                return
            if job["path"]>self.cfg["max_path_m"] or time.monotonic()-job["started"]>self.cfg["timeout_s"]:
                job["cancel_reason"]="navigation.maneuver_budget_exhausted"
            if not self.fresh():
                job["cancel_reason"]="navigation.observation_unavailable"
            if job["cancel_reason"] and not job["cancel_sent"]:
                job["cancel_sent"]=time.monotonic()
                wait_call(self,self.cancel,CancelMission.Request(mission_id=op))
            if job["cancel_sent"] and time.monotonic()-job["cancel_sent"]>4:
                raise RuntimeError("navigation.stop_unconfirmed")
        except ValueError as error:
            if not job["prepared"] and str(error).startswith("navigation."):
                job["pending"]=("reject",{"code":str(error)})
            else:
                self.disconnected()
        except Exception:
            self.disconnected()
            raise

    def disconnected(self):
        job=self.active
        job["pending"]=("unknown",{"code":"navigation.maneuver_outcome_unknown"})
        if job["sent"] and not job["cancel_sent"]:
            job["cancel_sent"]=time.monotonic()
            wait_call(self,self.cancel,CancelMission.Request(mission_id=job["lease"]["operation_id"]))

    def finish(self,state,count,detail):
        job=self.active
        op=job["lease"]["operation_id"] if job["sent"] and state!="REJECTED" else None
        receipt={"intent":job["intent"],"core_mission_id":op,"core_operation":op+"/waypoint/1" if op else None,
                 "core_state":state,"verified_count":count,"core_detail":detail[:400],
                 "terminal_stamp_ns":self.odom.stamp,"odom_path_m":job["path"],"cancel_reason":job["cancel_reason"]}
        job["pending"]=("candidate",{"measurement":receipt})


def stationary(rows):
    """Check both velocity samples and pose differences over a contiguous recent window."""
    if len(rows)<4 or rows[-1].stamp-rows[0].stamp<400_000_000:
        return None
    v=max(row.v for row in rows)
    w=max(row.w for row in rows)
    for a,b in zip(rows,rows[1:]):
        dt=(b.stamp-a.stamp)*1e-9
        if not 0<dt<=.25:
            return None
        v=max(v,math.dist(a.pose[:2],b.pose[:2])/dt)
        w=max(w,abs(math.remainder(b.pose[2]-a.pose[2],math.tau))/dt)
    if v>.025 or w>.06:
        return None
    return {"first_ns":rows[0].stamp,"last_ns":rows[-1].stamp,"samples":len(rows),
            "max_linear_mps":v,"max_angular_radps":w}


class Witness(Role):
    def __init__(self,site,sock):
        super().__init__("rover_maneuver_world_witness",site,sock)
        self.history=deque(maxlen=30000)
        self.stop=None
        self.confirmed_stop=None
        self.create_subscription(Odometry,self.topics["witness"],self.observe,qos_profile_sensor_data)
        self.braking=deque(maxlen=3000)
        if self.topics.get("stop_witness"):
            self.create_subscription(TwistStamped,self.topics["stop_witness"],self.observe_brakes,qos_profile_sensor_data)

    def observe(self,msg):
        point=sample(msg,self.topics["witness_frame"])
        if point and (not self.history or point.stamp>self.history[-1].stamp):
            self.history.append(point)

    def observe_brakes(self,msg):
        stamp=msg.header.stamp.sec*10**9+msg.header.stamp.nanosec
        twist=msg.twist
        v=math.hypot(twist.linear.x,twist.linear.y,twist.linear.z)
        w=math.hypot(twist.angular.x,twist.angular.y,twist.angular.z)
        if (msg.header.frame_id==self.topics["stop_frame"] and finite(v) and finite(w) and stamp>0
                and (not self.braking or stamp>self.braking[-1].stamp)):
            self.braking.append(Sample(stamp,time.monotonic(),(0.,0.,0.),v,w))

    def quiet(self,from_ns,last):
        start=max(from_ns,last-650_000_000)
        quiet=stationary([p for p in self.history if p.stamp>=start])
        if quiet and self.topics.get("stop_witness"):
            if not self.braking or time.monotonic()-self.braking[-1].wall>1:
                return None
            measured=stationary([p for p in self.braking if start<=p.stamp<=last])
            if not measured or last-measured["last_ns"]>200_000_000:
                return None
            quiet["braking"]={**measured,"source":self.topics["stop_witness"],"frame_id":self.topics["stop_frame"]}
        return quiet

    def tick(self):
        if not (self.history and time.monotonic()-self.history[-1].wall<1 and self.query.service_is_ready()):
            return
        if self.topics.get("stop_witness") and (not self.braking or time.monotonic()-self.braking[-1].wall>1):
            return
        item=self.next_job()
        if not item:
            return
        last=self.history[-1].stamp
        if item["kind"]=="stop":
            if item["stop_id"]==self.confirmed_stop:
                return
            if not self.stop or self.stop[0]!=item["stop_id"]:
                self.stop=(item["stop_id"],last,time.monotonic())
            window=self.quiet(self.stop[1],last)
            if window and time.monotonic()-self.stop[2]>.45:
                self.control("stopped",proof={**item,**window,"source":self.topics["witness"],
                             "window_ns":window["last_ns"]-window["first_ns"]})
                self.confirmed_stop=item["stop_id"]
            return
        if item["kind"]!="candidate":
            return
        fact=item["candidate"]
        start=fact["intent"]["odom_stamp_ns"]
        sequence=list(self.history)
        initial=min(range(len(sequence)),key=lambda i:abs(sequence[i].stamp-start))
        track=sequence[initial:]
        if (abs(track[0].stamp-start)>150_000_000 or track[0].v>.025 or track[0].w>.06 or
                any(b.stamp-a.stamp>250_000_000 for a,b in zip(track,track[1:]))):
            return
        quiet=self.quiet(fact["terminal_stamp_ns"],last)
        if not quiet:
            return
        op=item["operation_id"]
        status=self.core(op)
        if fact["core_mission_id"] is None:
            if status.found:
                return
        elif not status.found or status.state!=fact["core_state"] or status.verified_count!=fact["verified_count"]:
            return
        proof={**quiet,"source":self.topics["witness"],"world_frame":self.topics["witness_frame"],
               "resource_id":item["pin"]["resource_id"],"operation_id":op,"boot_id":item["lease"]["boot_id"],
               "epoch":item["lease"]["epoch"],"world_initial":list(track[0].pose),"world_final":list(track[-1].pose[:2]),
               "observed_path_m":sum(math.dist(a.pose[:2],b.pose[:2]) for a,b in zip(track,track[1:]))}
        self.control("verify",proof=proof)


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("role",choices=("execute","verify"))
    cli.add_argument("--site",type=Path,required=True)
    cli.add_argument("--profile",default="sim.json")
    cli.add_argument("--socket",required=True)
    args,ros_args=cli.parse_known_args()
    rclpy.init(args=ros_args)
    site=approved_site(args.site,os.environ["EDGE_PHYSICAL_PLATFORM_DIGEST"],int(os.environ["ROS_DOMAIN_ID"]),args.profile)
    node=(Executor if args.role=="execute" else Witness)(site,args.socket)
    next_tick=0
    last_error=None
    try:
        while rclpy.ok():
            rclpy.spin_once(node,timeout_sec=.025)
            if time.monotonic()<next_tick:
                continue
            next_tick=time.monotonic()+.15
            try:
                node.tick()
                last_error=None
            except Exception as error:
                msg=f"{type(error).__name__}: {error}"
                if msg!=last_error:
                    node.get_logger().warning(msg)
                    last_error=msg
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__=="__main__":
    main()
