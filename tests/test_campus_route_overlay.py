"""The optional reference walk has the same physical registration in both map modes."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
dep = importlib.util.spec_from_file_location("campus_map_texture",ROOT/"perception/map3d/texture.py")
pack = importlib.util.module_from_spec(dep)
dep.loader.exec_module(pack)


class RegisteredWalk(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0,str(ROOT/"scene/campus_large"))
        self.addCleanup(sys.path.pop,0)
        from install_map import install_route
        self.install = install_route
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.world = Path(tmp.name)
        self.atlas = self.world/"texture"
        self.atlas.mkdir()
        self.overview = self.world/"overview"/"manifest.json"
        self.overview.parent.mkdir()
        sdf = b"""<sdf><world><model name="road"><link name="surface">
          <visual name="gate"><pose>2 1 0.04 0 0 0</pose><geometry><box><size>4 3 .01</size></box></geometry></visual>
          </link></model></world></sdf>"""
        (self.world/"world.sdf").write_bytes(sdf)
        (self.world/"layout.json").write_text('{"revision":"r1","frame":"odom"}')
        self.meta = dict(scene_revision="r1",frame="odom",bounds=[-1,-2,5,3],
                         world_sha256=hashlib.sha256(sdf).hexdigest())
        self.doc = {"frame":"odom","scene_revision":"r1","unit":"m","routes":[{
            "id":"campus_walk","nodes":[{"id":"start","xy":[0,1]},{"id":"end","xy":[4,1]}],
            "edges":[{"from":"start","to":"end","model":"road","visual":"gate"}]}]}
        (self.atlas/"manifest.json").write_text(json.dumps(self.meta))

    def published(self):
        self.overview.write_text(json.dumps(self.doc))
        self.install(self.world, self.atlas)
        return json.loads((self.atlas/"manifest.json").read_bytes())

    def test_walk_is_derived_from_the_registered_gazebo_edges(self):
        meta=self.published()
        self.assertEqual(pack.registered_route(meta["route"],meta),{
            "id":"campus_walk","scene_revision":"r1","frame":"odom","length_m":4.,"points":[[0,1],[4,1]]})
        self.doc["routes"][0]["nodes"][1]["xy"]=[4,1.1]
        self.assertNotIn("route",self.published())
        self.assertEqual(self.meta["world_sha256"],self.published()["world_sha256"])

    def test_source_frame_revision_graph_and_absent_route(self):
        for field,value in (("frame","map"),("scene_revision","stale"),("routes",[])):
            with self.subTest(field=field):
                saved=self.doc[field]
                self.doc[field]=value
                self.assertNotIn("route",self.published())
                self.doc[field]=saved
        self.doc["routes"][0]["edges"][0]["from"]="other"
        self.assertNotIn("route",self.published())

    def test_viewer_rechecks_budget_extent_length_and_identity(self):
        meta=self.published()
        for key,value in (("frame","base_link"),("scene_revision","old"),("id","../other"),
                          ("points",[[1.,1.]]*257),("points",[[0,1],[10**500,1]]),
                          ("points",[[0,1],[True,1]]),("points",[[0,1],[float("nan"),1]]),
                          ("points",[[0,1],[0,1]]),("length_m",5),("length_m",10001)):
            with self.subTest(field=key):
                candidate=copy.deepcopy(meta["route"])
                candidate[key]=value
                with self.assertRaises(ValueError):
                    pack.registered_route(candidate, meta)
