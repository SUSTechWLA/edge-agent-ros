"""SDD-049 status compatibility. Run inside the ROS application image on an isolated DDS domain."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

import rclpy
from nav_msgs.msg import Odometry
from rclpy.qos import qos_profile_sensor_data

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import service
from kernel.worker import worker


class StatusCompatibility(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, {"EDGE_PLUGIN_DIR":directory.name, "EDGE_EXTRA_PLUGIN_DIR":"",
                                     "EDGE_SOFT_SOCKET":"", "EDGE_PHYSICAL_SOCKET":"",
                                     "EDGE_CONTRACT_DIR":directory.name, "EDGE_RETRY_LIMIT":"0"})
        env.start()
        self.addCleanup(env.stop)
        rclpy.init()
        self.addCleanup(rclpy.shutdown)
        self.robot = service.Robot()
        self.addCleanup(self.robot.destroy_node)
        self.source = rclpy.create_node("status_odometry_fixture")
        self.addCleanup(self.source.destroy_node)
        self.publisher = self.source.create_publisher(Odometry, "/odom", qos_profile_sensor_data)

    def start_agent_api(self):
        self.journal = service.Journal(self.root)
        self.addCleanup(self.journal.owner.close)
        self.addCleanup(self.journal.db.close)
        server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        server.journal = self.journal
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval":0.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}/agent/tasks"

    def publish_sample(self):
        msg = Odometry()
        msg.header.stamp.sec, msg.header.stamp.nanosec = 17, 1234
        msg.header.frame_id = "odom"
        msg.pose.pose.position.x, msg.pose.pose.position.y = 1.25, -2.5
        msg.pose.pose.orientation.w = 1.
        msg.twist.twist.linear.x = 0.125
        deadline = time.monotonic() + 5
        while self.robot.sample is None:
            self.assertLess(time.monotonic(), deadline, "Agent did not receive the fixture's ROS sample")
            self.publisher.publish(msg)
            rclpy.spin_once(self.robot, timeout_sec=0.05)
        subscriptions = self.robot.get_subscriptions_info_by_topic("/odom")
        self.assertEqual([(info.node_name, info.topic_type) for info in subscriptions],
                         [(self.robot.get_name(), "nav_msgs/msg/Odometry")])

    def dispatch(self, planning):
        # Drive one production queue iteration deterministically, including the SQLite transaction.
        keep_running = iter((True, False)).__next__
        worker(self.journal, self.robot, planning, keep_running=keep_running, plan_task=service.task_plan,
               registry=service.registry, services=service.runtime_plugins)

    def submit(self, key, text):
        request = Request(self.url, json.dumps({"id":key, "text":text}).encode(),
                          {"Content-Type":"application/json"})
        with urlopen(request, timeout=3) as reply:
            self.assertEqual(reply.status, 202)
        self.dispatch(planning=True)
        self.dispatch(planning=False)

    def read(self, key):
        with urlopen(self.url + "?id=" + key, timeout=3) as reply:
            raw = reply.read()
        return json.loads(raw), raw

    def assert_receipt(self, key):
        row, raw = self.read(key)
        self.assertEqual((row["state"], row["provider"]), ("SUCCEEDED", "local"))
        step, = row["steps"]
        self.assertEqual((step["tool"], step["version"], step["args"], step["operation_id"], step["status"]),
                         ("status", "1", {}, key + "/step/1", "SUCCEEDED"))
        # Frozen byte representation of the legacy wire evidence with a fixed ROS stamp.
        receipt = (b'{"source": "/odom", "frame": "odom", "stamp_ns": 17000001234, '
                   b'"stamp": {"sec": 17, "nanosec": 1234}, "x": 1.25, "y": -2.5, "yaw": 0.0, "speed": 0.125}')
        self.assertEqual(step["evidence"], json.loads(receipt))
        self.assertIn(b'"evidence": ' + receipt, raw)
        self.assertEqual([e["operation_id"] for e in row["events"] if e["kind"] == "dispatch"],
                         [key + "/step/1"])

    def test_existing_utterances_report_the_same_sample(self):
        self.start_agent_api()
        self.publish_sample()
        for index, text in enumerate(("status", "请查询一下机器人当前位置", "报告状态")):
            with self.subTest(text=text):
                key = f"query-{index}"
                self.submit(key, text)
                self.assert_receipt(key)

    def test_missing_and_expired_odometry_keep_the_error_contract(self):
        self.start_agent_api()
        for key in ("missing", "expired"):
            if key == "expired":
                self.publish_sample()
                time.sleep(2.05)
            with self.subTest(key=key):
                self.submit(key, "状态")
                row, _ = self.read(key)
                self.assertEqual((row["state"], row["detail"]), ("RECOVERY_REQUIRED", "等待新鲜里程计"))
                self.assertNotIn("evidence", row["steps"][0])
                event, = [e for e in row["events"] if e["kind"] == "retry_exhausted"]
                error = json.loads(event["payload"])["diagnostic"]
                self.assertEqual((error["code"], error["origin"]), ("tool.odom_stale", "ros.odometry"))

    def test_literal_status_still_works_during_soft_host_outage(self):
        self.start_agent_api()
        self.publish_sample()
        with patch.dict(os.environ, {"EDGE_SOFT_SOCKET":str(self.root / "offline.sock")}):
            self.submit("offline", "status")
        self.assert_receipt("offline")

    def test_legacy_unbound_task_resumes_from_old_sqlite_schema(self):
        steps = [{"tool":"status", "args":{}, "version":"1", "status":"PENDING"}]
        with sqlite3.connect(self.root / "tasks.db") as db:
            db.execute("""CREATE TABLE tasks(id TEXT PRIMARY KEY, text TEXT, state TEXT, detail TEXT,
                       provider TEXT, steps TEXT, cursor INTEGER, retries INTEGER, due REAL, created TEXT, updated TEXT)""")
            db.execute("INSERT INTO tasks VALUES('old','status','RUNNING','','local',?,0,0,0,'then','then')",
                       (json.dumps(steps),))
        self.start_agent_api()
        self.publish_sample()
        self.dispatch(planning=False)
        self.assert_receipt("old")


if __name__ == "__main__":
    unittest.main()
