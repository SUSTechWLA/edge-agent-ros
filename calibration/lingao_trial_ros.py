"""Site-local entry point for a bounded Lingao MCU-witnessed commissioning operation."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import stat
import time
import uuid

from calibration.lingao_short_trial import Claims, MAX_AGE, MAX_DRIVE, Measurement, Trial, VendorStraightLine, VENDOR_SHA256


class Permit:
    """Consumed from the site interlock, never from an Agent task or preflight report."""
    def __init__(self, path, robot, op, direction,owner_uid):
        self.path=path
        self.binding=(robot,op,direction)
        self.frozen_deadline=None
        self.reason="permit_unavailable"
        self.owner_uid=owner_uid
        self.boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def allowed(self, now, moving):
        try:
            fd=os.open(self.path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
            with os.fdopen(fd,"rb",buffering=0) as stream:
                attrs=os.fstat(fd)
                if not stat.S_ISREG(attrs.st_mode) or attrs.st_uid!=self.owner_uid or attrs.st_mode&0o022 or attrs.st_size>8192:
                    raise ValueError()
                data=json.loads(stream.read(8192))
            if (data["schema"]!="edge.lingao.directional-lease.v1" or
                    data["boot_id"]!=self.boot or
                    (data["robot_id"],data["operation_id"],data["direction"])!=self.binding or
                    data["rear_or_front_stop_verified"] is not True or data["clear"] is not True or
                    data["takeover_active"] is not False or data["estop_active"] is not False or
                    data["watchdog_armed"] is not True):
                raise ValueError()
            sampled,deadline=data["observed_monotonic_ns"],data["hard_stop_monotonic_ns"]
            if (type(sampled) is not int or type(deadline) is not int or
                    not 0<=now-sampled<=MAX_AGE*1e9 or not 0<deadline-now<=MAX_DRIVE*1e9 or
                    self.frozen_deadline not in (None,deadline)):
                raise ValueError()
            if moving and self.frozen_deadline is None:
                if deadline-now<.40e9:raise ValueError()
                self.frozen_deadline=deadline
            self.reason=None
            return True
        except (OSError,KeyError,TypeError,ValueError):
            self.reason="directional_interlock_unavailable"
            return False


def run(robot,op,direction,vendor,shadow_root=None):
    import rclpy
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data
    shadow=shadow_root is not None
    if str(uuid.UUID(op))!=op: raise ValueError("invalid_operation")
    # The real runtime owns both paths. No tool argument can select an actuator or live permit.
    state=Path(shadow_root) if shadow else Path("/var/lib/edge-body/lingao-commissioning")
    permit_dir=state if shadow else Path("/run/edge-body/lingao-commissioning")
    if not shadow:
        if os.environ.get("ROS_DOMAIN_ID")!="61" or os.environ.get("ROS_LOCALHOST_ONLY")!="1":
            raise ValueError("uncommissioned_hardware_DDS")
        for p in (permit_dir,*permit_dir.parents,state,*state.parents):
            attrs=p.lstat()
            expected=os.geteuid() if p==state else 0
            if not stat.S_ISDIR(attrs.st_mode) or attrs.st_uid!=expected or attrs.st_mode&0o022:
                raise ValueError("uncommissioned_runtime_directory")
    prefix="/edge/commissioning/shadow_"+op.replace("-","") if shadow else ""
    topic,odom=prefix+"/cmd_vel",prefix+"/lingao/odom"
    ledger=Claims(state/"operations.sqlite3")
    candidate=VendorStraightLine(vendor,-.01 if direction=="reverse" else .01)
    trial=Trial(direction,candidate.candidate)
    permit=Permit(permit_dir/(op+".json"),robot,op,direction,os.geteuid() if shadow else 0)
    ledger.claim(op,robot)
    rclpy.init(args=[])
    node=rclpy.create_node("lingao_commissioning_trial",use_global_arguments=False)
    pub=None
    interrupted=False
    def interrupt(*_):
        nonlocal interrupted
        interrupted=True
    signal.signal(signal.SIGTERM,interrupt)
    signal.signal(signal.SIGINT,interrupt)
    frames=None
    def observe(m):
        nonlocal frames
        now=time.monotonic()
        ts=m.header.stamp.sec*10**9+m.header.stamp.nanosec
        t,p,q=m.twist.twist,m.pose.pose.position,m.pose.pose.orientation
        values=[p.x,p.y,q.x,q.y,q.z,q.w,t.linear.x,t.linear.y,t.angular.z]
        bound=(m.header.frame_id,m.child_frame_id)
        source_now=time.time_ns() if not shadow else node.get_clock().now().nanoseconds
        if (not all(map(math.isfinite,values)) or bound!=("odom","base_foot") or frames not in (None,bound)
                or not .99<=sum(v*v for v in [q.x,q.y,q.z,q.w])<=1.01
                or not 0<=source_now-ts<=MAX_AGE*1e9):
            trial.stop(now,"invalid_odometry");return
        frames=bound
        yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
        trial.observe(Measurement(ts,now,p.x,p.y,yaw,t.linear.x,t.linear.y,t.angular.z))
    node.create_subscription(Odometry,odom,observe,qos_profile_sensor_data)
    sample_owner="shadow_mcu" if shadow else "lingao_base_driver"
    mux_owner="shadow_mux" if shadow else "vehicle_mode_mux"
    def owners(t,pub):
        f=node.get_publishers_info_by_topic if pub else node.get_subscriptions_info_by_topic
        return [e.node_namespace.rstrip("/")+"/"+e.node_name for e in f(t)]
    def valid_route():
        sinks=owners(topic,False)
        if (owners(odom,True)!=["/"+sample_owner] or sinks.count("/"+mux_owner)!=1 or
                set(sinks)-{"/"+mux_owner,"/control_history_observer"}):
            return False
        if shadow:return True
        return (not node.get_parameter("use_sim_time").value and not node.get_clock().ros_time_is_active and
                owners("/mode_mux/selected_cmd_vel",True)==["/"+mux_owner] and
                "/vehicle_web_remote" in owners("/mode_mux/selected_cmd_vel",False) and
                owners("/cmd_vel_safe",True)==["/vehicle_web_remote"] and
                owners("/cmd_vel_safe",False).count("/lingao_base_driver")==1)
    begin=time.monotonic()
    last_send=0.
    stop_sent=False
    stop_attempted=False
    try:
        while time.monotonic()-begin<10:
            rclpy.spin_once(node,timeout_sec=.015)
            now=time.monotonic()
            if interrupted: trial.stop(now,"interrupted")
            if trial.stop_time is not None:
                if trial.sent and not stop_sent and not stop_attempted:
                    stop_attempted=True
                    if valid_route() and permit.allowed(time.monotonic_ns(),True):
                        pub.publish(Twist())
                        stop_sent=True
                if (trial.start is None or trial.stopped(now) or now-trial.stop_time>3): break
                continue
            if trial.start is None and now-begin>5:
                trial.stop(now,permit.reason or "initial_baseline_or_route_unavailable");break
            if now-last_send<.025: continue
            last_send=now
            route_ok=valid_route()
            if route_ok and pub is None:
                pub=node.create_publisher(Twist,topic,10)
            matched=route_ok and pub.get_subscription_count()>0
            allowed=matched and permit.allowed(time.monotonic_ns(),trial.start is not None or trial.stopped(now))
            if (trial.sent and allowed and permit.frozen_deadline-time.monotonic_ns()<.030e9):
                trial.stop(now)
                pub.publish(Twist())
                stop_sent=True
                continue
            value=trial.tick(now,allowed)
            if value is not None and allowed:
                msg=Twist();msg.linear.x=value;pub.publish(msg)
                stop_sent=(value==0.)
        result=trial.receipt(time.monotonic())
        schema="edge.lingao.relative-trial.shadow.v1" if shadow else "edge.lingao.relative-trial.v1"
        result.update(schema=schema,operation_id=op,robot_id=robot,direction=direction,
                      environment="shadow" if shadow else "hardware",requested_distance_m=trial.sign*.01,
                      odometry_topic=odom,velocity_topic=topic,source_sha256=VENDOR_SHA256,
                      hard_stop_monotonic_ns=permit.frozen_deadline,boot_id=permit.boot,
                      commanded_stop_while_owner=stop_sent)
        ledger.finish(op,result)
        return result
    finally:
        # No competing writes after manual takeover or expiry. The site watchdog owns stopping.
        node.destroy_node()
        if rclpy.ok():rclpy.shutdown()
        ledger.db.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--operation-id",required=True)
    p.add_argument("--robot-id",required=True)
    p.add_argument("--direction",required=True,choices=("forward","reverse"))
    p.add_argument("--shadow-root",type=Path,help="Private fixture state and an isolated ROS topic prefix")
    p.add_argument("--vendor-file",type=Path,default=Path("/home/cas/humble_ws/install/lingao_base/lib/lingao_base/move_relative.py"))
    args=p.parse_args()
    result=run(args.robot_id,args.operation_id,args.direction,args.vendor_file,args.shadow_root)
    print(json.dumps(result,allow_nan=False))
    return 0 if result["state"]=="VERIFIED" else 1


if __name__=="__main__":
    raise SystemExit(main())
