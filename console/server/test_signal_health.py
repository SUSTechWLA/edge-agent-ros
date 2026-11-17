"""Run in an isolated ROS domain. Exercise real topic delivery, silence and recovery."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import rclpy
from nav_msgs.msg import Odometry
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import String

from observer import Observer


class SignalHealthTest(unittest.TestCase):
    def test_pose_and_status_disconnect_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ,{"EDGE_ENVIRONMENT":"hardware","EDGE_CONSOLE_DIR":directory,
                                        "EDGE_LEDGER_PATH":str(root/"unused_core.db"),
                                        "EDGE_TASK_DB":str(root/"unused_agent.db")}):
                rclpy.init()
                observer = Observer()
                publisher = Node("health_fixture")
                executor = SingleThreadedExecutor()
                executor.add_node(observer)
                try:
                    self.exercise(observer,publisher,executor)
                finally:
                    executor.shutdown()
                    publisher.destroy_node()
                    observer.destroy_node()
                    observer.history.close()
                    rclpy.shutdown()

    def exercise(self, observer, publisher, executor):
        pose = publisher.create_publisher(Odometry,"/edge/perception/pose",qos_profile_sensor_data)
        status = publisher.create_publisher(String,"/edge/perception/status",qos_profile_sensor_data)
        truth = publisher.create_publisher(Odometry,"/ground_truth",qos_profile_sensor_data)
        sample = Odometry()
        sample.header.frame_id = "odom"
        sample.child_frame_id = "base_link"
        sample.header.stamp.sec = 100
        sample.pose.pose.orientation.w = 1.0
        mode = String(data='{"mode":"wheel_imu","reason":"wheel and IMU available"}')

        def until(predicate, publish, timeout):
            end = time.monotonic()+timeout
            while time.monotonic() < end:
                publish()
                executor.spin_once(timeout_sec=0.03)
                if predicate():
                    return
                time.sleep(0.025)
            self.fail("timed out waiting for ROS health transition")

        def all_inputs():
            pose.publish(sample)
            status.publish(mode)
            truth.publish(sample)

        until(lambda:observer.health.get("pose") and observer.health.get("perception"), all_inputs, 10)
        self.assertEqual(truth.get_subscription_count(),0)
        self.assertFalse(observer.snapshot()["sources"]["truth"]["available"])

        # Status publication stops. Pose packets keep arriving with a frozen sensor stamp.
        until(lambda:observer.health.get("pose") is False and observer.health.get("perception") is False,
              lambda:pose.publish(sample), 9)
        stale = observer.snapshot()["sources"]
        self.assertLess(stale["pose"]["receive_age_s"],0.3)
        self.assertGreater(stale["pose"]["age_s"],stale["pose"]["ttl_s"])
        self.assertGreater(stale["perception"]["receive_age_s"],stale["perception"]["ttl_s"])
        sample.header.stamp.sec += 10
        until(lambda:observer.health.get("pose") and observer.health.get("perception"), all_inputs, 4)
        exported = [json.loads(line) for line in observer.history.export({},include_snapshot=True)]
        for key, topic in (("pose","/edge/perception/pose"),("perception","/edge/perception/status")):
            chain = [e for e in exported if e["source"]==topic and e["kind"].startswith("source.")]
            self.assertEqual([e["kind"] for e in chain],["source.online","source.stale","source.online"])
            self.assertEqual([e["cause_seq"] for e in chain],[None,chain[0]["seq"],chain[1]["seq"]])
            self.assertEqual([e["snapshot"]["sources"][key]["fresh"] for e in chain],[True,False,True])
            print(f"{topic} stale age={chain[1]['data']['age_s']}s "
                  f"receive_age={chain[1]['data']['receive_age_s']}s "
                  f"ttl={chain[1]['data']['ttl_s']}s seq={[e['seq'] for e in chain]}",flush=True)
        inactive = next(e for e in exported if e["kind"]=="perception.mode" and e["data"]["mode"] is None)
        self.assertIn("超时",inactive["detail"])


if __name__ == "__main__":
    unittest.main()
