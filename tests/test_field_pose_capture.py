"""Pose observations must remain measured in their original frame and clock."""
import math
from pathlib import Path
import sys
from types import SimpleNamespace as N
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "perception/rover_plan_field"))
from pose_capture import map_transform, measured


class FieldPoseTests(unittest.TestCase):
    def setUp(self):
        self.site = N(map_frame="map", odom_frame="odom", base_frame="base_foot", ttl_ms=500)
        self.header = N(stamp=N(sec=7, nanosec=100), frame_id="odom")
        self.point = N(x=1., y=-2., z=.05)
        self.rotation = N(x=0., y=0., z=math.sin(.15), w=math.cos(.15))
        self.msg = N(header=self.header, child_frame_id="base_foot",
                     pose=N(pose=N(position=self.point, orientation=self.rotation), covariance=list(range(36))),
                     twist=N(twist=N(linear=N(x=.03,y=.04,z=0.), angular=N(x=0.,y=0.,z=-.1))))

    def test_retains_wheel_pose_and_sample_time(self):
        sample=measured(self.msg,self.site,7_100_000_100)
        self.assertEqual((sample["frame_id"],sample["child_frame_id"]),("odom","base_foot"))
        self.assertEqual(sample["stamp_ns"],"7000000100")
        self.assertEqual(sample["position_m"],[1.,-2.,.05])
        self.assertAlmostEqual(sample["yaw_rad"],.3)
        self.assertEqual((sample["linear_mps"],sample["angular_rps"]),(.05,-.1))
        self.assertEqual(sample["planar_covariance"],[0,1,5,6,7,11,30,31,35])

    def test_stale_wrong_frame_and_invalid_quaternion_rejected(self):
        with self.assertRaises(ValueError):
            measured(self.msg,self.site,8_000_000_100)
        self.msg.child_frame_id="another_robot"
        with self.assertRaises(ValueError):
            measured(self.msg,self.site,7_000_000_100)
        self.msg.child_frame_id="base_foot"
        self.rotation.w=.1
        with self.assertRaises(ValueError):
            measured(self.msg,self.site,7_000_000_100)

    def test_map_transform_requires_direction_and_stamp(self):
        msg=N(header=N(stamp=self.header.stamp,frame_id="map"),child_frame_id="odom",
              transform=N(translation=self.point,rotation=self.rotation))
        sample=measured(self.msg,self.site,7_010_000_100)
        result=map_transform(msg,self.site,sample)
        self.assertEqual((result["parent_frame"],result["child_frame"]),("map","odom"))
        self.assertEqual(result["lookup_stamp_ns"],sample["stamp_ns"])
        msg.header.frame_id="odom"
        with self.assertRaises(ValueError):
            map_transform(msg,self.site,sample)


if __name__ == "__main__":
    unittest.main()
