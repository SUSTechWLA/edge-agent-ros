"""Local operator-service I/O for the attended Lingao 50 mm candidate."""
from collections import deque
from dataclasses import asdict
import json
import math
import multiprocessing
import os
from pathlib import Path
import select
import socket
import threading
import time
import uuid
from urllib.request import urlopen

from calibration.lingao_short_trial import Measurement

PROFILE = "forward-50mm-100mmps"
SPEED = 0.1
MAX_SECONDS = 0.5
HELD_SECONDS = 0.12
FRESH_SECONDS = 0.15
NODE = "lingao_attended_operator_io"


def _identity(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except ValueError:
        return False


class Window:
    """All writes run in the independent device-side owner, using its own clock."""
    def __init__(self, write, clock=time.monotonic):
        self.write, self.clock = write, clock
        self.spent = set()
        self.closed = set()
        self.operation = None
        self.moving = False
        self.sequence = 0
        self.deadline = self.held_until = 0.

    def close(self):
        if self.operation is not None:
            self.closed.add(self.operation)
        self.operation = None
        if self.moving:
            self.moving = False
            self.write(0.)

    def tick(self, healthy):
        if self.operation and (not healthy or self.clock() >= min(self.deadline, self.held_until)):
            self.close()

    def request(self, msg, healthy):
        self.tick(healthy)
        op, seq, method = msg.get("op"), msg.get("seq"), msg.get("method")
        if not _identity(op):
            return False
        if method == "begin":
            deadline = msg.get("deadline")
            if op in self.spent:
                return False
            self.spent.add(op)
            if (self.operation or not healthy or type(deadline) not in (float,int) or
                    not 0 < deadline-self.clock() <= MAX_SECONDS):
                return False
            self.operation, self.deadline, self.sequence = op, deadline, 0
            self.held_until = min(deadline,self.clock()+HELD_SECONDS)
            return True
        if type(seq) is not int or seq<=0:
            return False
        if method=="stop":
            self.spent.add(op)  # A cancellation delivered before begin fences that begin.
            if self.operation != op:
                return op in self.closed
            current = seq > self.sequence
            self.close()       # Release is conservative, including for a duplicate sequence.
            return current
        if method!="forward" or self.operation!=op or seq<=self.sequence or not healthy:
            return False
        if type(msg.get("vx")) not in (float,int) or msg["vx"]!=SPEED:
            self.close()
            return False
        self.tick(healthy)
        if self.operation != op:
            return False
        self.sequence = seq
        self.moving = True
        self.write(SPEED)
        self.held_until = min(self.deadline,self.clock()+HELD_SECONDS)
        return True


class Robot:
    def __init__(self, enable_hardware):
        import rclpy
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry
        from rclpy.qos import qos_profile_sensor_data
        from rclpy.signals import SignalHandlerOptions
        self.clock = time.monotonic
        self.web = None
        self.quit = threading.Event()
        self.publisher = None
        self.enabled = enable_hardware
        device = Path("/proc/device-tree/model").read_bytes().rstrip(b"\0").decode()
        if ("Jetson" not in device or os.environ.get("ROS_DOMAIN_ID")!="61" or
                os.environ.get("ROS_LOCALHOST_ONLY")!="1"):
            raise ValueError("需要 Jetson 本机及底盘 ROS 域 61、localhost-only 1")
        self.rclpy, self.twist = rclpy, Twist
        rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
        self.node = rclpy.create_node(NODE, enable_rosout=False,start_parameter_services=False)
        self.wheels, self.raw = deque(maxlen=140), deque(maxlen=40)
        self.selected, self.safe = None, None
        self.origin = None
        self.takeover_count = None
        self.graph = {}
        self.graph_time = 0.
        self.node.create_subscription(Odometry,"/lingao/odom",self.wheel,qos_profile_sensor_data)
        self.node.create_subscription(Twist,"/cmd_vel",lambda m:self.velocity("raw",m),qos_profile_sensor_data)
        self.node.create_subscription(Twist,"/mode_mux/selected_cmd_vel",lambda m:self.velocity("selected",m),qos_profile_sensor_data)
        self.node.create_subscription(Twist,"/cmd_vel_safe",lambda m:self.velocity("safe",m),qos_profile_sensor_data)
        if enable_hardware:
            # Endpoint discovery only. No startup command, including zero.
            self.publisher = self.node.create_publisher(Twist,"/cmd_vel",1)
        threading.Thread(target=self.poll_web,daemon=True).start()

    def poll_web(self):
        while not self.quit.is_set():
            try:
                with urlopen("http://127.0.0.1:7070/api/status",timeout=.12) as resp:
                    data=json.loads(resp.read(65536))
                control,takeover=data["control"],data["takeover"]
                idle=data["ok"] is True and all(f is False for f in (control["active"],takeover["active"],takeover["estop"]))
                self.web=(self.clock(),idle,takeover["count"])
            except (OSError,ValueError,KeyError,TypeError):
                self.web=None
            self.quit.wait(.04)

    def velocity(self,kind,m):
        value=(m.linear.x,m.linear.y,m.linear.z,m.angular.x,m.angular.y,m.angular.z)
        if kind=="raw":
            self.raw.append((self.clock(),value))
        else:
            setattr(self,kind,(self.clock(),value))

    def wheel(self,m):
        q,p,t=m.pose.pose.orientation,m.pose.pose.position,m.twist.twist
        stamp=m.header.stamp.sec*10**9+m.header.stamp.nanosec
        now=self.clock()
        values=(p.x,p.y,q.x,q.y,q.z,q.w,t.linear.x,t.linear.y,t.angular.z)
        if (m.header.frame_id!="odom" or m.child_frame_id!="base_foot" or
                not all(map(math.isfinite,values)) or not 0<=time.time_ns()-stamp<=FRESH_SECONDS*1e9):
            self.wheels.clear()
            return
        if self.wheels and (not 0<stamp-self.wheels[-1].stamp<=FRESH_SECONDS*1e9 or now-self.wheels[-1].arrival>FRESH_SECONDS):
            self.wheels.clear()
        yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
        self.wheels.append(Measurement(stamp,now,p.x,p.y,yaw,t.linear.x,t.linear.y,t.angular.z))

    def update(self):
        self.rclpy.spin_once(self.node,timeout_sec=0.)
        now=self.clock()
        if now-self.graph_time<.045:
            return
        def owners(topic,pub=True):
            f=self.node.get_publishers_info_by_topic if pub else self.node.get_subscriptions_info_by_topic
            return [e.node_name for e in f(topic)]
        self.graph={"odom":owners("/lingao/odom"),"selected":owners("/mode_mux/selected_cmd_vel"),
                    "safe":owners("/cmd_vel_safe"),"safe_sinks":owners("/cmd_vel_safe",False),
                    "raw":owners("/cmd_vel"),"raw_sinks":owners("/cmd_vel",False),
                    "selected_sinks":owners("/mode_mux/selected_cmd_vel",False)}
        self.graph_time=self.clock()

    def status(self,active):
        now=self.clock()
        g=self.graph
        web=self.web
        peers=[s for s in g.get("raw",[]) if s!=NODE]
        nonzero=any(any(v) for at,v in self.raw if now-at<.5)
        wheel=self.wheels[-1] if self.wheels and g.get("odom")==["lingao_base_driver"] else None
        detail=[]
        if (not web or now-web[0]>FRESH_SECONDS or not web[1] or
                active and web[2]!=self.takeover_count):
            detail.append("遥控接管、软件急停或新鲜状态无法排除")
        if (now-self.graph_time>FRESH_SECONDS or self.node.get_parameter("use_sim_time").value or
                self.node.get_clock().ros_time_is_active or g.get("selected")!=["vehicle_mode_mux"] or
                g.get("safe")!=["vehicle_web_remote"] or "lingao_base_driver" not in g.get("safe_sinks",[]) or
                "vehicle_web_remote" not in g.get("selected_sinks",[]) or "vehicle_mode_mux" not in g.get("raw_sinks",[])):
            detail.append("底盘真实速度仲裁拓扑未证实")
        if peers:
            detail.append("共享 /cmd_vel 写入者 "+", ".join(peers)+("，已观测非零输入" if nonzero else "，采样窗口未见非零，缺少会话间排他"))
        if not wheel or now-wheel.arrival>FRESH_SECONDS:
            detail.append("未取得新鲜 MCU 里程计")
        if not active:
            baseline=[s for s in self.wheels if now-s.arrival<=1.5]
            if (len(baseline)<4 or baseline[-1].arrival-baseline[0].arrival<1. or
                    any(math.hypot(s.vx,s.vy)>.004 or abs(s.wz)>.015 or
                        math.dist((s.x,s.y),(baseline[0].x,baseline[0].y))>.002 for s in baseline)):
                detail.append("缺少连续静止窗口")
            if nonzero:
                detail.append("常规入口近期存在非零命令")
        for name,sample in (("mux",self.selected),("安全出口",self.safe)):
            if not sample or now-sample[0]>FRESH_SECONDS or (not active and any(sample[1])):
                detail.append(name+" 缺少新鲜的预期速度状态")
        if self.enabled and (not self.publisher or self.publisher.get_subscription_count()<1 or g.get("raw",[]).count(NODE)!=1):
            detail.append("本操作的低优先级输出未匹配")
        if not self.enabled:
            detail.append("当前是只读现场观察")
        return {"eligible":not detail,"detail":"；".join(detail) or "车端在环试动输入已就绪",
                "observed_monotonic":now,"odom":asdict(wheel) if wheel else None,"profiles":[] if detail else [PROFILE],
                "raw_publishers":peers,"raw_nonzero_observed":nonzero,"web":web,
                "endpoints":g,"evidence_scope":"hardware",
                "target_reached":bool(active and wheel and self.origin and math.dist((wheel.x,wheel.y),self.origin)>=.05)}

    def begin(self):
        self.origin=(self.wheels[-1].x,self.wheels[-1].y)
        self.takeover_count=self.web[2]

    def write(self,vx):
        if not self.publisher:
            raise RuntimeError("read-only I/O")
        m=self.twist();m.linear.x=vx
        self.publisher.publish(m)

    def close(self):
        self.quit.set();self.node.destroy_node();self.rclpy.shutdown()


def _send(endpoint, msg):
    # Telemetry and replies never stall the deadline owner if the UI process freezes.
    try:
        endpoint.send(json.dumps(msg,allow_nan=False).encode())
        return True
    except (BlockingIOError, OSError):
        return False


def _owner(endpoint, backend, enabled, scope):
    robot = window = None
    try:
        robot=backend(enabled)
        window=Window(robot.write)
        next_status=0.
        while True:
            robot.update()
            status=robot.status(bool(window.operation))
            window.tick(status["eligible"])
            if status.get("target_reached"):
                window.close()
            messages=[]
            for _ in range(24):
                try:
                    messages.append(json.loads(endpoint.recv(8192)))
                except BlockingIOError:
                    break
            # A stop arriving together with an older forward/begin takes precedence.
            messages.sort(key=lambda m: m.get("method") not in ("stop","shutdown"))
            for msg in messages:
                if msg.get("method")=="shutdown":
                    return
                status=robot.status(bool(window.operation))
                recent = msg["method"]=="stop" or 0 <= time.monotonic()-msg["issued"] < .06
                ok=window.request(msg,status["eligible"]) if recent else False
                if ok and msg["method"]=="begin":
                    robot.begin()
                _send(endpoint,{"reply":msg.get("id"),"ok":ok})
            if time.monotonic()>=next_status:
                status=robot.status(bool(window.operation))
                status["evidence_scope"]=scope
                _send(endpoint,{"state":status})
                next_status=time.monotonic()+.024
            select.select([endpoint],[],[],.002)
    except Exception as exc:
        _send(endpoint,{"fault":f"{type(exc).__name__}: {exc}"})
    finally:
        try:
            if window:
                window.close()
        finally:
            if robot:
                robot.close()
            endpoint.close()


def _pair():
    if os.name=="posix":
        return socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM)
    # Portable loopback pair for isolated tests. Hardware output requires native Jetson ROS.
    a=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    b=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    a.bind(("127.0.0.1",0));b.bind(("127.0.0.1",0))
    a.connect(b.getsockname());b.connect(a.getsockname())
    return a,b


class LingaoAttendedIO:
    """Private trusted operator-host interface. Construction starts no processes or ROS nodes."""
    evidence_scope="hardware"
    _backend=Robot

    def __init__(self):
        self._cv=threading.Condition()
        self._pending={}
        self._proc=self._socket=self._reader=None
        self._state=self._unknown("现场观察尚未启动")

    def _unknown(self, detail):
        return {"eligible":False,"profiles":[],"observed_monotonic":0.,"odom":None,
                "detail":detail,"evidence_scope":self.evidence_scope}

    def start(self, *, enable_hardware=False):
        with self._cv:
            if self._proc is not None:
                raise RuntimeError("I/O owner already started")
            local,child=_pair()
            local.setblocking(False);child.setblocking(False)
            self._socket=local
            self._proc=multiprocessing.get_context("spawn").Process(
                target=_owner,args=(child,self._backend,enable_hardware is True,self.evidence_scope),daemon=True)
            try:
                self._proc.start()
            except BaseException:
                local.close();self._proc=self._socket=None
                raise
            finally:
                child.close()
            self._reader=threading.Thread(target=self._receive,args=(local,self._proc),daemon=True)
            self._reader.start()

    def _receive(self, local, proc):
        try:
            while proc.is_alive():
                if not select.select([local],[],[],.05)[0]:
                    continue
                msg=json.loads(local.recv(65536))
                with self._cv:
                    if "fault" in msg:
                        self._state=self._unknown(msg["fault"])
                    elif "state" in msg:
                        self._state=msg["state"]
                    elif msg.get("reply") in self._pending:
                        self._pending[msg["reply"]]=msg["ok"] is True
                    self._cv.notify_all()
        except (OSError,ValueError):
            pass
        finally:
            with self._cv:
                if self._state.get("observed_monotonic"):
                    self._state=self._unknown("独立车端 I/O 已退出")
                self._cv.notify_all()

    def inspect(self):
        with self._cv:
            current=dict(self._state)
        observed=current["observed_monotonic"]
        if observed and not 0<=time.monotonic()-observed<FRESH_SECONDS:
            return self._unknown("车端 I/O 状态已过期")
        if current["odom"] is not None:
            current["odom"]=Measurement(**current["odom"])
        return current

    def _call(self, method, op, **fields):
        with self._cv:
            if not self._proc or not self._proc.is_alive():
                return False
            key=uuid.uuid4().hex
            self._pending[key]=None
            try:
                if not _send(self._socket,{"id":key,"method":method,"op":op,"issued":time.monotonic(),**fields}):
                    return False
                answered=self._cv.wait_for(lambda:self._pending[key] is not None or not self._proc or
                                           not self._proc.is_alive(),timeout=.06)
                if not answered and method in ("begin","forward"):
                    # The acknowledgment may have been lost after acceptance.
                    _send(self._socket,{"method":"stop","op":op,"seq":fields.get("seq",0)+1})
                return answered and self._pending[key] is True
            finally:
                del self._pending[key]

    def begin(self, operation_id, deadline_monotonic):
        if type(deadline_monotonic) not in (float,int) or not math.isfinite(deadline_monotonic):
            return False
        return self._call("begin",operation_id,deadline=deadline_monotonic)

    def forward(self, operation_id, seq, vx_m_s):
        if type(vx_m_s) not in (int,float) or not math.isfinite(vx_m_s):
            return False
        return self._call("forward",operation_id,seq=seq,vx=vx_m_s)

    def stop(self, operation_id, seq):
        return self._call("stop",operation_id,seq=seq)

    def close(self):
        with self._cv:
            proc=self._proc
            if proc is None:
                return
            _send(self._socket,{"method":"shutdown"})
        proc.join(timeout=1)
        if proc.is_alive():
            # The built-in command-receipt watchdogs remain the fallback for a wedged owner.
            proc.terminate();proc.join(timeout=.3)
        self._reader.join(timeout=.2)
        with self._cv:
            self._socket.close();self._socket=self._proc=self._reader=None
            self._state=self._unknown("车端 I/O 已关闭")
            self._cv.notify_all()


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect",action="store_true",help="Read site evidence, without creating a velocity writer.")
    args=parser.parse_args()
    if not args.inspect:
        parser.print_help()
        return
    io=LingaoAttendedIO()
    io.start()
    reports=[]
    try:
        until=time.monotonic()+7
        while time.monotonic()<until:
            time.sleep(.12)
            data=io.inspect()
            if data["observed_monotonic"] and data["odom"] is not None:
                data["odom"]=asdict(data["odom"]);reports.append(data)
        if not reports:
            data=io.inspect()
            if data["odom"] is not None:
                data["odom"]=asdict(data["odom"])
            reports.append(data)
        print(json.dumps({"schema":"edge.lingao.attended-inspection.v1","profile":PROFILE,
                          "cold_start_scope":"read_only_no_velocity_publisher","samples":len(reports),
                          "cutoff":{"scope":"attended /cmd_vel command window","maximum_s":MAX_SECONDS,
                                    "hold_refresh_s":HELD_SECONDS,"normal_stop":"same-writer zero Twist",
                                    "owner_or_transport_failure":"downstream receipt-time watchdogs",
                                    "mechanical_stopping_distance_m":None},
                          "last":reports[-1]},ensure_ascii=False,allow_nan=False))
    finally:
        io.close()


if __name__=="__main__":
    main()
