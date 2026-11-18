"""ROS image test. Validate TF timestamps, clock epochs, stale input and source facts."""
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import rclpy
from builtin_interfaces.msg import Time
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import OccupancyGrid, Odometry
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import PointCloud2, PointField
from std_msgs.msg import String

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"perception"/"map3d"))
from provider import MapNode


class RosMap(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ, {"EDGE_MAP3D_JOURNAL":str(Path(tmp.name)/"map3d.sqlite"),
                                   "EDGE_MAP3D_ENV":"simulation", "EDGE_MAP3D_FRAME":"odom"})
        env.start()
        self.addCleanup(env.stop)
        rclpy.init()
        self.addCleanup(rclpy.try_shutdown)
        self.node=MapNode()
        self.addCleanup(self.node.destroy_node)
        self.addCleanup(self.node.journal.close)
        self.node.clock(Clock(clock=Time(sec=100)))

    def cloud(self, sec, frame, values=(1,0,.5,1,0,1.5)):
        msg=PointCloud2(width=2,height=1,point_step=12,row_step=24)
        msg.header.frame_id=frame
        msg.header.stamp=Time(sec=sec)
        msg.fields=[PointField(name=k,offset=4*i,datatype=PointField.FLOAT32,count=1) for i,k in enumerate("xyz")]
        msg.data=struct.pack("<ffffff",*values)
        self.node.cloud(msg)

    def test_registration_stale_reset_and_source_evidence(self):
        self.cloud(100,"unknown_sensor")
        view=self.node.snapshot()
        self.assertEqual(view["cloud"]["error"],"transform_unavailable")
        self.assertEqual(view["cloud"]["points"],[])
        tf=TransformStamped()
        tf.header.frame_id="odom"
        tf.child_frame_id="lidar"
        tf.transform.rotation.w=1.
        tf.transform.translation.z=2.
        self.node.buffer.set_transform_static(tf,"fixture")
        self.cloud(100,"lidar")
        view=self.node.snapshot()
        self.assertEqual(view["cloud"]["points"],[[1,0,2.5],[1,0,3.5]])
        self.assertEqual(view["cloud"]["status"],"online")
        before=time.monotonic()
        with patch("provider.time.monotonic",return_value=before+5):
            self.node.health()
            self.assertEqual(self.node.snapshot()["cloud"]["status"],"stale")
            self.cloud(100,"lidar")  # Repeated timestamps cannot refresh freshness.
            self.assertEqual(self.node.snapshot()["cloud"]["status"],"stale")
        self.cloud(110,"lidar")
        self.assertEqual(self.node.snapshot()["cloud"]["error"],"stamp_outside_window")
        self.node.clock(Clock(clock=Time(sec=5)))
        view=self.node.snapshot()
        self.assertEqual((view["generation"],view["cloud"]["points"],view["cloud"]["status"]),(2,[],"waiting"))
        self.cloud(100,"lidar")   # Old epoch still arriving after the reset.
        self.assertEqual(self.node.snapshot()["cloud"]["points"],[])
        self.cloud(5,"odom")
        self.assertEqual(self.node.snapshot()["cloud"]["points"],[[1,0,.5],[1,0,1.5]])
        facts=[json.loads(row[0]) for row in self.node.journal.db.execute("SELECT event FROM module_bus_events ORDER BY seq")]
        self.assertTrue(any(f["kind"]=="map3d.clock_reset" for f in facts))
        self.assertTrue(any(f["kind"]=="map3d.stale" and f["data"]["layer"]=="cloud" for f in facts))

    def test_empty_returns_only_refresh_packet_age_then_recover(self):
        with patch("provider.time.monotonic", return_value=500):
            self.cloud(100, "odom")
        for step in range(1, 21):
            with patch("provider.time.monotonic", return_value=500+step):
                self.node.clock(Clock(clock=Time(sec=100+step)))
                self.cloud(100+step, "odom", [float("nan")]*6)
                c = self.node.snapshot()["cloud"]
                self.assertEqual((c["error"], c["status"], c["sample_points"]), ("no_valid_returns", "error", 0))
                self.assertEqual((c["age_s"], c["packet_age_s"]), (step, 0))
                self.assertEqual((c["stamp_s"], c["packet_stamp_s"], len(c["points"])), (100, 100+step, 2))
        with patch("provider.time.monotonic", return_value=525):
            self.assertEqual(self.node.snapshot()["cloud"]["status"], "stale")
            self.node.clock(Clock(clock=Time(sec=121)))
            self.cloud(121, "odom")
            c = self.node.snapshot()["cloud"]
            self.assertEqual((c["status"], c["stamp_s"], c["age_s"]), ("online", 121, 0))
            self.assertNotIn("error", c)

    def test_organized_rgbd_padded_textured_cloud(self):
        # Vendor-style dense PCL XYZRGB has 32-byte aligned records. RGB is an ancillary field.
        width, height = 848, 480
        point = struct.pack("<fff4xI12x", 1.125, -2.125, 3.25, 0xFFE040)
        msg = PointCloud2(width=width, height=height, point_step=len(point), row_step=width*len(point))
        msg.header.frame_id = "odom"
        msg.header.stamp = Time(sec=100)
        msg.fields = [PointField(name=k, offset=i, datatype=PointField.FLOAT32, count=1)
                      for k,i in (("x",0), ("y",4), ("z",8), ("rgb",16))]
        msg.data = point * (width*height)
        self.assertEqual(len(msg.data), 13025280)
        self.node.cloud(msg)
        c = self.node.snapshot()["cloud"]
        self.assertEqual((c["status"], c["points"]), ("online", [[1.125,-2.125,3.25]]))
        self.assertGreater(c["sample_points"], 4000)
        self.assertLessEqual(c["sample_points"], 4096)

    def test_prior_and_pose_do_not_fabricate_a_cloud(self):
        msg=Odometry()
        msg.header.frame_id="wrong"
        msg.header.stamp=Time(sec=100)
        self.node.pose(msg)
        self.node.scene(String(data='{"frame":"wrong"}'))
        view=self.node.snapshot()
        self.assertEqual(view["pose"]["error"],"pose_frame_mismatch")
        self.assertEqual(view["scene"]["error"],"scene_frame_mismatch")
        msg.header.frame_id="odom"
        self.node.pose(msg)
        self.node.scene(String(data=json.dumps({"frame":"odom","zones":[],"features":[
            {"id":"roof","xy":[0,1],"base_z":2,"size":[1,2,.1]}]})))
        view=self.node.snapshot()
        self.assertEqual(view["scene"]["features"][0]["xyz"],[0,1,2])
        self.assertEqual(view["pose"]["status"],"online")
        self.assertEqual(view["cloud"]["points"],[])

    def test_grid_geometry_source_and_epoch(self):
        grid = OccupancyGrid()
        grid.header.frame_id = "odom"
        grid.header.stamp = Time(sec=100)
        grid.info.width, grid.info.height = 3, 2
        grid.info.resolution = 0.5
        origin = grid.info.origin
        origin.position.x, origin.position.y, origin.position.z = 7.0, -1.0, 2.0
        origin.orientation.z = origin.orientation.w = math.sqrt(.5)
        grid.data = [-1, 0, 23, 100, 0, -1]
        self.node.grid(grid)
        layer = self.node.snapshot()["grid"]
        self.assertEqual(layer["cells"], list(grid.data))
        self.assertEqual(layer["origin"][:2], [7, -1])
        self.assertAlmostEqual(layer["origin"][2], math.pi / 2)
        self.assertEqual((layer["origin_z"], layer["status"], layer["topic"]),
                         (2, "online", "/edge/perception/map"))
        with patch("provider.time.monotonic", return_value=time.monotonic()+6):
            self.node.grid(grid)
            self.assertEqual(self.node.snapshot()["grid"]["status"], "stale")
        for fault, expected in (("frame","grid_frame_mismatch"),("size","grid_size_invalid"),
                                ("orientation","grid_orientation_invalid"),("value","grid_value_invalid"),
                                ("resolution","grid_geometry_invalid"),("future","stamp_outside_window")):
            from copy import deepcopy
            bad = deepcopy(grid)
            if fault == "frame": bad.header.frame_id = "map"
            if fault == "size": bad.info.width = 1 << 20
            if fault == "orientation": bad.info.origin.orientation.x = .2
            if fault == "value": bad.data[0] = 101
            if fault == "resolution": bad.info.resolution = 0.0
            if fault == "future": bad.header.stamp = Time(sec=109)
            with self.subTest(fault=fault):
                self.node.grid(bad)
                self.assertEqual(self.node.snapshot()["grid"]["error"], expected)
        self.node.clock(Clock(clock=Time(sec=5)))
        self.assertEqual(self.node.snapshot()["grid"]["cells"], [])
        grid.header.stamp = Time(sec=5)
        self.node.grid(grid)
        self.assertEqual(self.node.snapshot()["grid"]["status"], "online")


if __name__=="__main__":
    unittest.main()
