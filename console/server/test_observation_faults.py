"""Drive the installed ROS nodes via DDS; read the same archive/API as the workbench."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

import rclpy
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import LaserScan, Range
from std_msgs.msg import String

from main import Handler
from observer import Observer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "perception"))
from node import Perception


class ObservationFaults(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        (base / "range.json").write_text(json.dumps({
            "id": "range", "topic": "/fixture/range", "type": "sensor_msgs/msg/Range", "ttl_s": 4,
        }))
        env = patch.dict(os.environ, {
            "EDGE_CONSOLE_DIR": folder.name, "EDGE_OBSERVATIONS_DIR": folder.name,
            "EDGE_ENVIRONMENT": "simulation", "EDGE_LEDGER_PATH": str(base / "no-core.db"),
            "EDGE_TASK_DB": str(base / "no-agent.db"),
        })
        env.start()
        self.addCleanup(env.stop)
        rclpy.init()
        self.addCleanup(rclpy.shutdown)
        self.observer = Observer()
        self.addCleanup(self.observer.history.close)
        self.addCleanup(self.observer.destroy_node)
        self.node = Node("sensor_fixture")
        self.addCleanup(self.node.destroy_node)
        self.executor = SingleThreadedExecutor()
        self.addCleanup(self.executor.shutdown)
        self.executor.add_node(self.observer)
        self.executor.add_node(self.node)
        http = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, observer=self.observer))
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        self.url = f"http://127.0.0.1:{http.server_port}"
        self.publishers = {}

    def fusion(self):
        node = Perception()
        self.addCleanup(node.destroy_node)
        self.executor.add_node(node)
        return node

    def get(self, path):
        with urlopen(self.url + path, timeout=2) as response:
            return json.load(response)

    def until(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("timed out waiting for DDS or a source transition")
            self.executor.spin_once(timeout_sec=.025)

    def publish(self, topic, msg, subscribers=1):
        if topic not in self.publishers:
            self.publishers[topic] = self.node.create_publisher(type(msg), topic, qos_profile_sensor_data)
        pub = self.publishers[topic]
        self.until(lambda: pub.get_subscription_count() >= subscribers)
        pub.publish(msg)

    @staticmethod
    def odom(sec, x):
        msg = Odometry()
        msg.header.stamp.sec = sec
        msg.header.frame_id = "odom"
        msg.child_frame_id = "base_link"
        msg.pose.pose.orientation.w = 1.
        msg.pose.pose.position.x = x
        return msg

    @staticmethod
    def scan(sec, values):
        msg = LaserScan()
        msg.header.stamp.sec = sec
        msg.header.frame_id = "laser_link"
        msg.range_min, msg.range_max, msg.angle_increment = .1, 4., .1
        msg.ranges = values
        return msg

    def seed(self, fusion):
        clock = Clock()
        clock.clock.sec = 80
        self.publish("/clock", clock, 2)
        self.until(lambda: self.observer.sim_clock == 80 and fusion.last_clock == 80)
        self.publish("/odom", self.odom(80, 2.), 2)
        self.until(lambda: self.observer.sources.get("pose") is not None)
        self.publish("/scan", self.scan(80, [1., 1.]), 2)
        self.until(lambda: self.observer.sources.get("map") is not None)
        self.assertIn(100, self.observer.sources["map"]["data"]["cells"])

    def test_invalid_scan_keeps_the_original_capture_time(self):
        fusion = self.fusion()
        self.seed(fusion)
        odom = self.odom(80, 2.)
        odom.header.stamp.nanosec = 100_000_000
        self.publish("/odom", odom)
        bad = self.scan(80, [float("nan"), float("-inf"), .01, 15.])
        bad.header.stamp.nanosec = 100_000_000
        self.publish("/scan", bad)
        self.until(lambda: fusion.matched + fusion.dropped >= 2)
        self.assertEqual((fusion.matched, fusion.dropped), (1, 1))
        self.assertEqual(fusion.mapped_stamp.nanosec, 0)
        self.assertIn("evidence", fusion.scan_reason)
        self.assertEqual(self.get("/api/state")["sources"]["scan"]["data"]["invalid"], 2)
        self.assertIn(100, self.get("/api/state")["sources"]["map"]["data"]["cells"])
        wrong_frame = self.scan(80, [1.])
        wrong_frame.header.stamp.nanosec = 150_000_000
        wrong_frame.header.frame_id = "unregistered_laser"
        self.publish("/scan", wrong_frame)
        self.until(lambda: fusion.dropped == 2)
        delayed = self.scan(80, [1., float("inf")])
        delayed.header.stamp.nanosec = 200_000_000
        self.publish("/scan", delayed)
        self.until(lambda: fusion.pending)
        self.assertEqual(fusion.matched, 1)
        odom.header.stamp.nanosec = 225_000_000
        self.publish("/odom", odom)
        self.until(lambda: self.observer.snapshot()["sources"]["map"]["stamp_s"] == 80.2)
        self.assertEqual((fusion.matched, fusion.dropped), (2, 2))

    def test_clock_rewind_clears_live_inputs_and_preserves_prior_replay(self):
        fusion = self.fusion()
        self.seed(fusion)
        sample = Range()
        sample.header.stamp.sec, sample.range = 80, 1.25
        self.publish("/fixture/range", sample)
        self.until(lambda: self.observer.snapshot()["extra_sources"]["range"]["available"])
        self.observer.tick()
        before = self.observer.event("fixture.before_rewind", "fixture", "map captured")
        clock = Clock()
        clock.clock.sec = 2
        self.publish("/clock", clock)
        self.until(lambda: self.observer.sim_clock == 2 and fusion.last_clock == 2)
        frame = self.get("/api/state")
        self.assertFalse(frame["sources"]["pose"]["available"])
        self.assertFalse(frame["extra_sources"]["range"]["available"])
        self.assertEqual(frame["trail"], [])
        prev = self.get(f"/api/replay?at={before}")
        self.assertEqual(prev["sources"]["pose"]["stamp_s"], 80)
        self.assertIn(100, prev["sources"]["map"]["data"]["cells"])
        # The last map is transient-local. A late joiner must see the reset.
        retained = []
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.node.create_subscription(OccupancyGrid, "/edge/perception/map", retained.append, qos)
        self.until(lambda: retained)
        self.assertEqual(retained[-1].header.stamp.sec, 2)
        self.assertEqual(set(retained[-1].data), {-1})
        # A queued old packet arrives after rewind, before valid data from the new run.
        self.publish("/odom", self.odom(80, 9.))
        self.publish("/fixture/range", sample)
        self.publish("/odom", self.odom(2, -.5))
        self.until(lambda: self.observer.snapshot()["sources"]["pose"]["stamp_s"] == 2)
        self.assertEqual(self.get("/api/state")["trail"][0]["x"], -.5)
        self.assertFalse(self.get("/api/state")["extra_sources"]["range"]["available"])
        sample.header.stamp.sec = 2
        self.publish("/fixture/range", sample)
        self.until(lambda: self.observer.snapshot()["extra_sources"]["range"]["stamp_s"] == 2)
        self.assertEqual(len(self.get("/api/events?kind=sensor.clock_reset")["events"]), 1)

    def test_malformed_status_cannot_break_state_and_event_archive(self):
        topic = "/edge/perception/status"
        self.publish(topic, String(data='{"mode":"wheel_only"}'))
        self.until(lambda: "perception" in self.observer.sources)
        for text in ('{"mode":"bad","residual":NaN}', '{"nested":[1e999]}'):
            # Fence on actual DDS receipt; the second rejection may be rate-limited.
            with patch.object(self.observer, "data_for", wraps=self.observer.data_for) as received:
                self.publish(topic, String(data=text))
                self.until(lambda: any(args.args[0]=="perception" for args in received.call_args_list))
            self.assertEqual(self.get("/api/state")["sources"]["perception"]["data"], {"mode":"wheel_only"})
            # A subsequent packet on the same DDS writer fences prior deliveries.
            self.publish(topic, String(data='{"mode":"recovered"}'))
            self.until(lambda: self.observer.sources["perception"]["data"].get("mode") == "recovered")
            self.assertEqual(self.get("/api/state")["sources"]["perception"]["data"], {"mode":"recovered"})
            self.publish(topic, String(data='{"mode":"wheel_only"}'))
            self.until(lambda: self.observer.sources["perception"]["data"].get("mode") == "wheel_only")
        warnings = self.get("/api/events?kind=source.invalid")["events"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["source"], topic)
        self.assertEqual(self.get(f"/api/replay?at={warnings[0]['seq']}")["sources"]["perception"]["data"],
                         {"mode": "wheel_only"})


if __name__ == "__main__":
    unittest.main()
