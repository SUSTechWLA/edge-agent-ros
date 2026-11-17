import json
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scene"))
from layout import Layout, RouteError
from start_world import generate


class SceneContract(unittest.TestCase):
    def setUp(self):
        self.site = Layout(ROOT/"scene/campus.json")

    def test_door_crossings_and_blocked_segments(self):
        self.assertEqual(self.site.route((0,0),"庭院")[-3:],["door_in","door_out","garden"])
        self.assertIn("door_in",self.site.route((3.3,1.35),"室内工位"))
        self.assertFalse(self.site.clear((1,2),(2.7,2)))
        self.assertFalse(self.site.clear((1,0.74),(2.6,0.74)))
        self.assertTrue(self.site.clear((1,0),(2.6,0)))

    def test_unknown_inaccessible_and_unlocalized(self):
        for point,place,code in [((0,0),"火星","scene.place_unknown"),((0,0),"维修区","scene.place_inaccessible"),
                                 ((20,20),"工位","scene.no_route")]:
            with self.subTest(place=place), self.assertRaises(RouteError) as caught:
                self.site.route(point,place)
            self.assertEqual(caught.exception.code,code)

    def test_each_mapped_obstacle_has_matching_physical_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/"world.sdf"
            generate(ROOT/"sim/world.sdf",self.site,output)
            world=ElementTree.parse(output).getroot().find("world")
            for feature in self.site.data["features"]:
                model=world.find(f"model[@name='{feature['id']}']")
                xyz=list(map(float,model.findtext("pose").split()))
                self.assertEqual(xyz[:2],feature["xy"])
                size=list(map(float,model.findtext("link/collision/geometry/box/size").split()))
                self.assertEqual(size,feature["size"])

    def test_renamed_transition_and_ambiguous_place(self):
        data = self.site.data
        next(z for z in data["zones"] if z["kind"]=="transition")["id"]="lobby"
        data["nodes"][0]["aliases"].append("共享地点")
        data["nodes"][-1]["aliases"]=["共享地点"]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"alternate.json"
            path.write_text(json.dumps(data),encoding="utf-8")
            other=Layout(path)
            self.assertEqual(other.plan((1.8,0),"庭院")["start_zone"],"lobby")
            with self.assertRaises(RouteError) as caught:
                other.plan((0,0),"共享地点")
            self.assertEqual(caught.exception.code,"scene.place_ambiguous")
