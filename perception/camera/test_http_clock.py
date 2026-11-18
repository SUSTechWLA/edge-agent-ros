"""Exercise the real HTTP read path before the ROS maintenance timer has a turn."""
import json
import threading
import unittest
from unittest.mock import patch

from frames import Source
from provider import CameraNode
from test_frames import SPEC, image


class ControlledClockCamera(CameraNode):
    """Use the normal source transitions with a deterministic, independently advancing clock."""
    def __init__(self):
        self.sources = {}
        self.ros_time = self.ros_ns = 1_000_000_000
        self.lock = threading.RLock()
        self.config_sha = "test-config"
        self.config_status = "ok"
        self.events = []
        for name in ("front", "rear"):
            source = Source({**SPEC, "id": name})
            source.event = name + "-added"
            source.observe(image(), 10.0, self.ros_ns)
            self.sources[name] = source

    def clock_ns(self):
        return self.ros_ns

    def fact(self, stage, data, parent):
        self.events.append((stage, data.copy(), parent))
        return str(len(self.events))


class ReadTimeFreshness(unittest.TestCase):
    def setUp(self):
        self.node = ControlledClockCamera()

    def read(self, path, arrival):
        with patch("provider.time.monotonic", return_value=arrival):
            status, mime, data, _ = self.node.response(path)
        return status, json.loads(data) if mime == "application/json" else data

    def test_forward_jump_first_image_read_rejects_then_recovers(self):
        self.assertEqual(self.read("/api/cameras/front/image",10.1)[0], 200)
        self.node.ros_ns = 5_000_000_000
        status, body = self.read("/api/cameras/front/image",10.2)
        self.assertEqual((status,body["state"],body["reason"]), (503,"stale","frame_timeout"))
        rows = self.read("/api/cameras",10.2)[1]["sources"]
        self.assertTrue(all(row["state"] == "stale" for row in rows))
        self.assertEqual([e[1]["source_id"] for e in self.node.events], ["front","rear"])
        sample = image()
        sample.header.stamp.sec = 5
        with patch("provider.time.monotonic", return_value=10.3):
            self.node.callback(self.node.sources["front"])(sample)
        self.assertEqual(self.read("/api/cameras/front/image",10.3)[0], 200)
        self.assertEqual(self.node.events[-1][1]["state"], "online")

    def test_paused_ros_time_catalog_read_checks_monotonic_age(self):
        rows = self.read("/api/cameras",11.1)[1]["sources"]
        self.assertEqual([row["state"] for row in rows], ["stale","stale"])
        self.assertEqual(self.read("/api/cameras/front/image",11.1)[0], 503)
        self.assertEqual(len(self.node.events), 2)

    def test_rewind_first_catalog_read_revokes_all_old_epoch_pixels(self):
        self.node.ros_ns = 500_000_000
        rows = self.read("/api/cameras",10.1)[1]["sources"]
        self.assertTrue(all((row["state"], row["reason"]) == ("waiting","clock_reset") for row in rows))
        self.assertTrue(all("stamp_ns" not in row for row in rows))
        self.assertEqual(self.read("/api/cameras/rear/image",10.1)[0], 503)
        self.assertEqual(len(self.node.events), 2)


if __name__ == "__main__":
    unittest.main()
