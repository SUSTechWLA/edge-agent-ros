"""SDD-023: MID-360 wire layout, moving TF(t), hot-reload, and input isolation on ROS 2."""
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import rclpy
from builtin_interfaces.msg import Time
from geometry_msgs.msg import TransformStamped
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import PointCloud2, PointField

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"perception/map3d"),str(ROOT/"console/server")]
from provider import MapNode
from inputs import MAX_SOURCES
from observations import Manifest, preview


def mid360(sec=101,frame="livox_frame"):
    # Upstream xfer_format=0 uses XYZ float32, intensity float32, tag/line uint8 and a float64 timestamp.
    fields=[PointField(name=k,offset=i,datatype=t,count=1) for k,i,t in (
        ("x",0,7),("y",4,7),("z",8,7),("intensity",12,7),("tag",16,2),("line",17,2),("timestamp",18,8))]
    msg=PointCloud2(width=2,height=1,point_step=26,row_step=52,fields=fields,is_dense=True)
    msg.header.stamp=Time(sec=sec);msg.header.frame_id=frame
    msg.data=b"".join(struct.pack("<ffffBBd",1,2,z,55,0x10,line,sec*1e9+line*1e4) for z,line in ((1,0),(2,1)))
    return msg


class LidarInputs(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        self.catalog=root/"inputs";self.catalog.mkdir()
        env=patch.dict(os.environ,{"EDGE_MAP3D_INPUTS_DIR":str(self.catalog),"EDGE_MAP3D_ENV":"simulation",
                                  "EDGE_MAP3D_JOURNAL":str(root/"journal.sqlite")})
        env.start();self.addCleanup(env.stop)
        rclpy.init();self.addCleanup(rclpy.try_shutdown)
        self.node=MapNode()
        self.addCleanup(self.node.destroy_node);self.addCleanup(self.node.journal.close)
        self.node.clock(Clock(clock=Time(sec=102)))
        self.file=self.catalog/"livox-mid360.json"
        shutil.copyfile(ROOT/"perception/map3d/profiles/livox-mid360.json",self.file)
        self.cfg=json.loads(self.file.read_bytes())
        self.node.health()

    def current(self):
        return self.node.snapshot()["cloud_sources"]["livox-mid360"]

    def test_vendor_layout_missing_tf_and_interpolated_moving_mount(self):
        # An independent primary sensor already has its data in the map frame.
        self.node.cloud(mid360(frame="odom"))
        item=self.node.inputs["livox-mid360"]
        receive=self.node.input_callback("livox-mid360",item)
        receive(mid360())
        self.assertEqual((self.current()["error"],self.current()["points"]),("transform_unavailable",[]))
        self.assertEqual(len(self.node.snapshot()["cloud"]["points"]),2)

        tf=TransformStamped()
        tf.transform.rotation.w=1.;tf.header.frame_id="odom";tf.child_frame_id="base_link"
        for sec,x in ((100,10.),(102,14.)):
            tf.header.stamp=Time(sec=sec);tf.transform.translation.x=x
            self.node.buffer.set_transform(tf,"motion_fixture")
        tf=TransformStamped()
        tf.header.frame_id="base_link";tf.child_frame_id="livox_frame";tf.transform.rotation.w=1.
        tf.transform.translation.x=.3;tf.transform.translation.z=.8
        self.node.buffer.set_transform_static(tf,"calibration_fixture")
        receive(mid360())
        live=self.current()
        self.assertEqual((live["source_frame"],live["topic"],live["status"]),("livox_frame","/livox/lidar","online"))
        self.assertEqual(live["points"],[[13.3,2,1.8],[13.3,2,2.8]])
        receive(mid360(frame="borrowed"))
        self.assertEqual(self.current()["error"],"sensor_frame_mismatch")
        self.assertEqual(self.current()["points"],live["points"])
        self.assertEqual(self.node.snapshot()["cloud"]["status"],"online")
        # Same descriptor is usable by the Console's existing observation registry.
        self.assertFalse(Manifest.load(self.file).error)
        raw,truncated=preview(mid360())
        self.assertTrue(truncated)
        self.assertEqual((raw["width"],raw["point_step"],raw["fields"][-1]["name"]),(2,26,"timestamp"))

    def test_per_source_sampling_window_and_age(self):
        for ttl, sec, expected in ((.1, 100, "error"), (10, 97, "online")):
            with self.subTest(ttl=ttl):
                self.file.write_text(json.dumps(dict(self.cfg, ttl_s=ttl, expected_frame="odom")))
                self.node.health()
                self.node.input_callback("livox-mid360", self.node.inputs["livox-mid360"])(mid360(sec, "odom"))
                self.assertEqual(self.current()["status"], expected)
                if expected == "online":
                    self.assertGreaterEqual(self.current()["age_s"], 5)
                    self.node.clock(Clock(clock=Time(sec=110)))
                    self.assertEqual(self.current()["status"], "stale")
                else:
                    self.assertEqual(self.current()["error"], "stamp_outside_window")
        self.node.cloud(mid360(105, "odom"))
        self.assertEqual(self.node.snapshot()["cloud"]["error"], "stamp_outside_window")

    def test_reload_duplicate_old_callback_budget_and_removal(self):
        old=self.node.input_callback("livox-mid360",self.node.inputs["livox-mid360"])
        self.file.write_text("{")
        self.node.health()
        self.assertEqual(self.current()["error"],"manifest_invalid")
        self.file.write_text(json.dumps(dict(self.cfg,topic="/replacement",expected_frame="odom")))
        self.node.health()
        old(mid360(frame="odom"))
        self.assertEqual((self.current()["status"],self.current()["points"]),("waiting",[]))
        self.node.input_callback("livox-mid360",self.node.inputs["livox-mid360"])(mid360(frame="odom"))
        self.assertEqual(self.current()["status"],"online")
        other=self.catalog/"other.json"
        other.write_text(json.dumps(dict(self.cfg,id="other",topic="/replacement")))
        self.node.health()
        self.assertEqual(self.current()["error"],"topic_conflict")
        self.assertEqual(self.current()["points"],[])
        for i in range(MAX_SOURCES):
            (self.catalog/f"extra{i}.json").write_text(json.dumps(dict(self.cfg,id=f"extra{i}")))
        self.node.health()
        self.assertEqual(self.node.snapshot()["input_catalog_error"],"input_catalog_limit")
        self.assertFalse(self.node.inputs)
        for path in self.catalog.glob("*.json"):
            path.unlink()
        self.node.health()
        self.assertIsNone(self.node.snapshot()["input_catalog_error"])
        kinds=[json.loads(row[0])["kind"] for row in self.node.journal.db.execute("SELECT event FROM module_bus_events")]
        for kind in ("map3d.installed","map3d.removed","map3d.error","map3d.restored"):
            self.assertIn(kind,kinds)


if __name__=="__main__":
    unittest.main()
