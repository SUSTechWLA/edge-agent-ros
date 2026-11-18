"""The optional simulation appearance pack can never authorize ROS occupancy."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"perception"/"map3d"))
from texture import TexturePack


class Appearance(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.png=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+AvzEpAAAAABJRU5ErkJggg==")
        self.meta=dict(name="simulation",scene_id="campus_large",scene_revision="1.0",revision="a",frame="odom",
                       source="test fixture",width=1,height=1,bounds=[0,1,2,3],areas=[],
                       sha256=hashlib.sha256(self.png).hexdigest())
        (self.root/"texture.png").write_bytes(self.png)
        self.snapshot=dict(environment="simulation",frame="odom",scene={"id":"campus_large","revision":"1.0","status":"online"})

    def pack(self,**changes):
        (self.root/"manifest.json").write_text(json.dumps({**self.meta,**changes}),encoding="utf-8")
        return TexturePack(self.root)

    def test_one_edition_one_scene(self):
        pack=self.pack()
        info=pack.for_map(self.snapshot)
        self.assertEqual(pack.files[info["url"]],self.png)
        for change,code in [({"frame":"other"},"texture_frame_mismatch"),
                            ({"environment":"hardware"},"texture_frame_mismatch"),
                            ({"scene":{}},"texture_waiting_for_scene"),
                            ({"scene":{"id":"small_world","revision":"1.0"}},"texture_scene_mismatch"),
                            ({"scene":{"id":"campus_large","revision":"2.0"}},"texture_scene_mismatch")]:
            self.assertEqual(pack.for_map({**self.snapshot,**change})["error"],code)
        self.assertNotEqual(self.pack(bounds=[0,1,4,5]).info["key"],info["key"])
        self.assertIsNone(TexturePack(None).for_map(self.snapshot))

    def test_bad_or_partial_editions_are_never_served(self):
        for change in [{"width":4097},{"width":3},{"bounds":[2,0,1,3]},{"height_sha256":"bad","height_scale_m":.05},
                       {"sha256":"incorrect"},{"areas":[{"id":"site","bounds":[0,0,0,0]}]}]:
            with self.subTest(change=change):
                pack=self.pack(**change)
                self.assertEqual(pack.info["status"],"error")
                self.assertFalse(pack.files)
        (self.root/"height.png").write_bytes(self.png)
        pack=self.pack(height_sha256=self.meta["sha256"],height_scale_m=.05,height_offset_m=0)
        self.assertIn(pack.info["height_url"],pack.files)


if __name__=="__main__":
    unittest.main()
