"""Cross-check the offline Delft site against its source pages and Gazebo contract."""
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]/"scene"/"campus_large"
spec = importlib.util.spec_from_file_location("real_site", ROOT/"real_site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


class DelftCampus(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((site.DATA/"manifest.json").read_bytes())

    def test_pinned_sources_cover_the_extent_and_include_landmarks(self):
        source_ids, rectangles = set(), []
        with ZipFile(site.DATA/"sources.zip") as archive:
            for name, digest in self.manifest["cityjson_pages"].items():
                raw = archive.read(name)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
                page = json.loads(raw)
                self.assertFalse(any(link["rel"] == "next" for link in page["links"]))
                self.assertTrue(page["metadata"]["metadata"]["referenceSystem"].endswith("/7415"))
                rectangles.append(tuple(map(int, Path(name).stem.split("_"))))
                source_ids.update(feature["id"] for feature in page["features"])
            self.assertEqual(hashlib.sha256(archive.read("ortho.source")).hexdigest(), self.manifest["wms_sha256"])
        west,south,east,north = self.manifest["bbox_rd_m"]
        self.assertEqual(sum((r-l)*(t-b) for l,b,r,t in rectangles), (east-west)*(north-south))
        for i,(l,b,r,t) in enumerate(rectangles):
            self.assertTrue(west <= l < r <= east and south <= b < t <= north)
            for x,y,u,v in rectangles[:i]:
                self.assertTrue(min(r,u) <= max(l,x) or min(t,v) <= max(b,y))
        included = set(self.manifest["source_building_ids"])
        self.assertTrue(included <= source_ids)
        self.assertEqual(len(included), self.manifest["buildings"])
        self.assertIn("NL.IMBAG.Pand.0503100000030264", included)

    def test_collision_has_valid_indexed_normals_on_all_faces(self):
        lines = gzip.decompress((site.DATA/"buildings.obj.gz").read_bytes()).decode().splitlines()
        vertices, normals, texcoords = [], [], 0
        faces = walls = pitched_roofs = 0
        material = ""
        for line in lines:
            kind, *args = line.split()
            if kind == "v":
                vertices.append(tuple(map(float, args)))
            elif kind == "vn":
                normal = tuple(map(float,args))
                self.assertAlmostEqual(math.hypot(*normal), 1, places=6)
                normals.append(normal)
            elif kind == "vt":
                texcoords += 1
            elif kind == "usemtl":
                material = args[0]
            elif kind == "f":
                self.assertEqual(len(args),3)
                for corner in args:
                    p,t,n = map(int,corner.split("/"))
                    self.assertTrue(0 < p <= len(vertices) and 0 < n <= len(normals) and 0 < t <= texcoords)
                nz = normals[n-1][2]
                walls += int(material in ("brick","glass") and abs(nz)<0.01)
                pitched_roofs += int(material == "ortho" and 0.05 < nz < .97)
                faces += 1
        self.assertEqual(faces, sum(self.manifest["triangles_by_surface"].values()))
        self.assertGreater(walls, 1000)
        self.assertGreater(pitched_roofs, 100)
        self.assertLessEqual(max(x for x,y,z in vertices),650)
        self.assertLessEqual(max(y for x,y,z in vertices),760)
        self.assertGreater(max(z for x,y,z in vertices),90)
        self.assertGreater(max(z for x,y,z in vertices if 300<x<440 and 155<y<330),80)

    def test_world_and_texture_resources_install_offline(self):
        world = ET.Element("world")
        with tempfile.TemporaryDirectory() as tmp:
            bookmark = site.append_site(world, Path(tmp))
            self.assertEqual(bookmark["bounds"], [395,12,1045,772])
            model = world.find("model")
            self.assertEqual(model.findtext("pose"),"395 12 0 0 0 0")
            self.assertEqual(model.findtext("link/collision/geometry/mesh/uri"),
                             model.findtext("link/visual[@name='buildings_visual']/geometry/mesh/uri"))
            for uri in model.iter("uri"):
                mesh = Path(tmp)/uri.text.removeprefix("file:///campus/")
                mtl = mesh.parent/mesh.read_text().splitlines()[0].split()[1]
                for line in mtl.read_text().splitlines():
                    if line.startswith("map_Kd "):
                        self.assertTrue((mesh.parent/line.split()[1]).is_file())
            image = (Path(tmp)/"real_site"/"ortho.png").read_bytes()
            self.assertEqual(image[:8],b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II",image[16:24]),(1560,1824))


if __name__ == "__main__":
    unittest.main()
