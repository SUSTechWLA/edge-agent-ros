from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "perception/rover_plan_field"))
import pose_window as window


class FieldWindowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = patch.dict(os.environ, {"EDGE_FIELD_AUDIT_DIR": tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.stamp = time.time_ns() - 50_000_000
        self.pose = {"schema_version": 1, "valid": True, "source_topic": "/lingao/odom",
                     "clock_id": "unix", "max_age_ms": 500, "ros_domain_id": 61, "epoch": "once",
                     "frame_id": "odom", "child_frame_id": "base_foot", "stamp_ns": str(self.stamp),
                     "x_m": 0.0, "y_m": 0.0, "z_m": 0.0, "yaw_rad": math.radians(179)}

    def test_measured_deltas_and_first_write_survive_retries(self):
        self.assertIsNone(window.get("new"))
        start = window.start("new",self.pose)
        self.assertEqual("pending", start["status"])
        self.assertEqual(start,window.start("new",None))
        final = window.finish("new",{**self.pose,"x_m":.03,"y_m":.04,"yaw_rad":math.radians(-179),
                                     "stamp_ns":str(self.stamp+2_000_000)})
        self.assertEqual("completed",final["status"])
        self.assertAlmostEqual(math.radians(2), final["delta"]["yaw_delta_rad"])
        self.assertAlmostEqual(.05, final["delta"]["displacement_m"])
        self.assertAlmostEqual(.002,final["delta"]["source_elapsed_s"])
        self.assertFalse(final["continuous_motion_verified"])
        self.assertEqual(final,window.finish("new",None))
        self.assertEqual(final,window.get("new"))

    def test_undetermined_and_incomparable_endpoints_remain_explicit(self):
        for case, change, reason in (("stale",{"stamp_ns":"1"},"boundary_pose_unavailable"),
                                     ("duplicate",{},"source_time_did_not_advance"),
                                     ("reset",{"epoch":"two"},"pose_reference_changed"),
                                     ("missing",{"valid":False,"reason":"no_sensor"},"boundary_pose_unavailable")):
            window.start(case,self.pose)
            result=window.finish(case,{**self.pose,**change})
            self.assertEqual("incomparable", result["status"])
            self.assertEqual(reason,result["reason"])
            self.assertIsNone(result["delta"])
        self.assertEqual("odometry_stale_at_boundary",window.get("stale")["end_pose"]["reason"])
        self.assertEqual("no_sensor",window.get("missing")["end_pose"]["reason"])
        self.assertIsNone(window.finish("late-open",self.pose)["start_pose"])

    def test_concurrent_duplicate_first_samples_cannot_be_replaced(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda n:window.start("simultaneous",{**self.pose,"x_m":n}),range(16)))
        first=window.get("simultaneous")
        self.assertTrue(all(first==r for r in results))
        self.assertIn(first["start_pose"]["pose"]["x_m"],range(16))


if __name__ == "__main__":
    unittest.main()
