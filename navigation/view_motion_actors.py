"""Separate velocity arbiter, odometry executor and independent world-pose witness for a local deployment."""
from collections import deque
import json
import math
import os
from pathlib import Path
import signal
import threading
import time
import uuid

from edge_contracts.soft_transport import SoftError, request_at
from edge_contracts.spatial_facts import rotation
from navigation.planar_pose import wrap_yaw
from navigation import target_tracking
from navigation.view_motion_site import CONTROL, SENSORS, absolute_goal, deploy_profile, guard_site, reverse_limit
from navigation.wheel_gyro_odometry import WheelGyroOdometry

# Set by SIGTERM/SIGINT; loops exit and their finally blocks still publish zero velocity on a live context.
STOPPING = threading.Event()


def pose(p):
    q = p.orientation
    return (p.position.x, p.position.y, math.atan2(2*(q.w*q.z+q.x*q.y), 1-2*(q.y*q.y+q.z*q.z)))


def stamp(h):
    return h.sec*1_000_000_000 + (h.nanosec if hasattr(h,"nanosec") else h.nsec)


class Poses:
    def __init__(self):
        self.rows = deque(maxlen=16000)

    def put(self, ns, xyz):
        if ns <= 0 or not all(math.isfinite(n) for n in xyz):
            return
        if self.rows and ns < self.rows[-1][0]:
            self.rows.clear()
        if not self.rows or ns != self.rows[-1][0]:
            self.rows.append((ns,time.monotonic_ns(),xyz))

    def anchor(self, ns):
        rows = list(self.rows)
        a = next((r for r in reversed(rows) if r[0] <= ns),None)
        b = next((r for r in rows if r[0] >= ns),None)
        if not a or not b or b[0]-a[0] > 400_000_000:
            raise ValueError("source_pose_window_unavailable")
        if math.dist(a[2][:2],b[2][:2]) > .018 or abs(wrap_yaw(a[2][2]-b[2][2])) > .025:
            raise ValueError("source_pose_not_stationary")
        return a[2]

    def fresh(self):
        return bool(self.rows and time.monotonic_ns()-self.rows[-1][1] < 1_500_000_000)

    def stationary(self, after_ns):
        quiet_ns = round(CONTROL["stationary_seconds"]*1e9)
        if not self.fresh():
            return None
        rows = list(self.rows)
        last = rows[-1]
        start = next((i for i in range(len(rows)-2,-1,-1) if last[1]-rows[i][1] >= quiet_ns
                      and last[0]-rows[i][0] >= quiet_ns),None)
        if start is None or rows[start][1] <= after_ns:
            return None
        data = rows[start:]
        speeds = []
        turns = []
        for a,b in zip(data,data[1:]):
            dt = (b[0]-a[0])*1e-9
            speeds.append(math.dist(a[2][:2],b[2][:2])/dt)
            turns.append(abs(wrap_yaw(a[2][2]-b[2][2]))/dt)
        if max(speeds) >= .025 or max(turns) >= .05:
            return None
        return dict(first_ns=data[0][1],last_ns=data[-1][1],sample_count=len(data),
                    sensor_stamp_ns=data[-1][0],sensor_clock_id="gazebo_sim",
                    max_linear_mps=max(speeds),max_angular_radps=max(turns),stop_pose=list(data[-1][2]))


def command_to_goal(current, goal, stage, *, reverse_mps=0.):
    x,y,yaw = current
    dx,dy = goal[0]-x,goal[1]-y
    distance = math.hypot(dx,dy)
    heading = wrap_yaw(math.atan2(dy,dx)-yaw + (math.pi if reverse_mps else 0.))
    if stage != "final" and distance <= .030:
        stage = "final"
    if stage == "pivot" and abs(heading) < .06:
        stage = "drive"
    if stage == "drive" and abs(heading) > .35:
        stage = "pivot"
    err = wrap_yaw(goal[2]-yaw) if stage == "final" else heading
    if stage == "final" and abs(err) < .016:
        return 0.,0.,"arrived"
    limits = CONTROL["command_limits"]
    omega = max(-limits["yaw_radps"],min(limits["yaw_radps"],1.6*err))
    speed = -min(reverse_mps,.75*distance) if reverse_mps else min(limits["forward_mps"],.75*distance)
    return speed if stage == "drive" else 0., omega, stage


def scan_clear(scan, v, w):
    if scan is None or len(scan.ranges) < 90 or abs(scan.angle_increment)*(len(scan.ranges)-1) < 6.15:
        return False
    for i,distance in enumerate(scan.ranges):
        if math.isnan(distance) or distance <= 0 or distance < scan.range_min:
            return False
        if not math.isfinite(distance):
            continue
        angle = scan.angle_min+i*scan.angle_increment
        x,y = distance*math.cos(angle),distance*math.sin(angle)
        if abs(w) > .001 and distance < .287+.085:
            return False
        if v > 0 and -.234 < x < .234+.10+.55*v and abs(y) < .166+.085:
            return False
        if v < 0 and -.234-.10-.55*abs(v) < x < .234 and abs(y) < .166+.085:
            return False
    return True


def run_guard():
    import rclpy
    from geometry_msgs.msg import Twist
    from sensor_msgs.msg import LaserScan
    from std_msgs.msg import String
    from gz.transport13 import Node
    from gz.msgs10.twist_pb2 import Twist as GazeboTwist
    from rclpy.qos import qos_profile_sensor_data
    node = rclpy.create_node("fixed_body_arbiter")
    gz = Node()
    motor = gz.advertise("/model/lingao/cmd_vel",GazeboTwist)
    selected = node.create_publisher(Twist,CONTROL["selected"]["topic"],1)
    safe = node.create_publisher(Twist,CONTROL["safe"]["topic"],1)
    status = node.create_publisher(String,"/fixed/motion_guard",1)
    latch = Path("/guard-state/software_stop")
    reverse_mps = reverse_limit(json.loads(SENSORS.read_bytes()).get("view_tracking",{}))
    last = {"software_stop":latch.exists()}
    def receive(key, data):
        last[key] = (time.monotonic(),data)
    def stop(msg):
        if msg.data.strip().lower() == CONTROL["stop"]["latch_value"]:
            last["software_stop"] = True
            latch.touch(exist_ok=True)
    node.create_subscription(Twist,CONTROL["command"]["topic"],lambda x:receive("cmd",x),1)
    node.create_subscription(String,CONTROL["stop"]["topic"],stop,10)
    node.create_subscription(LaserScan,"/fixed/perimeter_scan",lambda x:receive("scan",x),qos_profile_sensor_data)
    def relay(v,w):
        msg=Twist()
        msg.linear.x,msg.angular.z = float(v),float(w)
        safe.publish(msg)
        out = GazeboTwist()
        out.linear.x,out.angular.z = float(v),float(w)
        motor.publish(out)
    try:
        while rclpy.ok() and not STOPPING.is_set():
            rclpy.spin_once(node,timeout_sec=.019)
            now = time.monotonic()
            cmd_ns, cmd = last.get("cmd",(0.,None))
            scan_ns,scan = last.get("scan",(0.,None))
            writers = node.get_publishers_info_by_topic(CONTROL["command"]["topic"])
            ready = (not last.get("software_stop") and len(writers)==1
                     and writers[0].node_name == "fixed_view_executor" and now-scan_ns < 1.2)
            v,w = (cmd.linear.x,cmd.angular.z) if cmd and now-cmd_ns < .35 else (0.,0.)
            limits = CONTROL["command_limits"]
            if not ready or not math.isfinite(v+w) or not -reverse_mps <= v <= limits["forward_mps"] or abs(w) > limits["yaw_radps"]:
                v,w=0.,0.
            proposal = Twist()
            proposal.linear.x,proposal.angular.z = v,w
            selected.publish(proposal)
            clear = scan_clear(scan,v,w)
            relay(v if clear else 0., w if clear else 0.)
            msg = String()
            msg.data = "ready" if ready and clear else "blocked"
            status.publish(msg)
            time.sleep(.035)
    finally:
        for _ in range(4):
            relay(0.,0.)
            time.sleep(.03)
        node.destroy_node()


def run(role):
    import rclpy
    from rclpy.signals import SignalHandlerOptions
    guard_site()
    # rclpy's own SIGTERM handler invalidates the context before finally can publish the zero command.
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOPPING.set())
    if role == "guard":
        try:
            return run_guard()
        finally:
            rclpy.try_shutdown()  # SIGTERM may already have shut the context down
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from std_msgs.msg import String
    from rclpy.qos import qos_profile_sensor_data
    guard, vision = {}, {}
    node = rclpy.create_node("fixed_view_" + role)
    poses = Poses()
    if role == "executor":
        from sensor_msgs.msg import Imu
        sensors=json.loads(SENSORS.read_bytes())
        tracking = sensors.get("view_tracking")
        reverse_mps = reverse_limit(tracking or {})
        imu=sensors["imu"]
        mount=sensors["extrinsics"]["imu"]
        if (mount["parent_frame"]!=sensors["base_frame"] or mount["child_frame"]!=imu["frame"]
                or mount["serial"]!=imu["serial"] or mount["robot_id"]!=sensors["robot_id"] or not mount["reference"]):
            raise ValueError("uncommissioned_imu_mount")
        gyro_z=rotation(mount["translation_m"],mount["rotation_xyzw"])[2][:3]
        origin = uuid.uuid4().hex
        pose_pub = node.create_publisher(String,"/fixed/capture/boot_pose",qos_profile_sensor_data)
        def publish_pose(ns, xyz):
            poses.put(ns,xyz)
            x,y,yaw = xyz
            pose_pub.publish(String(data=json.dumps(dict(frame="boot_odom",epoch=f"{origin}:{fusion.epoch}",
                stamp_ns=ns,position_m=[x,y,0.],orientation_xyzw=[0.,0.,math.sin(yaw/2),math.cos(yaw/2)],
                yaw_rad=yaw))))
        def reset_observations():
            poses.rows.clear()
            vision.clear()
        fusion=WheelGyroOdometry(publish_pose,reset_observations)
        pub=node.create_publisher(Twist,CONTROL["command"]["topic"],1)
        def odom(msg):
            if (msg.header.frame_id == CONTROL["odometry"]["parent_frame"]
                    and msg.child_frame_id == CONTROL["odometry"]["child_frame"]):
                fusion.push("wheel",stamp(msg.header.stamp),msg.twist.twist.linear.x)
        def gyro(msg):
            if msg.header.frame_id==imu["frame"] and msg.angular_velocity_covariance[0] >= 0:
                v=msg.angular_velocity
                fusion.push("gyro",stamp(msg.header.stamp),sum(c*w for c,w in zip(gyro_z,(v.x,v.y,v.z))))
        def readiness(msg):
            guard.update(state=msg.data,received=time.monotonic())
        node.create_subscription(Odometry,CONTROL["odometry"]["topic"],odom,qos_profile_sensor_data)
        node.create_subscription(Imu,imu["topic"],gyro,qos_profile_sensor_data)
        node.create_subscription(String,"/fixed/motion_guard",readiness,1)
        if tracking:
            def see(msg):
                try:
                    writers = node.get_publishers_info_by_topic(tracking["topic"])
                    if len(writers) != 1 or writers[0].node_name != "edge_target_track":
                        return
                    row = target_tracking.observation(msg.data, sensors["camera"]["frame"], tracking["target"])
                    prev = vision.get("stamp_ns", 0)
                    if row["stamp_ns"] > prev or row["stamp_ns"] + 1_000_000_000 < prev:
                        vision.update(row=row, stamp_ns=row["stamp_ns"], receipt=time.monotonic())
                except (ValueError, TypeError):
                    return
            node.create_subscription(String,tracking["topic"],see,qos_profile_sensor_data)
        def speed(v,w):
            val=Twist()
            val.linear.x,val.angular.z=float(v),float(w)
            pub.publish(val)
    else:
        from gz.transport13 import Node as Gazebo
        from gz.msgs10.pose_v_pb2 import Pose_V
        gz=Gazebo()
        def world(msg):
            p=next((p for p in msg.pose if p.name=="lingao"),None)
            if p is not None:
                poses.put(stamp(msg.header.stamp),pose(p))
        if not gz.subscribe(Pose_V,"/world/fixed_calibration/dynamic_pose/info",world):
            raise RuntimeError("world witness unavailable")
    site=deploy_profile()
    instance = role+"-"+uuid.uuid4().hex
    def request(action,**fields):
        return request_at("/run/view/physical.sock",action,instance_id=instance,**fields)
    active, last_poll, start_ns, attach_deadline = None, 0., 0, None
    spinner=threading.Thread(target=rclpy.spin,args=(node,),daemon=True)
    spinner.start()
    try:
        while rclpy.ok() and not STOPPING.is_set():
            now = time.monotonic()
            if active is None and now-last_poll > .18 and poses.fresh():
                last_poll=now
                if not os.path.exists("/run/view/physical.sock"):
                    continue
                try:
                    work = request("next",binding=site["pin"]["binding"])
                    attach_deadline = None
                except SoftError as exc:
                    # After a restart the controller hands over this idle slot only once the earlier process
                    # has been silent for a health window. Wait for that bounded handover, then fail visibly.
                    if exc.code != "plugin.fence_rejected" or str(exc) != "different process incarnation":
                        raise
                    attach_deadline = attach_deadline or now + 2*site["health_ttl_ms"]/1000 + 5
                    if now > attach_deadline:
                        raise
                    time.sleep(.5)
                    continue
                except (OSError, RuntimeError) as exc:
                    if not os.path.exists("/run/view/physical.sock"):
                        time.sleep(.2)
                        continue
                    raise
                if work and work["kind"] in ("execute","candidate"):
                    active=work
                    stage="pivot"
                    start_ns=time.monotonic_ns()
                    print(role,work["operation_id"],work["kind"],flush=True)
            if active:
                lease, goal = active["lease"], active["goal"]
                try:
                    if now-start_ns/1e9 > 395 or not poses.fresh():
                        raise ValueError("motion_observation_or_time_budget")
                    anchor = poses.anchor(goal["stamp_ns"])
                    target = absolute_goal(anchor, goal["translation_heading"])
                    if role=="executor":
                        if active.setdefault("localization_epoch",fusion.epoch) != fusion.epoch:
                            raise ValueError("source_localization_epoch_changed")
                        if goal.get("origin_epoch", f"{origin}:{fusion.epoch}") != f"{origin}:{fusion.epoch}":
                            raise ValueError("source_boot_origin_changed")
                        request("check",lease=lease)
                        if stage=="pivot" and "checked_source" not in active:
                            current = poses.rows[-1][2]
                            if math.dist(current[:2],anchor[:2]) > .035 or abs(wrap_yaw(current[2]-anchor[2])) > .035:
                                raise ValueError("source_pose_has_changed")
                            active["checked_source"]=True
                        if now-guard.get("received",0)>1.2 or guard.get("state") != "ready":
                            raise ValueError("independent_clearance_unavailable")
                        if stage!="arrived":
                            row = vision.get("row")
                            if tracking and (not target_tracking.ready(row, now-vision.get("receipt",0.),
                                                                      poses.rows[-1][0], goal["stamp_ns"])
                                             or active.get("tracking_provider", row["provider"]) != row["provider"]
                                             or min(*row["box"][:2], 1-row["box"][2], 1-row["box"][3]) < .018):
                                speed(0.,0.)
                                if now-active.setdefault("tracking_paused",now) > 3.:
                                    raise ValueError("target_tracking_lost")
                                time.sleep(.045)
                                continue
                            active.pop("tracking_paused",None)
                            v,w,stage=command_to_goal(poses.rows[-1][2], target, stage,
                                                      reverse_mps=reverse_mps if goal.get("reverse") else 0.)
                            if tracking:
                                active.setdefault("tracking_provider",row["provider"])
                                v,w=target_tracking.tracked_command(v,w,stage,row["box"],CONTROL["command_limits"]["yaw_radps"])
                            speed(v,w)
                        else:
                            speed(0.,0.)
                            current = poses.rows[-1][2]
                            if math.dist(current[:2],target[:2]) > .05:
                                stage="pivot"
                            elif abs(wrap_yaw(current[2]-target[2])) > .03:
                                stage="final"
                            elif poses.stationary(start_ns):
                                request("candidate",lease=lease,measurement={"goal_ref":goal["goal_ref"],"outcome":"arrived"})
                                active=None
                    else:
                        quiet = poses.stationary(active["candidate"]["observe_after_ns"])
                        if quiet:
                            request("verify",proof={**quiet,"start_pose":list(anchor),"goal_ref":goal["goal_ref"],
                                    "operation_id":lease["operation_id"],"boot_id":lease["boot_id"],"epoch":lease["epoch"],
                                    "frame_id":"world","axes":CONTROL["axes"],
                                    "clock_id":"host_monotonic","evidence_ref":"/world/fixed_calibration/dynamic_pose/info"})
                            active=None
                except Exception as exc:
                    print(role,active["operation_id"] if active else "",type(exc).__name__,str(exc),flush=True)
                    if role=="executor":
                        speed(0.,0.)
                        reason = "navigation.target_tracking_lost" if str(exc)=="target_tracking_lost" else "navigation.motion_unconfirmed"
                        request("unknown",lease=lease,code=reason)
                    active=None
            time.sleep(.045)
    finally:
        if role=="executor":
            for _ in range(4):
                speed(0.,0.)
                time.sleep(.03)
            if active and active.get("kind")=="execute" and STOPPING.is_set():
                # Report the interrupted motion now rather than leaving the task to wait for the lease to expire.
                try:
                    request("unknown",lease=active["lease"],code="navigation.executor_stopped")
                except Exception as exc:
                    print(role,active["operation_id"],"unknown report failed",type(exc).__name__,flush=True)
        rclpy.try_shutdown()  # SIGTERM may already have shut the context down
        spinner.join(timeout=2)
        node.destroy_node()
