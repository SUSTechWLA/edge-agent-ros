"""SDD-030 failure and replay cases of the actual campus actuator provider."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sqlite3
import sys
import threading
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scene/campus_large/robot_agent"))
try:
    from provider import Fleet, JOINTS
except ModuleNotFoundError as error:
    if error.name not in {"geometry_msgs","rclpy","gz"}:
        raise
    Fleet = None


@unittest.skipUnless(Fleet,"run in the ROS/Gazebo image")
class RobotFeedback(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE receipts(op TEXT PRIMARY KEY, robot TEXT, action TEXT, result TEXT)")
        self.node=SimpleNamespace(lock=threading.RLock(),db=self.db,poses={},joints={},sensors={},
                                  active={},flight=Mock(),commands={},scene="campus-test")
        self.node.readings=lambda key:Fleet.readings(self.node,key)

    def test_stale_encoder_denies_admission_with_fresh_imu(self):
        now=time.monotonic()
        self.node.joints["go1"]=(now-5.,100.,{j:.65 for j in JOINTS["go1"]})
        self.node.sensors["go1"]=(now,100.)
        result=Fleet.execute(self.node,"go1","伸腿","blocked-before-motion")
        self.assertEqual(result["error"]["code"],"robot.feedback_missing")
        self.assertFalse(self.node.active)
        self.assertEqual(self.db.execute("SELECT count(*) FROM receipts").fetchone()[0],0)

    def test_lost_altitude_stops_and_replay_returns_original_failure(self):
        now=time.monotonic()
        self.node.sensors["crazyflie"]=(now,3.)
        self.node.poses["crazyflie"]=(now,3.,[11.5,5.8,.08],[0,0,0,1])
        result=Fleet.execute(self.node,"crazyflie","起飞","flight-test")
        self.assertEqual(result["state"],"RUNNING")
        self.node.poses.clear()
        Fleet.tick(self.node)
        self.assertEqual(self.node.flight.publish.call_args.args[0].linear.z,0.)
        self.node.active["crazyflie"]["missing_since"]-=8.
        Fleet.tick(self.node)
        self.assertFalse(self.node.active)
        replay=Fleet.execute(self.node,"crazyflie","起飞","flight-test")
        self.assertEqual(replay["error"]["code"],"robot.feedback_missing")
        conflict=Fleet.execute(self.node,"crazyflie","降落","flight-test")
        self.assertEqual(conflict["error"]["code"],"robot.operation_conflict")
        self.assertEqual(self.db.execute("SELECT count(*) FROM receipts").fetchone()[0],1)


if __name__=="__main__":
    unittest.main()
