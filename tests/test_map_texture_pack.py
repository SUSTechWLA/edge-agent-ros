"""The optional simulation appearance pack can never authorize ROS occupancy."""
import base64
import hashlib
import json
from pathlib import Path
import struct
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

    def test_mesh_pins_the_validated_bytes_and_preserves_planar_fallback(self):
        # A complete binary glTF header and its JSON chunk. Scene content is checked in the browser test.
        chunk = b'{"asset":{"version":"2.0"}} '
        glb = struct.pack("<4sIIII",b"glTF",2,len(chunk)+20,len(chunk),0x4e4f534a)+chunk
        digest = hashlib.sha256(glb).hexdigest()
        path = self.root/"scene.glb"
        path.write_bytes(glb)
        area = dict(id="building",label="Tower",bounds=[0,0,8,10],
                    camera=dict(eye=[6,2,4],target=[4,2,3]))
        pack = self.pack(mesh_sha256=digest,areas=[area])
        self.assertEqual(pack.info["areas"],[area])
        url = pack.info["mesh_url"]
        self.assertEqual(pack.files[url],glb)
        path.write_bytes(glb[:-1]+b"?")
        self.assertEqual(pack.files[url],glb)
        for raw,reason in ((path.read_bytes(),"checksum_mismatch"),(b"old","format_invalid"),(None,"missing")):
            if raw is None:
                path.unlink()
            else:
                path.write_bytes(raw)
            broken = self.pack(mesh_sha256=digest)
            self.assertTrue(broken.info["mesh_error"].endswith(reason))
            self.assertNotIn("mesh_url",broken.info)
            self.assertIn(broken.info["url"],broken.files)


if __name__=="__main__":
    unittest.main()
