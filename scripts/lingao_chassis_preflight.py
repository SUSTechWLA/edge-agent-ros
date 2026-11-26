"""Read-only field observations for a Lingao calibration station (native ROS 2 Humble)."""
import argparse
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.request import urlopen


FRESH_NS = 350_000_000
NS = 1_000_000_000


def check(label, ok, detail, *, required=True):
    return {"label": label, "ok": ok, "detail": detail, "required": required}


def stopped(samples, now_ns):
    """Samples carry source stamp, monotonic arrival, frame pair, twist norms and position."""
    if len(samples) < 3:
        return False, f"采得 {len(samples)} 帧；静止证据不足"
    stamps, arrivals, frames, linear, angular, positions = zip(*samples)
    if not 0 <= now_ns - stamps[-1] <= FRESH_NS or time.monotonic() - arrivals[-1] > 0.35:
        return False, "最新里程计过期或来自未来"
    if len(set(frames)) != 1 or not all(frames[0]):
        return False, "里程计参考帧缺失或在窗口内改变"
    if stamps[-1] - stamps[0] < 1.5 * NS or arrivals[-1] - arrivals[0] < 1.5:
        return False, f"{len(samples)} 帧，有效观测不足 1.5 秒"
    if any(not 0 < b - a <= FRESH_NS for a, b in zip(stamps, stamps[1:])) or any(
        b - a > 0.35 for a, b in zip(arrivals, arrivals[1:])
    ):
        return False, "里程计不连续或重复"
    values = [*linear, *angular, *(v for p in positions for v in p)]
    if not all(map(math.isfinite, values)):
        return False, "里程计含非有限数值"
    travel = max(math.dist(p, positions[0]) for p in positions)
    return (max(linear) <= 0.01 and max(angular) <= 0.02 and travel <= 0.02,
            f"{len(samples)} 帧 / {(stamps[-1]-stamps[0])/NS:.2f} 秒，"
            f"峰值 {max(linear):.3f} m/s、{max(angular):.3f} rad/s，位置变化 {travel:.3f} m")


def stamp_ns(stamp):
    return stamp.sec * NS + stamp.nanosec


def valid_rear_cloud(message):
    if hasattr(message, "points"):
        points = ((p.x, p.y, p.z) for p in message.points)
    else:
        from sensor_msgs_py.point_cloud2 import read_points
        points = read_points(message, field_names=("x", "y", "z"), skip_nans=False)
    # Bounded inspection of the native scan, without converting a full cloud to a Python list.
    from itertools import islice
    return any(all(map(math.isfinite, p)) and math.hypot(*p) > 0.05
               for p in islice(points, 2048))


def depth_sample(message):
    """Count nonzero native depth pixels using the ROS row stride (either byte order)."""
    if (message.encoding != "16UC1" or message.width < 1 or message.height < 1 or
            message.step < 2 * message.width or len(message.data) < message.step * message.height):
        return 0, 0
    raw = memoryview(message.data).cast("B")
    total = valid = 0
    for row in range(0, message.height, max(1, message.height // 12)):
        for col in range(0, message.width, max(1, message.width // 16)):
            offset = row * message.step + col * 2
            total += 1
            valid += int(raw[offset] != 0 or raw[offset + 1] != 0)
    return valid, total


def web_status():
    try:
        with urlopen("http://127.0.0.1:7070/api/status", timeout=0.7) as response:
            status = json.loads(response.read(65_536))
        takeover, control = status["takeover"], status["control"]
        flags = [takeover["active"], takeover["estop"], control["active"]]
        ok = status.get("ok") is True and all(v is False for v in flags)
        return check("车端遥控仲裁", ok, f"只读状态：遥控接管 {flags[0]}，软件急停 {flags[1]}，遥控输入 {flags[2]}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return check("车端遥控仲裁", None, f"状态读取失败（{type(exc).__name__}）")


def inspect_ros(args):
    import rclpy
    from rclpy.action.graph import get_action_server_names_and_types_by_node
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.time import Time
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import Image, LaserScan, PointCloud2
    from rosidl_runtime_py.utilities import get_message
    from tf2_ros import Buffer, TransformListener

    node = rclpy.create_node("calibration_chassis_preflight", enable_rosout=False,
                             start_parameter_services=False)
    try:
        tf = Buffer()
        listener = TransformListener(tf, node)
        samples, rear, scans, passive_rear = [], [], [], []
        depth = []
        direction = {"forward": "前进", "reverse": "倒车", "both": "前进与倒车"}[args.direction]
        forward_required = args.direction in ("forward", "both")
        reverse_required = args.direction in ("reverse", "both")

        def odometry(msg):
            twist, p = msg.twist.twist, msg.pose.pose.position
            linear = math.hypot(twist.linear.x, twist.linear.y, twist.linear.z)
            angular = math.hypot(twist.angular.x, twist.angular.y, twist.angular.z)
            samples.append((stamp_ns(msg.header.stamp), time.monotonic(),
                            (msg.header.frame_id, msg.child_frame_id), linear, angular, (p.x, p.y, p.z)))

        def cloud(msg, readings):
            try:
                valid = valid_rear_cloud(msg)
            except (ValueError, TypeError, AssertionError, KeyError, IndexError):
                valid = False
            count = len(msg.points) if hasattr(msg, "points") else msg.width * msg.height
            readings[:] = [(msg.header, valid, count)]

        def scan(msg):
            valid = bool(msg.ranges) and 0 <= msg.range_min < msg.range_max and any(
                math.isfinite(x) and msg.range_min <= x <= msg.range_max for x in msg.ranges)
            scans[:] = [(msg.header, valid)]

        def depth_image(msg):
            depth[:] = [msg]

        node.create_subscription(Odometry, args.odom, odometry, qos_profile_sensor_data)
        node.create_subscription(Image, args.front_depth, depth_image, qos_profile_sensor_data)
        node.create_subscription(LaserScan, args.obstacles, scan, qos_profile_sensor_data)
        node.create_subscription(PointCloud2, args.passive_rear, lambda m: cloud(m, passive_rear),
                                 qos_profile_sensor_data)
        rear_subscription = None
        rear_type = ""
        started = time.monotonic()
        discovery = 0.0
        while time.monotonic() - started < 5.5:
            if rear_subscription is None and time.monotonic() >= discovery:
                discovery = time.monotonic() + 0.4
                types = dict(node.get_topic_names_and_types()).get(args.rear, [])
                if len(types) == 1 and types[0] in ("sensor_msgs/msg/PointCloud2", "livox_ros_driver2/msg/CustomMsg"):
                    rear_type = types[0]
                    try:
                        rear_subscription = node.create_subscription(get_message(rear_type), args.rear,
                                                                     lambda m: cloud(m, rear), qos_profile_sensor_data)
                    except (ImportError, AttributeError, ValueError):
                        pass
            rclpy.spin_once(node, timeout_sec=0.04)
            # Allow extra time for live DDS discovery without relaxing the stopped window.
            if time.monotonic() - started >= 3 and samples and samples[-1][1] - samples[0][1] >= 1.8:
                break

        def owners(topic, publishers=True):
            query = node.get_publishers_info_by_topic if publishers else node.get_subscriptions_info_by_topic
            return [e.node_name for e in query(topic)]

        now = node.get_clock().now().nanoseconds
        def fresh(records):
            return bool(records and records[0][1] and records[0][0].frame_id and
                        0 <= now - stamp_ns(records[0][0].stamp) <= FRESH_NS)

        ok, detail = stopped(samples, now)
        odom_owner = owners(args.odom)
        odom_base = samples[-1][2][1] if samples else None
        checks = [check("实车里程计与停稳窗口", ok and odom_owner == ["lingao_base_driver"] and odom_base == args.base,
                        f"{args.odom} 来源 {odom_owner}，子帧 {odom_base}（期望 {args.base}）；{detail}")]
        checks.append(check("本次预检方向", True, f"{direction}。移动入口使用底盘速度仲裁，方向以外的项目仅供参考。"))
        depth_ok, depth_detail = None, "本探针尚未收到深度帧"
        if depth:
            msg = depth[0]
            age = now - stamp_ns(msg.header.stamp)
            good, total = depth_sample(msg)
            depth_ok = 0 <= age <= FRESH_NS and good > 0 and msg.header.frame_id == args.front_depth_frame
            depth_detail = f"{msg.width}×{msg.height}，{msg.header.frame_id}，采样年龄 {age/1e6:.0f} ms，有效测点 {good}/{total}"
        checks.append(check("前进 · D435 深度观测", depth_ok, f"{args.front_depth}；{depth_detail}", required=False))
        fresh_rear = fresh(rear)
        checks.append(check("原厂后向保护输入", fresh_rear and len(owners(args.rear)) == 1,
                            f"{args.rear} 发布者 {owners(args.rear)}；"
                            f"{rear_type or '无可用点云类型'}；有效新鲜采样 {fresh_rear}。用于倒车。", required=reverse_required))
        passive_ok = None
        passive_detail = f"本探针在 DDS 域 {os.environ.get('ROS_DOMAIN_ID')} 中未收到观测；可见性未知"
        if passive_rear:
            header, valid, count = passive_rear[0]
            age_ms = (now - stamp_ns(header.stamp)) / 1_000_000
            passive_ok = (fresh(passive_rear) and header.frame_id == args.passive_rear_frame
                          and len(owners(args.passive_rear)) == 1)
            passive_detail = (f"帧 {header.frame_id}（期望 {args.passive_rear_frame}），{count} 点，"
                              f"有效点 {valid}，采样年龄 {age_ms:.0f} ms")
        checks.append(check("后向原生点云（独立观测）", passive_ok,
                            f"{args.passive_rear}；{passive_detail}", required=False))
        transform_ok = fresh_rear and tf.can_transform(args.base, rear[0][0].frame_id,
                                                       Time.from_msg(rear[0][0].stamp))
        checks.append(check("原厂后向保护输入到车体的变换", transform_ok,
                            f"按扫描时刻查询 {rear[0][0].frame_id if rear else '未知源帧'} → {args.base}。用于倒车。",
                            required=reverse_required))
        checks.append(check("导航方案的避障扫描（参考）", fresh(scans) and len(owners(args.obstacles)) == 1,
                            f"{args.obstacles} 发布者 {owners(args.obstacles)}；有效新鲜扫描 {fresh(scans)}", required=False))
        mux_pubs, mux_subs = owners("/mode_mux/selected_cmd_vel"), owners("/mode_mux/selected_cmd_vel", False)
        safe_pubs, safe_subs = owners("/cmd_vel_safe"), owners("/cmd_vel_safe", False)
        path = (mux_pubs == ["vehicle_mode_mux"] and "vehicle_web_remote" in mux_subs
                and safe_pubs == ["vehicle_web_remote"] and "lingao_base_driver" in safe_subs
                and "lingao_base_driver" not in (mux_subs + owners("/cmd_vel", False)))
        checks.append(check("已部署的车端速度仲裁链", path,
                            f"mux 发布者 {mux_pubs}，安全出口发布者 {safe_pubs}，底盘订阅者 {safe_subs}"))
        stop_sources = owners("/stop_cmd_vel")
        guard_subscribers = owners(args.front_depth, False)
        known_route = path and stop_sources == ["mqtt_nav_bridge"] and "vehicle_web_remote" not in guard_subscribers
        checks.append(check("前进 · 正常移动入口的障碍制停验收", False if known_route else None,
                            f"停车输入发布者 {stop_sources}。目标跟随 close_guard 约束 /front_servo/cmd_vel；"
                            "常规 /cmd_vel 前进通道尚缺障碍触发、底盘停车的独立回执。",
                            required=forward_required))
        servers = []
        for name, namespace in set(node.get_node_names_and_namespaces()):
            try:
                actions = get_action_server_names_and_types_by_node(node, name, namespace)
            except RuntimeError:  # Endpoint disappeared during discovery.
                continue
            if any(topic == "/navigate_to_pose" and "nav2_msgs/action/NavigateToPose" in types
                   for topic, types in actions):
                servers.append(name)
        checks.append(check("Nav2 方案的动作服务（参考）", len(servers) == 1,
                            f"/navigate_to_pose 的服务端 {servers}。原厂 move_relative.py 可独立于 Nav2 运行。",
                            required=False))
        checks.append(check("自主换位的现场验收", None,
                            "缺少任务绑定的有限位移、避障制停和独立停稳验收回执"))
        return checks
    finally:
        node.destroy_node()


def snapshot(args, startup_error=None):
    try:
        if startup_error is not None:
            raise startup_error
        checks = inspect_ros(args)
    except Exception as exc:
        checks = [check("ROS 现场观测", None, f"{type(exc).__name__}: {exc}")]
    checks.append(web_status())
    # This observer neither commissions a controller nor issues motion authorization.
    blocked = any(c["ok"] is False and c["required"] for c in checks)
    return {"schema": "edge.calibration.motion-preflight.v1", "task_id": args.task_id,
            "robot_id": args.robot_id, "status": "blocked" if blocked else "unavailable",
            "observed_at_ns": str(time.time_ns()), "valid_for_ms": 9000,
            "checks": [{k: c[k] for k in ("label", "ok", "detail")} for c in checks], "last_test": None}


def write_report(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix="."+path.name+".",
                                     delete=False, encoding="utf-8") as file:
        temporary = Path(file.name)
        try:
            json.dump(data, file, ensure_ascii=False, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            file.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--robot-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--odom", default="/lingao/odom")
    parser.add_argument("--direction", choices=("forward","reverse","both"), default="both")
    parser.add_argument("--front-depth", default="/camera/camera/aligned_depth_to_color/image_raw")
    parser.add_argument("--front-depth-frame", default="camera_color_optical_frame")
    parser.add_argument("--rear", default="/livox/lidar_192_168_123_211")
    parser.add_argument("--passive-rear", default="/edge/calibration/passive/rear/points")
    parser.add_argument("--passive-rear-frame", default="rear_mid360")
    parser.add_argument("--obstacles", default="/obstacle_scan")
    parser.add_argument("--base", default="base_foot")
    parser.add_argument("--watch", action="store_true", help="Refresh observations until interrupted.")
    args = parser.parse_args()
    # Defaults are the deployed Lingao host's DDS domain. Explicit ROS environment takes precedence.
    os.environ.setdefault("ROS_DOMAIN_ID", "61")
    os.environ.setdefault("ROS_LOCALHOST_ONLY", "1")
    startup_error = None
    try:
        import rclpy
        from rclpy.signals import SignalHandlerOptions
        # The process owns a single ROS context; the service manager owns SIGTERM.
        rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    except (ImportError, RuntimeError) as exc:
        startup_error = exc
    try:
        while True:
            write_report(args.out, snapshot(args, startup_error))
            if not args.watch:
                break
            time.sleep(1.0)
    finally:
        if startup_error is None:
            rclpy.shutdown()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
