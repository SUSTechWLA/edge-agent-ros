"""ROS provider decisions, with deterministic service responses for lost-ledger faults."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scene"))
from provider import Scene
from nav_msgs.msg import Odometry
from rosgraph_msgs.msg import Clock


class SceneReconciliation(unittest.TestCase):
    def setUp(self):
        self.scene = Scene.__new__(Scene)
        import threading
        self.scene.lock = threading.Lock()
        self.scene.db = sqlite3.connect(":memory:")
        self.addCleanup(self.scene.db.close)
        self.scene.db.executescript("CREATE TABLE routes(op, place, plan, binding);"
                                   "CREATE TABLE decisions(op, place, body, refused);")
        plan = {"waypoints":[[0, 0], [1, 2]]}
        self.scene.db.execute("INSERT INTO routes VALUES('op','garden',?,'bound')", (json.dumps(plan),))
        self.scene.get_mission = Mock()
        self.scene.submit_mission = Mock()
        self.scene.call = Mock()
        self.scene.layout = SimpleNamespace(data={"frame":"odom"})
        self.scene.sim_time_ns = self.scene.position = None

    def test_lost_core_record_requires_reconciliation_without_resubmission(self):
        self.scene.call.return_value = SimpleNamespace(found=False)
        reply = self.scene.navigate("op", "garden", {})
        self.assertEqual((reply["state"], reply["error"]["code"]),
                         ("RECOVERY_REQUIRED", "scene.mission_unaccounted"))
        self.assertEqual(self.scene.call.call_count, 1)
        self.assertIs(self.scene.call.call_args.args[0], self.scene.get_mission)

    def test_success_needs_all_positions_and_correct_order(self):
        pt = lambda x, y: SimpleNamespace(x=x,y=y)
        for evidence in ([], [pt(0,0)], [pt(1,2),pt(0,0)], [pt(0,0),pt(float("nan"),2)]):
            with self.subTest(evidence=evidence):
                self.scene.call.return_value = SimpleNamespace(
                    found=True, state="SUCCEEDED", verified_count=len(evidence), evidence=evidence)
                reply = self.scene.navigate("op","garden",{})
                self.assertEqual(reply["error"]["code"],"scene.evidence_mismatch")

    def test_clock_reset_discards_old_pose_and_late_sample(self):
        def clock(sec):
            msg=Clock()
            msg.clock.sec=sec
            self.scene.observe_clock(msg)
        def pose(sec,x):
            msg=Odometry()
            msg.header.frame_id="odom"
            msg.header.stamp.sec=sec
            msg.pose.pose.position.x=x
            self.scene.observe(msg)
        clock(80)
        pose(80,1.)
        self.assertEqual(self.scene.sample(),(1.,0.))
        clock(2)
        pose(80,2.)
        self.assertIsNone(self.scene.sample())
        pose(2,0.)
        pose(1,9.)
        self.assertEqual(self.scene.sample(),(0.,0.))


if __name__ == "__main__":
    unittest.main()
