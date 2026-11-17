"""Deployment readiness check. It never submits a task or publishes a velocity."""
import json
import sys
import time

import rclpy
from edge_interfaces.action import NavigateTo
from edge_interfaces.srv import SubmitMission, GetMission, CancelMission
from nav_msgs.msg import Odometry
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data


def main():
    rclpy.init()
    node = Node("edge_readiness")
    clients = {name: node.create_client(kind, "/edge/" + name) for name, kind in (
        ("submit_mission", SubmitMission), ("get_mission", GetMission), ("cancel_mission", CancelMission))}
    action = ActionClient(node, NavigateTo, "/edge/navigate_to")
    stamps = {"/odom": set(), "/ground_truth": set()}

    def on_sample(topic, msg):
        stamp = msg.header.stamp
        if len(stamps[topic]) < 2:
            stamps[topic].add((stamp.sec, stamp.nanosec))

    for topic in stamps:
        node.create_subscription(Odometry, topic, lambda msg, t=topic: on_sample(t, msg),
                                 qos_profile_sensor_data)
    deadline = time.monotonic() + 40
    try:
        while True:
            rclpy.spin_once(node, timeout_sec=0.1)
            checks = {name: client.service_is_ready() for name, client in clients.items()}
            checks["navigate_to"] = action.server_is_ready()
            checks.update({topic: len(samples) >= 2 for topic, samples in stamps.items()})
            if all(checks.values()) or time.monotonic() >= deadline:
                print(json.dumps(checks))
                return 0 if all(checks.values()) else 1
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
