"""Inspect an idle rover's planar scan, odometry and timestamped TF. Never opens a motion client."""
import argparse
import json
import math
import time


def scan_coverage(scan):
    n, step = len(scan.ranges), scan.angle_increment
    if (not 2 <= n <= 4096 or not 0 < step <= .12 or
            not all(map(math.isfinite,(scan.angle_min,scan.angle_max,scan.range_min,scan.range_max))) or
            not 0 <= scan.range_min < scan.range_max):
        return False
    span = scan.angle_max - scan.angle_min
    return (abs(span-(n-1)*step) <= .5*step and math.tau-1.6*step <= span <= math.tau+.5*step and
            all(r == math.inf or scan.range_min <= r <= scan.range_max for r in scan.ranges))


def collect(a):
    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.time import Time
    from sensor_msgs.msg import LaserScan
    from tf2_ros import Buffer, TransformException, TransformListener

    rclpy.init()
    node=rclpy.create_node("rover_input_probe")
    buf=Buffer()
    listener=TransformListener(buf,node)
    latest={}
    stamps={"scan":[], "odom":[]}
    gap={"scan":0., "odom":0.}
    seen_at={}
    issues=set()
    def receive(name,msg):
        now=time.monotonic()
        stamp=Time.from_msg(msg.header.stamp).nanoseconds
        age=(node.get_clock().now().nanoseconds-stamp)*1e-9
        if not 0 < stamp or not -.05 <= age <= .25:
            issues.add(name+".clock_or_age")
        if stamps[name]:
            delta=(stamp-stamps[name][-1])*1e-9
            gap[name]=max(gap[name],delta,now-seen_at[name])
            if delta<=0:
                issues.add(name+".nonmonotonic")
        stamps[name].append(stamp)
        seen_at[name]=now
        latest[name]=msg
        if name=="scan":
            if msg.header.frame_id!=a.scan_frame or not scan_coverage(msg):
                issues.add("scan.frame_or_full_sweep")
        else:
            t=msg.twist.twist
            if msg.header.frame_id!=a.odom_frame or msg.child_frame_id!=a.base_frame:
                issues.add("odom.frames")
            if not (math.hypot(t.linear.x,t.linear.y) <= .02 and abs(t.angular.z) <= .04):
                issues.add("odom.not_stationary")

    node.create_subscription(LaserScan,a.scan,lambda m:receive("scan",m),qos_profile_sensor_data)
    node.create_subscription(Odometry,a.odom,lambda m:receive("odom",m),qos_profile_sensor_data)
    end=time.monotonic()+a.seconds
    extrinsics=None
    transform_stamps=set()
    try:
        while time.monotonic()<end:
            rclpy.spin_once(node,timeout_sec=.05)
            if "scan" not in latest:
                continue
            scan=latest["scan"]
            when=Time.from_msg(scan.header.stamp)
            if when.nanoseconds in transform_stamps:
                continue
            try:
                t=buf.lookup_transform(a.base_frame,scan.header.frame_id,when).transform
                buf.lookup_transform(a.odom_frame,a.base_frame,when)
            except TransformException:
                continue
            q=t.rotation
            yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
            vertical=1-2*(q.x*q.x+q.y*q.y)
            extrinsics={"x_m":t.translation.x,"y_m":t.translation.y,"z_m":t.translation.z,"yaw_rad":yaw,
                        "tilt_rad":math.acos(max(-1.,min(1.,vertical)))}
            if (not all(map(math.isfinite,extrinsics.values())) or extrinsics["tilt_rad"]>a.angle_tolerance or
                    math.hypot(t.translation.x-a.scan_xy_yaw[0],t.translation.y-a.scan_xy_yaw[1])>a.xy_tolerance or
                    abs(math.remainder(yaw-a.scan_xy_yaw[2],math.tau))>a.angle_tolerance):
                issues.add("tf.differs_from_site_calibration")
            transform_stamps.add(when.nanoseconds)
        for name,max_gap in (("scan",.25),("odom",.20)):
            if (len(stamps[name])<3 or gap[name]>max_gap or
                    time.monotonic()-seen_at.get(name,-float("inf"))>max_gap):
                issues.add(name+".coverage_gap")
        covered=sorted(transform_stamps)
        if (len(covered)<3 or not stamps["scan"] or stamps["scan"][-1]-covered[-1]>200_000_000 or
                any(b-a>250_000_000 for a,b in zip(covered,covered[1:]))):
            issues.add("tf.missing_at_scan_stamp")
        writers=[i.node_namespace.rstrip("/")+"/"+i.node_name for i in node.get_publishers_info_by_topic(a.command)]
        return {"input_checks_passed":not issues,"issues":sorted(issues),"sample_count":{k:len(v) for k,v in stamps.items()},
                "largest_interval_s":gap,"scan_topic":a.scan,"odom_topic":a.odom,"base_frame":a.base_frame,
                "observed_scan_extrinsics":extrinsics,"tf_stamps":len(transform_stamps),
                "command_topic":a.command,"visible_command_publishers":sorted(writers),"motion_authorized":False}
    finally:
        listener.unregister()
        node.destroy_node()
        rclpy.shutdown()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan",default="/scan")
    parser.add_argument("--odom",default="/odom")
    parser.add_argument("--command",default="/cmd_vel")
    parser.add_argument("--base-frame",default="base_link")
    parser.add_argument("--odom-frame",default="odom")
    parser.add_argument("--scan-frame",required=True)
    parser.add_argument("--scan-xy-yaw",nargs=3,type=float,required=True,metavar=("X_M","Y_M","YAW_RAD"))
    parser.add_argument("--xy-tolerance",type=float,default=.01)
    parser.add_argument("--angle-tolerance",type=float,default=math.radians(1))
    parser.add_argument("--seconds",type=float,default=6)
    args=parser.parse_args()
    if not (1<=args.seconds<=120 and 0<args.xy_tolerance<=.1 and 0<args.angle_tolerance<=.2 and
            all(map(math.isfinite,args.scan_xy_yaw))):
        parser.error("invalid duration, tolerance or measured extrinsics")
    result=collect(args)
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    raise SystemExit(0 if result["input_checks_passed"] else 1)
