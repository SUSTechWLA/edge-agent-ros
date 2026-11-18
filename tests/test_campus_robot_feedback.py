"""SDD-030 failure and replay cases of the actual campus actuator provider."""
from pathlib import Path
import json
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
        self.node=SimpleNamespace(lock=threading.RLock(),db=self.db,poses={},joints={},sensors={},invalid_feedback={},
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

    def test_nonfinite_altitude_cannot_drive_or_break_other_robot_telemetry(self):
        def pose(key, z, w=1.):
            return SimpleNamespace(name=key,position=SimpleNamespace(x=0.,y=0.,z=z),
                                   orientation=SimpleNamespace(x=0.,y=0.,z=0.,w=w))
        def receive(*poses):
            header=SimpleNamespace(stamp=SimpleNamespace(sec=4,nsec=0))
            Fleet.on_poses(self.node,SimpleNamespace(header=header,pose=poses))
        self.node.overview_port=0
        self.node.telemetry=Mock()
        receive(pose("crazyflie",.1),pose("go1",.3))
        self.node.sensors["crazyflie"]=(time.monotonic(),4.)
        self.assertEqual(Fleet.execute(self.node,"crazyflie","起飞","nan-flight")["state"],"RUNNING")
        receive(pose("crazyflie",float("nan")))
        Fleet.tick(self.node)
        self.assertEqual(self.node.flight.publish.call_args.args[0].linear.z,0.)
        Fleet.emit_states(self.node)
        robots={r["id"]:r for r in json.loads(self.node.telemetry.publish.call_args.args[0].data)["robots"]}
        self.assertEqual(robots["crazyflie"]["status"],"waiting")
        self.assertEqual(robots["go1"]["status"],"online")
        self.node.active["crazyflie"]["missing_since"]-=8.
        Fleet.tick(self.node)
        receipt=Fleet.execute(self.node,"crazyflie","起飞","nan-flight")
        self.assertEqual(receipt["error"]["code"],"robot.feedback_missing")
        self.assertIn("pose",receipt["error"]["invalid_sources"])
        for w in (0.,float("inf")):
            receive(pose("crazyflie",.1,w))
            self.assertIsNone(self.node.readings("crazyflie"))
        receive(pose("crazyflie",.1))
        self.assertEqual(Fleet.execute(self.node,"crazyflie","起飞","after-feedback")["state"],"RUNNING")

    def test_invalid_joint_and_imu_immediately_revoke_recent_samples(self):
        header=SimpleNamespace(stamp=SimpleNamespace(sec=4,nanosec=0))
        def joints(key,names,values):
            Fleet.on_joints(self.node,key,SimpleNamespace(header=header,name=names,position=values))
        def imu(key,x):
            Fleet.on_imu(self.node,key,SimpleNamespace(header=header,linear_acceleration=SimpleNamespace(x=x,y=0.,z=0.)))
        names=JOINTS["go1"]
        imu("go1",0.)
        for bad_names,angles in ((names,[.3]*3),(names,[.3]*3+[float("inf")]),(names+[names[0]],[.3]*5)):
            joints("go1",names,[.3]*len(names))
            self.assertIsNotNone(self.node.readings("go1"))
            joints("go1",bad_names,angles)
            self.assertIsNone(self.node.readings("go1"))
        joints("g1",JOINTS["g1"],[0.])
        imu("g1",0.)
        self.assertIsNotNone(self.node.readings("g1"))
        imu("g1",float("nan"))
        self.assertEqual(Fleet.execute(self.node,"g1","放下左臂","bad-imu")["error"]["code"],"robot.feedback_missing")
        imu("g1",0.)
        self.assertIsNotNone(self.node.readings("g1"))


if __name__=="__main__":
    unittest.main()
