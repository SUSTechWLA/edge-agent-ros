"""Offline atlas registration and rebuild tests for the Gazebo image."""
import hashlib
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET
import zlib

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE/"scene/campus_large"))
try:
    import texture
except ModuleNotFoundError as error:
    if error.name not in {"numpy", "fcntl"}:
        raise
    texture = None


def samples(path):
    raw = path.read_bytes()
    width, height, _, mode = struct.unpack(">IIBB", raw[16:26])
    channels = 1 if mode == 0 else 3
    size = struct.unpack(">I", raw[33:37])[0]
    pixels = zlib.decompress(raw[41:41+size])
    def get(col, row):
        start = row*(1+width*channels)+1+col*channels
        return list(pixels[start:start+channels])
    return get


@unittest.skipUnless(texture, "run in the Linux Gazebo image with its numerical dependencies")
class TestOrthoAtlas(unittest.TestCase):
    def test_mesh_uv_and_shape_occlusion_in_world_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            model=root/"models/Campus"
            model.mkdir(parents=True)
            (model/"model.sdf").write_text("<sdf><model><link><visual><pose>0 0 -0.25 0 0 0</pose><geometry><mesh><uri>model://Campus/m.obj</uri></mesh></geometry></visual></link></model></sdf>")
            (model/"m.obj").write_text("mtllib m.mtl\nv -1 -1 3\nv 1 -1 3\nv 1 1 3\nv -1 1 3\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nusemtl colored\nf 1/1 2/2 3/3 4/4\n")
            (model/"m.mtl").write_text("newmtl colored\nKd 1 1 1\nmap_Kd palette.png\n")
            texture.png(model/"palette.png",texture.np.uint8([[[255,0,0],[0,255,0]],[[0,0,255],[220,220,220]]]))
            (root/"world.sdf").write_text("""<sdf><world>
              <model name="floor"><link><visual><material><diffuse>.2 .2 .2 1</diffuse></material></visual></link></model>
              <include><uri>model://Campus</uri><pose>4 6 .5 0 0 0</pose></include>
              <model name="training"><link><visual><pose>4 6 4 0 0 0</pose><geometry><box><size>.5 .5 1</size></box></geometry>
              <material><diffuse>1 .5 0 1</diffuse></material></visual></link></model>
              </world></sdf>""")
            (root/"overview").mkdir()
            (root/"overview/manifest.json").write_text('{"bounds":[0,0,16,10],"frame":"odom","areas":[]}')
            (root/"layout.json").write_text('{"id":"campus_large","frame":"odom","revision":"1.0"}')
            out=root/"atlas"
            info=texture.bake(root,root,out,width=128)
            get_color=samples(out/"texture.png")
            get_height=samples(out/"height.png")
            self.assertEqual([info["width"],info["height"]],[128,80])
            # Pixel center maps to (col+.5)/8, 10-(row+.5)/8. V grows north.
            self.assertGreaterEqual(get_color(27,27)[0],178)  # shadow cast by the taller box
            self.assertEqual(get_color(27,27)[1:],[0,0])
            self.assertEqual(get_color(37,27),[0,255,0])
            self.assertEqual(get_color(27,37),[0,0,255])
            self.assertEqual(get_color(32,31),[255,127,0])
            self.assertAlmostEqual(get_height(32,31)[0]*info["height_scale_m"],4.5,delta=.011)
            self.assertAlmostEqual(get_height(27,27)[0]*info["height_scale_m"],3.25,delta=.011)
            self.assertEqual(get_height(2,2),[0])
            self.assertEqual(hashlib.sha256((out/"texture.png").read_bytes()).hexdigest(),info["sha256"])
            # Corrupt an output, then run again. Input identity and restored PNG bytes must agree.
            (out/"texture.png").write_bytes(b"interrupted")
            self.assertEqual(texture.bake(root,root,out,width=128),info)
            self.assertEqual(samples(out/"texture.png")(37,27),[0,255,0])

    def test_rotated_sdf_footprint_is_metric(self):
        atlas=texture.Atlas([-2,-2,2,2],64,[.1,.2,.3])
        v=ET.fromstring("""<visual><pose>0 0 .5 0 0 1.57079632679</pose>
          <geometry><box><size>2 .3 1</size></box></geometry><material><diffuse>.5 .5 .5 1</diffuse></material></visual>""")
        atlas.primitive(v)
        self.assertEqual(atlas.z[21,32],1)
        self.assertEqual(atlas.z[32,21],0)

    def test_generated_obj_parent_pose_material_and_cache_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/"overview").mkdir()
            models=root/"meshes"
            models.mkdir()
            (models/"room.obj").write_text("mtllib room.mtl\nv 0 0 2\nv 4 0 2\nv 0 2 2\nusemtl roof\nf 1 2 3\n")
            material=models/"room.mtl"
            material.write_text("newmtl roof\nKd 1 0 0\n")
            (root/"world.sdf").write_text("""<sdf><world>
                <model name="floor"><link><visual><material><diffuse>0 0 0 1</diffuse></material></visual></link></model>
                <model name="hall"><pose>7 4 1 0 0 1.57079632679</pose><link><pose>1 0 0 0 0 0</pose>
                <visual><geometry><mesh><uri>file:///campus/meshes/room.obj</uri></mesh></geometry></visual>
                </link></model></world></sdf>""")
            (root/"overview/manifest.json").write_text('{"bounds":[0,0,16,16],"frame":"odom","areas":[]}')
            (root/"layout.json").write_text('{"id":"campus_large","frame":"odom","revision":"test"}')
            out=root/"out"
            before=texture.bake(root,root,out,width=64)
            # Inside the rotated roof in world metres at (6.625, 5.875), not near (0,0).
            self.assertGreater(samples(out/"texture.png")(26,40)[0],200)
            self.assertEqual(samples(out/"height.png")(26,40)[0]*before["height_scale_m"],3.)
            material.write_text("newmtl roof\nKd 0 1 0\n")
            after=texture.bake(root,root,out,width=64)
            self.assertNotEqual(before["revision"],after["revision"])
            self.assertGreater(samples(out/"texture.png")(26,40)[1],200)

if __name__=="__main__":
    unittest.main()
