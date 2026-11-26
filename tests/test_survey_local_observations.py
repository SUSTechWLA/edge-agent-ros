from math import pi
from pathlib import Path
from struct import pack
from tempfile import TemporaryDirectory
from types import SimpleNamespace as Record
import unittest

from edge_contracts.spatial_facts import rotation
from exploration.local_observations import LocalObservations


WORLD = """<sdf><world><model name="edge_rover"><link name="base_link">
<sensor type="rgbd_camera"><pose>0.2 0 0.3 0 0 0</pose><camera><optical_frame_id>optical</optical_frame_id></camera><topic>/depth</topic></sensor>
<sensor><pose>0.1 0 0.2 0 0 0</pose><gz_frame_id>laser</gz_frame_id><topic>/scan</topic></sensor>
</link></model></world></sdf>"""


class LocalObservationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        world = Path(temp.name, "world.sdf")
        world.write_text(WORLD)
        self.view = LocalObservations(world)
        self.now = 2_000_000_000
        self.view.poses.append((self.now, rotation((1,-1,.4),(0,0,0,1))))
        header = Record(frame_id="optical", stamp=Record(sec=2, nanosec=0))
        self.image = Record(header=header, width=2, height=2, encoding="32FC1", step=8,
                            is_bigendian=0, data=pack("<ffff",1,2,0,float("inf")))
        self.info = Record(header=header, width=2, height=2, k=[2,0,1,0,2,1,0,0,1])
        self.scan = dict(stamp_ns=self.now, frame="laser", ranges=[1., 2.], range_min=.1,
                         range_max=3., angle_min=0., angle_increment=pi/2)

    def test_depth_uses_optical_axes_and_scan_stays_in_its_plane(self):
        self.view.camera("depth", self.image)
        self.view.camera("info", self.info)
        scene = self.view.snapshot(self.now, [self.scan])
        self.assertEqual(scene["depth"]["points"], [[2.2,-.5,1.2],[3.2,-1,1.7]])
        self.assertEqual(scene["laser"]["points"], [[2.1,-1,.6],[1.1,1,.6]])
        self.assertEqual(scene["depth"]["pose_stamp_ns"], self.now)

    def test_require_frame_time_intrinsics_and_nearby_pose(self):
        self.view.camera("depth", self.image)
        self.info.header = Record(frame_id="optical", stamp=Record(sec=1,nanosec=0))
        self.view.camera("info", self.info)
        self.assertNotIn("depth", self.view.snapshot(self.now,[self.scan]))
        wrong_frame = Record(**{**vars(self.info), "header":Record(frame_id="another-camera", stamp=self.image.header.stamp)})
        self.view.camera("info", wrong_frame)
        self.assertNotIn("depth", self.view.snapshot(self.now,[self.scan]))
        self.assertNotIn("laser", self.view.snapshot(self.now+651_000_000,[self.scan]))
        self.assertNotIn("laser", self.view.snapshot(self.now,[{**self.scan,"stamp_ns":self.now-61_000_000}]))
        self.assertNotIn("laser", self.view.snapshot(self.now,[{**self.scan,"frame":"unknown"}]))
