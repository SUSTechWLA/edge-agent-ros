"""One stationary-base left turn. Uses the existing ROS input and latching stock stop."""
import argparse
from collections import deque
import fcntl
import uuid
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

import rclpy
from action_msgs.msg import GoalStatusArray
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.duration import Duration
from rclpy.qos import QoSProfile, qos_profile_sensor_data, ReliabilityPolicy, DurabilityPolicy
from std_msgs.msg import String
from control_profile import read_profile

CONTROL, CONTROL_SHA = read_profile()
ODOM = CONTROL["odometry"]
FRAMES = (ODOM["parent_frame"], ODOM["child_frame"])
STOP = CONTROL["stop"]
TARGET = math.radians(10)
CUTOFF = math.radians(9)
MAX_DRIFT = .015
MAX_WZ = .18
DEADLINES = {"forward-50cm": 5_700_000_000, "left-10deg": 2_100_000_000}


def angle(yaw, origin):
    return math.remainder(yaw - origin, 2 * math.pi)


def pose_delta(w, start):
    return angle(w[4], start[4]), math.dist(w[2:4], start[2:4])


def check_turn_sample(w, prev, start):
    yaw_delta, drift = pose_delta(w, start)
    require(w[8:] == FRAMES and all(map(math.isfinite, w[2:8])),
            "bad MCU frame or measurement")
    stamp_delta = (w[1] - prev[1]) * 1e-9
    require(0 < stamp_delta <= .15 and 0 <= w[0] - prev[0] <= .15, "MCU telemetry discontinuity")
    require(abs(angle(w[4], prev[4])) <= .003 + MAX_WZ * stamp_delta, "MCU yaw jump")
    require(-.006 <= yaw_delta <= math.radians(12) and -.025 <= w[7] <= MAX_WZ,
            "unexpected turn direction, angle or yaw rate")
    require(drift <= MAX_DRIFT and math.hypot(w[5], w[6]) <= .04,
            "unexpected centre displacement or linear velocity")


def require(ok, detail):
    if not ok:
        raise RuntimeError(detail)


def remote_state():
    with urlopen("http://127.0.0.1:7070/api/status", timeout=.16) as res:
        value = json.load(res)
    require(value.get("ok") is True, "native arbiter HTTP unavailable")
    return {"estop": value["takeover"]["estop"], "takeover": value["takeover"]["active"],
            "counter": value["takeover"]["count"], "manual": value["control"]["active"]}


def idle(s):
    return all(s[k] is False for k in ("estop","takeover","manual"))


class Child:
    def __init__(self, args):
        self.p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.lines = deque(maxlen=256)
        def read():
            for line in self.p.stdout:
                self.lines.append(line.rstrip())
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()

    def send(self, text):
        self.p.stdin.write(text + "\n")
        self.p.stdin.flush()

    def has(self, text):
        return any(line==text or line.startswith(text+" ") for line in tuple(self.lines))

    def observer_alive(self):
        rows=tuple(self.lines)
        return (self.p.poll() is None and not any(line.startswith(("FOREIGN","FAULT","ERROR")) for line in rows)
                and any(line.startswith("ALIVE ") and 0 <= time.monotonic_ns()-int(line.split()[-1]) < 120_000_000
                        for line in rows[-12:]))


def run(args, state):
    turning = args.profile == "left-10deg"
    limits = CONTROL["command_limits"]
    command = ((0., 0., 0., 0., 0., limits["yaw_radps"]) if turning
               else (limits["forward_mps"], 0., 0., 0., 0., 0.))
    max_duration = DEADLINES[args.profile]
    require(os.environ.get("ROS_DOMAIN_ID") == "61" and os.environ.get("ROS_LOCALHOST_ONLY") == "1"
            and b"Jetson" in Path("/proc/device-tree/model").read_bytes(), "unexpected robot or domain")
    require(hashlib.sha256(Path("/home/cas/vehicle-web-remote/server.py").read_bytes()).hexdigest()
            == "94cd69afdfe84ef3c1ca3dff9c1537540754a15d5b4b62363471b2f7babb4aae", "unverified stock stop handler")
    folder = Path(__file__).resolve().parent
    claim = state / (str(args.operation_id) + ".armed")
    receipt = {"scope":"operator_attended_hardware", "profile":args.profile,
               "chassis_profile_sha256":CONTROL_SHA,
               "operation_id":str(args.operation_id) if args.operation_id else None,
               "requested_vx_m_s":command[0], "requested_wz_rad_s":command[5],
               "maximum_command_s":max_duration/1e9, "begin_ns":str(time.time_ns()), "positive_writes":0}
    rclpy.init()
    node = rclpy.create_node("field_twist_one_second", enable_rosout=False, start_parameter_services=False)
    topics = tuple(CONTROL[key]["topic"] for key in ("command","selected","safe"))
    trace = {topic:[] for topic in topics}
    wheels, stops, teleop, active_goals = [], [], [], {}
    def twist(topic,m):
        trace[topic].append((time.monotonic(),tuple(getattr(c,k) for c in (m.linear,m.angular) for k in "xyz")))
    def wheel(m):
        p,q,t=m.pose.pose.position,m.pose.pose.orientation,m.twist.twist
        yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
        if not .999 <= math.hypot(q.x,q.y,q.z,q.w) <= 1.001:
            yaw=math.nan
        wheels.append((time.monotonic(),m.header.stamp.sec*10**9+m.header.stamp.nanosec,p.x,p.y,yaw,t.linear.x,t.linear.y,t.angular.z,m.header.frame_id,m.child_frame_id))
    for topic in topics:
        node.create_subscription(Twist,topic,lambda m,topic=topic:twist(topic,m),qos_profile_sensor_data)
    node.create_subscription(Odometry,ODOM["topic"],wheel,qos_profile_sensor_data)
    node.create_subscription(String,STOP["topic"],lambda m:stops.append((time.monotonic(),m.data)),10)
    node.create_subscription(Twist,"/cmd_vel_teleop",lambda m:teleop.append(time.monotonic()),qos_profile_sensor_data)
    action_qos=QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
    for action in ("navigate_to_pose","navigate_through_poses","follow_waypoints","follow_path"):
        node.create_subscription(GoalStatusArray,"/"+action+"/_action/status",lambda m,a=action:
                                 active_goals.update({a:any(v.status in (1,2,3) for v in m.status_list)}),action_qos)
    def spin(s):
        end=time.monotonic()+s
        while time.monotonic()<end:
            rclpy.spin_once(node,timeout_sec=min(.008,max(0.,end-time.monotonic())))
    def owners(topic, publishers=True):
        infos=(node.get_publishers_info_by_topic(topic) if publishers else node.get_subscriptions_info_by_topic(topic))
        return sorted((x.node_name,x.node_namespace,x.topic_type) for x in infos)
    def unique(topic,name,kind):
        return owners(topic)==[(name,"/",kind)]
    def route_gids():
        endpoints=[]
        for topic in (topics[1],topics[2],ODOM["topic"]):
            endpoints.extend((topic,e.node_name,bytes(e.endpoint_gid)) for e in node.get_publishers_info_by_topic(topic))
        endpoints.extend((STOP["topic"],e.node_name,bytes(e.endpoint_gid)) for e in node.get_subscriptions_info_by_topic(STOP["topic"]) if e.node_name=="vehicle_web_remote")
        return sorted(endpoints)
    def fresh_wheels():
        return bool(wheels and wheels[-1][8:]==FRAMES and
                    0 <= time.time_ns()-wheels[-1][1] < 150_000_000 and
                    time.monotonic()-wheels[-1][0]<.15 and all(map(math.isfinite,wheels[-1][2:8])))
    def quiet(rows):
        quiet_s = CONTROL["stationary_seconds"]
        return (len(rows)>15 and rows[-1][0]-rows[0][0]>=quiet_s and rows[-1][1]-rows[0][1]>=round(quiet_s*1e9) and
                all(0 < b[1]-a[1] <= 150_000_000 and 0 <= b[0]-a[0] <= .15 for a,b in zip(rows,rows[1:])) and
                all(w[8:]==FRAMES and math.hypot(w[5],w[6])<=.003 and abs(w[7])<=.015 and
                    abs(angle(w[4],rows[0][4]))<=.006 and math.dist(w[2:4],rows[0][2:4])<=.0015
                    for w in rows))
    def latest_outputs_fresh():
        return all(trace[t] and time.monotonic()-trace[t][-1][0]<.15 for t in topics[1:])
    def pose(w):
        return {"stamp_ns":str(w[1]),"x_m":w[2],"y_m":w[3],"yaw_rad":w[4],"frame":FRAMES[0],"child":FRAMES[1]}

    writer = node.create_publisher(Twist,topics[0],QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE, lifespan=Duration(nanoseconds=120_000_000)))
    guard = brake = None
    hmi_end=threading.Event()
    web_samples=deque(maxlen=12)
    def poll_web():
        while not hmi_end.is_set():
            try:web_samples.append((time.monotonic(),remote_state()))
            except (OSError,ValueError,RuntimeError):web_samples.append((time.monotonic(),None))
            hmi_end.wait(.045)
    armed=False
    t0=None
    zero=Twist()
    requested=Twist();requested.linear.x,requested.angular.z=command[0],command[5]
    try:
        guard=Child([str(folder/"build"/"field_raw_gid")])
        brake=Child([sys.executable,"-B",str(folder/"brake.py"),args.profile])
        until=time.monotonic()+9
        while time.monotonic()<until and not (guard.has("READY") and brake.has("READY "+CONTROL_SHA) and fresh_wheels()
              and quiet([w for w in wheels if time.monotonic()-w[0]<2.])):
            require(guard.p.poll() is None and brake.p.poll() is None,"observers unavailable")
            spin(.025)
        require(guard.has("READY") and brake.has("READY "+CONTROL_SHA),"observers or brake profile not ready")
        require(fresh_wheels() and quiet([w for w in wheels if time.monotonic()-w[0]<2.]),"MCU standstill unavailable")
        require(unique(ODOM["topic"],"lingao_base_driver",ODOM["type"]) and
                unique(topics[1],"vehicle_mode_mux",CONTROL["selected"]["type"]) and
                unique(topics[2],"vehicle_web_remote",CONTROL["safe"]["type"]),"unexpected odometry or control owner")
        require(("vehicle_mode_mux","/",CONTROL["command"]["type"]) in owners(topics[0],False) and
                ("vehicle_web_remote","/",CONTROL["selected"]["type"]) in owners(topics[1],False) and
                ("lingao_base_driver","/",CONTROL["safe"]["type"]) in owners(topics[2],False) and
                {x[0] for x in owners(topics[0])} == {"field_twist_one_second","lingao_mqtt_bridge"},
                "unexpected source/sink")
        require(latest_outputs_fresh() and all(not any(v) for rows in trace.values() for _,v in rows),
                "recent nonzero velocity or stale output")
        for action in ("navigate_to_pose","navigate_through_poses","follow_waypoints","follow_path"):
            require(not owners("/"+action+"/_action/status") or active_goals.get(action) is False,
                    "unknown or active navigation goal")
        h=remote_state();require(idle(h),"local estop or takeover")
        receipt["initial_hmi"]=h
        route=route_gids()
        require(len(route)==4,"ambiguous native route")
        threading.Thread(target=poll_web,daemon=True).start()
        # Prove this very writer passes through the native selector while stationary.
        marker=Twist();marker.linear.x=-0.
        tag=time.monotonic()
        for _ in range(5):
            writer.publish(marker);spin(.05)
        for t in topics:
            require(any(at>=tag and not any(v) and math.copysign(1,v[0])<0 for at,v in trace[t]),"stock route did not select zero marker "+t)
        require(guard.has("OWN") and all(not any(v) for rows in trace.values() for _,v in rows),"source identification or idle verification failed")
        require(guard.p.poll() is None and brake.p.poll() is None and not teleop and not stops and
                not any(active_goals.values()) and idle(remote_state()) and fresh_wheels() and latest_outputs_fresh(),
                "arming condition changed")
        require(guard.observer_alive(), "source conflict or lost observer before start")
        if args.mode == "preflight":
            receipt["result"]="verified_current_route_and_stop_listener_without_arming"
            return True
        start_w=wheels[-1];receipt["initial_odom"]=pose(start_w)
        checked=len(wheels)
        with claim.open("x") as claim_file:
            claim_file.write(str(time.time_ns()));claim_file.flush();os.fsync(claim_file.fileno())
        deadline=time.monotonic_ns()+max_duration
        armed=True
        brake.send("ARM "+str(deadline))
        ack_by=time.monotonic()+.09
        while time.monotonic()<ack_by and not brake.has("ARMED "+str(deadline)):spin(.002)
        require(brake.has("ARMED "+str(deadline)),"independent stop timer not armed")
        t0=time.monotonic()
        receipt["deadline_from_first_dispatch_s"]=deadline/1e9-t0
        next_write=t0
        next_graph=t0
        last_progress=0.; progress_time=t0
        while time.monotonic_ns()<deadline:
            require(brake.p.poll() is None and guard.observer_alive(), "control ownership changed or observer stale")
            require(not teleop and not stops and not any(active_goals.values()),"takeover, external stop or active goal")
            require(fresh_wheels() and latest_outputs_fresh(),"odometry or native arbitration feedback stale")
            recent=web_samples[-1] if web_samples else None
            require(recent and time.monotonic()-recent[0]<.18 and recent[1] and idle(recent[1]) and
                    recent[1]["counter"]==h["counter"],"stock safety state or manual priority changed")
            if time.monotonic()>=next_graph:
                require(route_gids()==route,"native control or braking endpoint changed")
                next_graph=time.monotonic()+.045
            if turning:
                for i in range(checked,len(wheels)):
                    check_turn_sample(wheels[i], wheels[i-1], start_w)
                checked=len(wheels)
                w=wheels[-1]; progress,_=pose_delta(w,start_w)
                if progress >= CUTOFF:
                    receipt["turn_at_brake_deg"]=math.degrees(progress)
                    break
            else:
                w=wheels[-1]; dx=w[2]-start_w[2];dy=w[3]-start_w[3];c=math.cos(start_w[4]);s=math.sin(start_w[4])
                progress=dx*c+dy*s
                require(progress>=-.0015 and w[5]>=-.004 and abs(dy*c-dx*s)<=.003 and
                        math.hypot(w[5],w[6])<=.105 and abs(math.remainder(w[4]-start_w[4],2*math.pi))<=.035,
                        "unexpected wheel motion")
                if progress-last_progress>=.004:
                    last_progress,progress_time=progress,time.monotonic()
                if time.monotonic()-t0>.50:
                    require(time.monotonic()-progress_time<.40,"odometry progression stalled")
                predicted=progress+max(0.,w[5])*max(0.,time.monotonic()-w[0])
                if predicted>=.480:
                    receipt.update(latch_progress_m=progress,projected_latch_progress_m=predicted)
                    break
            for t in topics[1:]:
                require(all(all(math.isfinite(x) for x in v) and v in ((0.,)*6,command)
                            for at,v in trace[t] if at>=t0),"unexpected output "+t)
            if time.monotonic()-t0>.22:
                require(all(any(at>=t0 and v==command for at,v in trace[t]) for t in topics[1:]),"motion input not selected")
            if turning and time.monotonic()-t0>.65:
                history=[r for r in wheels if r[0]>=t0 and r[0]<=w[0]-.35]
                require(history and angle(w[4],history[-1][4])>.006 and progress>.012,
                        "no sustained positive MCU yaw response")
            if time.monotonic_ns() >= deadline-80_000_000:
                break
            if time.monotonic()>=next_write and time.monotonic_ns()<deadline-80_000_000:
                writer.publish(requested);receipt["positive_writes"]+=1;next_write=time.monotonic()+.045
            spin(.004)
        receipt["result"]="target_cutoff" if ("turn_at_brake_deg" in receipt or "latch_progress_m" in receipt) else "deadline_before_target"
    except Exception as e:
        receipt.update(result="stopped_early" if armed else "not_armed",reason=str(e))
    finally:
        if armed:
            start_zero=time.monotonic()
            try: brake.send("STOP")
            except (BrokenPipeError,OSError): pass
            for _ in range(15):
                writer.publish(zero);spin(.025)
            spin(2.5)
            cutoff=time.monotonic()
            after=[w for w in wheels if cutoff-w[0]<=1.85]
            receipt["mcu_stopped"]=fresh_wheels() and quiet(after)
            receipt["stop_commands_seen"]=[m for _,m in stops[-45:]]
            try: receipt["final_hmi"]=remote_state()
            except Exception as e: receipt["hmi_error"]=str(e)
            receipt["brake_observer"]=list(brake.lines)
            if t0 is not None and wheels:
                observed=[w for w in wheels if w[0]>=t0]
                final_yaw,final_drift=pose_delta(wheels[-1],start_w)
                receipt.update(zero_after_s=start_zero-t0,final_odom=pose(wheels[-1]),
                               peak_mcu_left_rad_s=max((w[7] for w in observed),default=None),
                               peak_centre_drift_m=max((pose_delta(w,start_w)[1] for w in observed),default=None),
                               final_centre_drift_m=final_drift,
                               relative_yaw_deg=math.degrees(final_yaw),
                               yaw_error_deg=math.degrees(final_yaw-TARGET))
                receipt["topics"]={t:{"positive":sum(v==command for at,v in rows if at>=t0),
                    "last_three_zero":len(rows)>=3 and all(not any(v) for _,v in rows[-3:])} for t,rows in trace.items()}
                receipt["trace"]={t:[(round(at-t0,5),list(v)) for at,v in rows if at>=t0] for t,rows in trace.items()}
                receipt["mcu_trace"]=[(round(w[0]-t0,5),str(w[1]),*w[2:8]) for w in wheels if w[0]>=t0]
                forward_m = ((wheels[-1][2]-start_w[2])*math.cos(start_w[4]) +
                             (wheels[-1][3]-start_w[3])*math.sin(start_w[4]))
                receipt["odom_signed_forward_m"] = forward_m
                receipt["peak_mcu_forward_m_s"] = max((w[5] for w in observed), default=None)
                response_ok = (.035 <= (receipt["peak_mcu_left_rad_s"] or 0) <= MAX_WZ
                               and (receipt["peak_centre_drift_m"] or 0) <= MAX_DRIFT
                               and abs(receipt["yaw_error_deg"]) <= 2.) if turning else (
                                   .48 <= forward_m <= .515 and .025 <= (receipt["peak_mcu_forward_m_s"] or 0) <= .105)
                receipt["verified"] = bool(receipt["result"]=="target_cutoff" and receipt["mcu_stopped"] and
                    receipt.get("final_hmi",{}).get("estop") is True and receipt["stop_commands_seen"] and
                    all(v==STOP["latch_value"] for v in receipt["stop_commands_seen"]) and response_ok and
                    all(item["positive"]>0 and item["last_three_zero"] for item in receipt["topics"].values()))
        if guard:receipt["gid_observer_tail"]=list(guard.lines)[-8:]
        hmi_end.set()
        if brake:
            brake.p.stdin.close()
            try:brake.p.wait(timeout=3)
            except subprocess.TimeoutExpired:pass
        if guard and guard.p.poll() is None:
            guard.p.terminate();guard.p.wait(timeout=3)
        path=state/(str(args.operation_id)+".json" if args.mode=="run" else f"preflight-{time.time_ns()}.json")
        with path.open("x") as report:
            json.dump(receipt,report,ensure_ascii=False,indent=2,allow_nan=False)
            report.flush();os.fsync(report.fileno())
        print(path.name, json.dumps({k:v for k,v in receipt.items() if k not in ("trace","mcu_trace","stop_commands_seen")},ensure_ascii=False),flush=True)
        node.destroy_node();rclpy.shutdown()

    return receipt.get("verified", False)


def main():
    parser = argparse.ArgumentParser(description="Run an operator-attended trial on the Lingao Jetson.")
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("profile", choices=tuple(DEADLINES))
    parser.add_argument("--operation-id", type=uuid.UUID, help="Unique request identifier, retained with the receipt.")
    parser.add_argument("--attended", action="store_true",
                        help="The on-site operator confirms clearance and has vehicle emergency stop within reach.")
    args = parser.parse_args()
    if args.mode == "run" and (not args.operation_id or not args.attended):
        parser.error("run requires --operation-id and --attended")
    os.umask(0o077)
    state = Path.home() / ".local/state/edge-lingao-control"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "owner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.mode == "run" and (state / (str(args.operation_id) + ".json")).exists():
            parser.error("operation already recorded")
        if args.mode == "run":
            for claim in state.glob("*.armed"):
                report = claim.with_suffix(".json")
                if not report.exists() or json.loads(report.read_text()).get("mcu_stopped") is not True:
                    parser.error(f"unresolved prior operation: {claim.stem}")
        return 0 if run(args, state) else 1


if __name__ == "__main__":
    sys.exit(main())
