"""Run in an isolated Docker network after building edge_gateway; exercises ROS actions."""
import subprocess
import time
from pathlib import Path

import rclpy
from action_msgs.msg import GoalStatus
from ament_index_python.packages import get_package_prefix
from edge_interfaces.action import NavigateTo
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.action import ActionClient
from std_msgs.msg import String


class Probe:
    def __init__(self):
        self.node = rclpy.create_node("gateway_watchdog_probe")
        self.odom = self.node.create_publisher(Odometry, "/odom", 1)
        self.heartbeats = self.node.create_publisher(String, "/edge/active_operation", 1)
        self.velocity = self.node.create_subscription(Twist, "/cmd_vel", self.on_velocity, 1)
        self.client = ActionClient(self.node, NavigateTo, "/edge/navigate_to")
        self.operation = ""
        self.frame = "odom"
        self.observations = True
        self.speed = 0.0
        self.last_velocity = None
        self.node.create_timer(0.035, self.publish)

    def on_velocity(self, msg):
        self.last_velocity = msg.linear.x

    def publish(self):
        if self.operation:
            self.heartbeats.publish(String(data=self.operation))
        if self.observations:
            msg = Odometry()
            msg.header.stamp = self.node.get_clock().now().to_msg()
            msg.header.frame_id = self.frame
            msg.child_frame_id = "base_link"
            msg.pose.pose.orientation.w = 1.0
            msg.twist.twist.linear.x = self.speed
            self.odom.publish(msg)

    def until(self, check, seconds=4):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(self.node, timeout_sec=0.025)
            if check():
                return
        raise AssertionError(f"condition did not occur within {seconds}s")

    def spin_for(self, seconds):
        end = time.monotonic() + seconds
        self.until(lambda: time.monotonic() >= end, seconds + 1)

    def send(self, operation):
        future = self.client.send_goal_async(NavigateTo.Goal(operation_id=operation, x=1.0))
        self.until(future.done)
        return future.result()

    def start(self, operation):
        self.operation = operation
        handle = self.send(operation)
        assert handle.accepted, operation
        self.until(lambda: self.last_velocity is not None and self.last_velocity > 0.1)
        return handle

    def test(self):
        assert self.client.wait_for_server(timeout_sec=10)
        self.frame = "map"
        self.spin_for(0.6)
        assert not self.send("wrong-frame").accepted
        self.frame = "odom"
        self.spin_for(0.35)

        goal = self.start("cancel")
        assert not self.send("competing").accepted
        self.speed = 0.2
        self.spin_for(0.15)
        cancel = goal.cancel_goal_async()
        self.until(cancel.done)
        assert cancel.result().goals_canceling
        result = goal.get_result_async()
        self.until(lambda: self.last_velocity == 0.0)
        self.spin_for(0.3)
        assert not result.done(), "cancel acknowledgement must not imply physical stop"
        self.speed = 0.0
        self.spin_for(0.17)
        assert not result.done(), "brief quiet interval released the base"
        self.speed = 0.11
        self.spin_for(0.11)
        self.speed = 0.0
        self.spin_for(0.17)
        assert not result.done(), "rebound did not restart the settling interval"
        self.until(result.done)
        assert result.result().status == GoalStatus.STATUS_CANCELED
        assert "stationary" in result.result().result.detail
        assert not self.send("cancel").accepted, "old operation was reused"
        print("PASS frame validation, exclusivity, operation replay, physical cancel")

        goal = self.start("heartbeat-expiry")
        self.operation = ""
        result = goal.get_result_async()
        self.until(result.done)
        assert result.result().status == GoalStatus.STATUS_ABORTED
        assert "heartbeat expired" in result.result().result.detail
        assert self.last_velocity == 0.0
        print("PASS operation watchdog stopped motion and confirmed stationary samples")

        goal = self.start("lost-odometry")
        self.observations = False
        result = goal.get_result_async()
        self.until(lambda: self.last_velocity == 0.0, 2)
        self.until(result.done, 5)
        assert result.result().status == GoalStatus.STATUS_ABORTED
        assert result.result().result.detail.startswith("UNKNOWN:")
        self.observations = True
        self.spin_for(0.3)
        assert not self.send("after-unknown").accepted, "unconfirmed stop released the base"
        print("PASS odometry watchdog and unknown-outcome resource fence")


if __name__ == "__main__":
    executable = Path(get_package_prefix("edge_gateway"), "lib/edge_gateway/edge_gateway_node")
    gateway = subprocess.Popen([executable])
    try:
        rclpy.init()
        probe = Probe()
        probe.test()
    finally:
        gateway.terminate()
        gateway.wait(timeout=8)
        rclpy.try_shutdown()
