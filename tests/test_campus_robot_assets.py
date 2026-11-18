"""Offline checks of the four-body asset, joint and sensor contract (SDD-030)."""
import importlib.util
import math
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET


MOD = Path(__file__).resolve().parents[1]/"scene"/"campus_large"/"robots"/"__init__.py"
spec = importlib.util.spec_from_file_location("campus_robot_models",MOD)
bodies = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bodies)


class CampusRobotAssets(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)
        self.world=ET.fromstring('<world><model name="edge_rover"><pose>0 0 .08 0 0 0</pose></model></world>')
        self.records=bodies.append_robots(self.world,self.path)

    def test_source_meshes_are_indexed_and_have_native_physical_scale(self):
        self.assertEqual({r["id"] for r in self.records},{"crazyflie","edge_rover","go1","g1"})
        self.assertEqual(len(self.world.findall("model")),4)
        for name in ("crazyflie","go1","g1"):
            model=self.world.find(f"model[@name='{name}']")
            self.assertEqual(model.findtext("link/sensor/topic"),f"/model/{name}/imu")
            points=[]
            count=0
            for visual in model.findall("link/visual"):
                uri=visual.findtext("geometry/mesh/uri")
                path=self.path/uri.removeprefix("file:///campus/")
                lines=path.read_text().splitlines()
                points.extend(tuple(map(float,line[2:].split())) for line in lines if line.startswith("v "))
                count+=sum(line.startswith("f ") for line in lines)
                self.assertTrue(any(line.startswith("vn ") for line in lines))
            self.assertGreater(count,1000)
            if name=="crazyflie":
                self.assertAlmostEqual(max(x for x,y,z in points)-min(x for x,y,z in points),.1055,places=3)
            self.assertTrue(all(math.isfinite(v) for p in points for v in p))

    def test_receivers_and_observations_are_isolated_per_model(self):
        for name in ("go1","g1"):
            model=self.world.find(f"model[@name='{name}']")
            physical={j.get("name") for j in model.findall("joint[@type='revolute']")}
            drivers=model.findall("plugin[@name='gz::sim::systems::JointPositionController']")
            self.assertEqual(physical,{p.findtext("joint_name") for p in drivers})
            self.assertEqual(len(drivers),4 if name=="go1" else 1)
            for p in drivers:
                self.assertEqual(p.findtext("topic"),f"/model/{name}/joint/{p.findtext('joint_name')}/cmd_pos")
            if name=="go1":
                for joint in model.findall("joint[@type='revolute']"):
                    lo=float(joint.findtext("axis/limit/lower"))
                    hi=float(joint.findtext("axis/limit/upper"))
                    self.assertLessEqual(lo,.15-.1)
                    self.assertGreaterEqual(hi,.65+.1)
            self.assertEqual(model.findtext("plugin[@name='gz::sim::systems::JointStatePublisher']/topic"),
                             f"/model/{name}/joint_state")
            self.assertEqual(model.findtext("joint[@name='bench_mount']/parent"),"world")
        craft=self.world.find("model[@name='crazyflie']")
        self.assertEqual(craft.findtext("plugin[@name='gz::sim::systems::VelocityControl']/topic"),
                         "/model/crazyflie/cmd_vel")
        self.assertEqual(craft.findtext("plugin[@name='gz::sim::systems::OdometryPublisher']/dimensions"),"3")


if __name__ == "__main__":
    unittest.main()
