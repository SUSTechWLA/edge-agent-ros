"""Regression for triangulated OBJ quads, vertical walls, UVs and SDF placement."""
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
import zlib


NATIVE = (shutil.which("g++") and Path("/usr/include/assimp/scene.h").is_file()
          and importlib.util.find_spec("numpy"))


@unittest.skipUnless(NATIVE, "Run in the Gazebo build image with Assimp, PNG and NumPy")
class CampusMesh(unittest.TestCase):
    def test_quad_and_vertical_wall_survive_export(self):
        import numpy as np
        from scene.campus_large.export_mesh import export
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def chunk(name, data):
                return struct.pack(">I",len(data))+name+data+struct.pack(">I",zlib.crc32(name+data))
            image = (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR", struct.pack(">IIBBBBB",1,1,8,2,0,0,0))
                     +chunk(b"IDAT",zlib.compress(b"\0\xff\x80\0"))+chunk(b"IEND",b""))
            (root/"facade.png").write_bytes(image)
            (root/"wall.mtl").write_text("newmtl Wall\nKd 1 1 1\nmap_Kd facade.png\nnewmtl Roof\nKd .4 .6 .8\n")
            (root/"wall.obj").write_text("mtllib wall.mtl\nv 0 0 0\nv 2 0 0\nv 2 0 3\nv 0 0 3\nv 0 3 3\n"
                "vt 0 0\nvt 2 0\nvt 2 1\nvt 0 1\nusemtl Wall\nf 1/1 2/2 3/3 4/4\n"
                "usemtl Roof\nf 3 5 4\n")
            world = root/"world.sdf"
            world.write_text("<sdf><world><model name='school'><static>true</static><pose>10 4 0 0 0 0</pose>"
                "<link name='body'><pose>0 3 0 0 0 0</pose><visual name='facade'><pose>0 0 1 0 0 0</pose>"
                "<geometry><mesh><uri>wall.obj</uri><scale>2 1 1</scale></mesh></geometry></visual></link></model>"
                "<model name='robot'><static>false</static><link name='body'/></model></world></sdf>")
            file = root/"fixture.glb"
            export(world,file,[])
            content = file.read_bytes()
            header, version, size, desc_size = struct.unpack_from("<4sIII",content)
            self.assertEqual((header,version,size),(b"glTF",2,len(content)))
            doc = json.loads(content[20:20+desc_size])
            binary = memoryview(content)[28+desc_size:]
            def accessor(id_):
                a = doc["accessors"][id_]; v = doc["bufferViews"][a["bufferView"]]
                values = np.frombuffer(binary[v["byteOffset"]:v["byteOffset"]+v["byteLength"]],
                    {5123:"<u2",5125:"<u4",5126:"<f4"}[a["componentType"]])
                return values.reshape(a["count"],-1)
            root_node, node = doc["nodes"]
            self.assertEqual(root_node["name"],"ROS world")
            self.assertEqual(node["name"],"school/facade")
            np.testing.assert_allclose(np.reshape(node["matrix"],(4,4)).T,
                [[2,0,0,10],[0,1,0,7],[0,0,1,1],[0,0,0,1]])
            prims = {doc["materials"][p["material"]]["name"]:p for p in doc["meshes"][node["mesh"]]["primitives"]}
            self.assertEqual(len(accessor(prims["Wall"]["indices"])),6)
            self.assertEqual(len(accessor(prims["Roof"]["indices"])),3)
            wall = prims["Wall"]["attributes"]
            for pos,uv,normal in zip(accessor(wall["POSITION"]), accessor(wall["TEXCOORD_0"]), accessor(wall["NORMAL"])):
                self.assertEqual(pos[1],0)
                np.testing.assert_allclose(uv,[pos[0],1-pos[2]/3],atol=1e-6)
                self.assertAlmostEqual(abs(normal[1]),1)
            self.assertEqual(doc["images"][0]["mimeType"],"image/png")
            view = doc["bufferViews"][doc["images"][0]["bufferView"]]
            self.assertEqual(binary[view["byteOffset"]:view["byteOffset"]+8].tobytes(),image[:8])


if __name__=="__main__":
    unittest.main()
