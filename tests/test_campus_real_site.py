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
                if page["features"]:
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
        self.assertGreaterEqual(min(x for x,y,z in vertices),3)
        self.assertLessEqual(max(x for x,y,z in vertices),542)
        self.assertGreaterEqual(min(y for x,y,z in vertices),3)
        self.assertLessEqual(max(y for x,y,z in vertices),1167)
        self.assertGreater(max(z for x,y,z in vertices),90)
        self.assertGreater(max(z for x,y,z in vertices if 130<x<270 and 565<y<740),80)

    def test_world_and_texture_resources_install_offline(self):
        world = ET.Element("world")
        with tempfile.TemporaryDirectory() as tmp:
            bookmark = site.append_site(world, Path(tmp))
            self.assertEqual(bookmark["bounds"], [-410,-161,135,1009])
            model = world.find("model")
            self.assertEqual(model.findtext("pose"),"-410 -161 0 0 0 0")
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
            self.assertEqual(struct.unpack(">II",image[16:24]),(1090,2340))

    def test_street_camera_is_outdoors_and_sees_the_measured_ewi_tower(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera = site.append_site(ET.Element("world"), Path(tmp))["street_camera"]
        east, north = site.SITE_XY
        eye = camera["eye"]
        origin = (eye[0]-east, eye[1]-north, eye[2])
        self.assertGreaterEqual(eye[2],2.4)
        self.assertLessEqual(eye[2],4)
        look = camera["target"]
        dx, dy = look[0]-eye[0], look[1]-eye[1]
        rays = [(0,0,1), (0,0,-1), (dx,dy,look[2]-eye[2]), (dx,dy,75-eye[2])]
        nearest = [(math.inf,None)]*len(rays)
        vertices = []

        def cross(a,b):
            return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

        def dot(a,b):
            return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]

        for line in gzip.decompress((site.DATA/"buildings.obj.gz").read_bytes()).decode().splitlines():
            kind,*args = line.split()
            if kind == "v":
                vertices.append(tuple(map(float,args)))
            elif kind == "g":
                building = args[0]
            elif kind == "f":
                a,b,c = (vertices[int(corner.split("/")[0])-1] for corner in args)
                e1 = tuple(y-x for x,y in zip(a,b))
                e2 = tuple(y-x for x,y in zip(a,c))
                offset = tuple(y-x for x,y in zip(a,origin))
                q = cross(offset,e1)
                for i,direction in enumerate(rays):
                    h = cross(direction,e2)
                    det = dot(e1,h)
                    if abs(det)<1e-9:
                        continue
                    u,v,t = dot(offset,h)/det,dot(direction,q)/det,dot(e2,q)/det
                    if 0<=u and 0<=v and u+v<=1 and 0<t<nearest[i][0]:
                        nearest[i] = t,building
        self.assertEqual([name for _,name in nearest[:2]],[None,None])
        self.assertEqual([name for _,name in nearest[2:]],["NL_IMBAG_Pand_0503100000030264"]*2)


if __name__ == "__main__":
    unittest.main()
