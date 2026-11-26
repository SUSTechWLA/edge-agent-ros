"""Stop-only deadline helper for a single 10 degree in-place left turn."""
import os
from pathlib import Path
import select
import signal
import sys
import time
from control_profile import read_profile

CONTROL, CONTROL_SHA = read_profile()
STOP = CONTROL["stop"]
ARM_LIMITS = {"forward-50cm": (5_600_000_000,5_800_000_000), "left-10deg": (1_950_000_000,2_150_000_000)}


def report(line):
    try:
        record = (line + "\n").encode("ascii", errors="replace")
        return os.write(1, record) == len(record)
    except (BlockingIOError, BrokenPipeError, OSError):
        return False


def matched(node, writer):
    all_names = node.get_node_names_and_namespaces()
    if all_names.count(("vehicle_web_remote", "/")) != 1:
        return False
    listeners = node.get_subscriptions_info_by_topic(STOP["topic"])
    target = [e for e in listeners if (e.node_name, e.node_namespace, e.topic_type) ==
              ("vehicle_web_remote", "/", STOP["type"])]
    if len(target) != 1:
        return False
    # Verify that the distinct physical-route writer is visible as well.
    sinks = node.get_publishers_info_by_topic(CONTROL["safe"]["topic"])
    if len(sinks) != 1 or (sinks[0].node_name, sinks[0].node_namespace, sinks[0].topic_type) != (
            "vehicle_web_remote", "/", CONTROL["safe"]["type"]):
        return False
    # Count all discovered readers, including any other observers. Otherwise
    # one incompatible required reader plus one matched observer could look ready.
    return writer.get_subscription_count() == len(listeners)


def stop_burst(writer, stop_msg, duration_type):
    end = time.monotonic() + .8
    sent = 0
    while time.monotonic() < end:
        try:
            writer.publish(stop_msg)
            sent += 1
        except Exception:
            pass
        time.sleep(.025)
    ack = "unknown"
    if sent and hasattr(writer, "wait_for_all_acked"):
        try:
            ack = str(bool(writer.wait_for_all_acked(duration_type(nanoseconds=200_000_000)))).lower()
        except Exception:
            pass
    report(f"STOP_SENT count={sent} dds_acked={ack}")
    return 0 if sent else 1


def run():
    min_arm_ns,max_arm_ns = ARM_LIMITS[sys.argv[1]]
    if (os.environ.get("ROS_DOMAIN_ID") != "61" or os.environ.get("ROS_LOCALHOST_ONLY") != "1"
            or "Jetson" not in Path("/proc/device-tree/model").read_bytes().decode(errors="replace")):
        raise RuntimeError("wrong_site_or_domain")
    from rclpy.duration import Duration
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    from rclpy.signals import SignalHandlerOptions
    from std_msgs.msg import String

    os.set_blocking(0, False)
    os.set_blocking(1, False)
    requested_exit = []
    def on_signal(sig, frame):
        requested_exit.append(sig)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node("field_brake_once_" + str(os.getpid()), enable_rosout=False,
                             start_parameter_services=False, use_global_arguments=False)
    writer = node.create_publisher(String, STOP["topic"], QoSProfile(depth=1,
                                    reliability=ReliabilityPolicy.RELIABLE,
                                    durability=DurabilityPolicy.VOLATILE))
    stop_msg = String(data=STOP["latch_value"])
    deadline = None
    try:
        discovery_end = time.monotonic() + 6
        while not matched(node, writer):
            if requested_exit or time.monotonic() >= discovery_end or select.select([0], [], [], .025)[0]:
                report("ERROR no_verified_stop_subscriber_or_premature_input")
                return 1
        if not report("READY " + CONTROL_SHA):
            return 1
        pending = b""
        arm_by = time.monotonic() + 60
        while not requested_exit:
            if deadline is not None:
                remaining = (deadline - time.monotonic_ns()) * 1e-9
                if remaining <= 0:
                    break
            else:
                remaining = arm_by - time.monotonic()
                if remaining <= 0:
                    report("ERROR arming_timeout")
                    return 1
            if not select.select([0], [], [], min(.02, remaining))[0]:
                continue
            data = os.read(0, 256)
            if not data:
                break
            pending += data
            if len(pending) > 128:
                raise ValueError("oversized_control_record")
            while b"\n" in pending:
                record, pending = pending.split(b"\n", 1)
                if record == b"STOP":
                    return 0
                parts = record.split(b" ")
                if deadline is not None or len(parts) != 2 or parts[0] != b"ARM" or not parts[1].isdigit():
                    raise ValueError("invalid_or_replayed_control_record")
                candidate = int(parts[1])
                if not matched(node, writer):
                    raise ValueError("required_stop_subscriber_lost")
                if not min_arm_ns <= candidate - time.monotonic_ns() <= max_arm_ns:
                    raise ValueError("invalid_arm_deadline")
                deadline = candidate
                if not report(f"ARMED {deadline}"):
                    return 1
        return 0
    finally:
        # All exits after ARM converge here, including pipe loss and SIGTERM.
        try:
            if deadline is not None and stop_burst(writer, stop_msg, Duration):
                raise RuntimeError("no_stop_publication")
        finally:
            node.destroy_node()
            rclpy.shutdown()


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as exc:
        report("ERROR " + str(exc).replace("\n", " ")[:160])
        sys.exit(1)
