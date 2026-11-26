"""Witness geometry uses the submitted SDF and its rendered vertices."""
import json
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET

from system.fixed_rover_calibration_witness import inspect_geometry
from system.fixed_rover_world import generate


class FixedWorldGeometryTest(unittest.TestCase):
    def test_panel_world_vertices_and_four_driven_wheels(self):
        root=Path(__file__).resolve().parents[1]
        model=root/"artifacts/visualizations/lingao-sensor-layout/model/lingao_mid360.urdf.xacro"
        profile=json.loads((root/"calibration/station/gazebo-fixed-rover.json").read_bytes())
        with tempfile.TemporaryDirectory() as tmp:
            sdf=Path(tmp)/"world.sdf"
            generate(sdf,model,profile)
            assets=lambda uri:(sdf.parent/uri.rsplit("/",1)[1]).read_bytes()
            facts=inspect_geometry(sdf,assets)
            self.assertTrue(facts["board_static"] and facts["rover_dynamic"])
            self.assertEqual(facts["board_world_z_min_m"],0)
            self.assertEqual(set(facts["wheel_radius_m"].values()),{.075})
            xml=ET.parse(sdf)
            pose=xml.findtext(".//model[@name='calibration_board']/pose")
            self.assertEqual(list(map(float,pose.split()[:3])),[0.,4.,.5])
            xml.find(".//model[@name='calibration_board']/link").append(ET.fromstring("<pose>0 0 .03 0 0 0</pose>"))
            xml.write(sdf)
            with self.assertRaisesRegex(ValueError,"ground target"):
                inspect_geometry(sdf,assets)
