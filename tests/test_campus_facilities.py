"""SDD-028. Check the metric training facilities from the same geometry Gazebo reads."""
import math
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scene/campus_large"))
import athletics
import build


def numbers(element, path):
    return list(map(float, element.findtext(path).split()))


def face_bounds(surface, name):
    vertices = sorted({i for (group,_),faces in surface.faces.items() if name == group
                       for triangle in faces for i in triangle})
    points = [surface.vertices[i-1] for i in vertices]
    return [(min(p[n] for p in points),max(p[n] for p in points)) for n in range(3)]


class FacilitiesTest(unittest.TestCase):
    def test_hollow_building_and_two_metre_portal(self):
        world = ET.Element("world")
        features = build.teaching(world)
        link = world.find("./model[@name='teaching']/link")
        self.assertEqual(len({v.get("name") for v in link.findall("visual")}),len(link.findall("visual")))
        self.assertAlmostEqual(numbers(link.find("./visual[@name='hall_3_roof']"),"pose")[2],14.72)
        self.assertAlmostEqual(numbers(link.find("./visual[@name='studio_2_roof']"),"pose")[2],11.04)
        self.assertIsNotNone(link.find("./visual[@name='portal_north_glass']"))
        self.assertTrue(any(p.get("name","").startswith("desktop_") for p in link))
        # Sweep a 2.198 m-wide, 2.699 m-tall gate along the mission corridor.
        # Also probe open indoor volume at the centre of each upper storey.
        for part in link.findall("collision"):
            x,y,z = numbers(part,"pose")[:3]
            size = part.find("geometry/box/size")
            if size is not None:
                dx,dy,dz = (n/2 for n in map(float,size.text.split()))
            else:
                dx = dy = float(part.findtext("geometry/cylinder/radius"))
                dz = float(part.findtext("geometry/cylinder/length"))/2
            intrudes = (x+dx>0 and x-dx<4 and abs(y)-dy<1.099 and z+dz>.02 and z-dz<2.699)
            self.assertFalse(intrudes,part.get("name"))
            for height in (5.5,9.2,12.8):
                self.assertFalse(abs(x+7)<dx and abs(y)<dy and abs(z-height)<dz,part.get("name"))
        self.assertTrue(any(p["base_z"]==2.7 and p["id"]=="lintel" for p in features))

    def test_metric_track_and_fully_marked_infield(self):
        surface = athletics.playing_surface()
        self.assertAlmostEqual(2*athletics.STRAIGHT + math.tau*(athletics.KERB_RADIUS+.3),400,delta=.002)
        for i in range(8):
            (_,xmax),(ymin,ymax),_ = face_bounds(surface,f"lane_{i+1}")
            self.assertAlmostEqual(ymax,36.5+(i+1)*1.22,places=4)
            self.assertAlmostEqual(-ymin,ymax,places=4)
            self.assertAlmostEqual(xmax,42.195+ymax,places=4)
        for s in (-1,1):
            (_, _),(y0,y1),_ = face_bounds(surface,f"touchline_{s}")
            self.assertAlmostEqual((y0+y1)/2,s*34)
            (x0,x1),_,_ = face_bounds(surface,f"goal_line_{s}")
            self.assertAlmostEqual((x0+x1)/2,s*52.5)
            _,(y0,y1),_ = face_bounds(surface,f"penalty_area_{s}")
            self.assertAlmostEqual((y1-y0)/2,20.16+.06)
            (x0,x1),_,_ = face_bounds(surface,f"penalty_mark_{s}")
            self.assertAlmostEqual((x0+x1)/2,s*41.5)
            self.assertIn((f"penalty_arc_{s}","paint"),surface.faces)
            for t in (-1,1):
                self.assertIn((f"corner_{s}_{t}","paint"),surface.faces)
                # All four corners lie inside the inner track kerb with run-off.
                self.assertLess(math.hypot(52.5-42.195,t*34),36.5)
        for faces in surface.faces.values():
            for triangle in faces:
                a,b,c = [surface.vertices[i-1] for i in triangle]
                self.assertGreater((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]),0,
                                   "Gazebo would cull the top face")

    def test_goals_have_regulation_clearances_and_mesh_is_resolvable(self):
        with tempfile.TemporaryDirectory() as folder:
            world, root = ET.Element("world"), Path(folder)
            build.stadium(world, root)
            link = world.find("./model[@name='track']/link")
            for side in (-1,1):
                posts = [link.find(f"./collision[@name='goal_post_{side}_{i}']") for i in (-1,1)]
                inner_width = numbers(posts[1],"pose")[1]-numbers(posts[0],"pose")[1]-numbers(posts[0],"geometry/box/size")[1]
                bar = link.find(f"./collision[@name='crossbar_{side}']")
                self.assertAlmostEqual(inner_width,7.32)
                self.assertAlmostEqual(numbers(bar,"pose")[2] - numbers(bar,"geometry/box/size")[2]/2,2.44)
            path = root / link.findtext(".//mesh/uri")
            self.assertTrue(path.exists())
            material = path.parent / path.read_text().splitlines()[0].split()[1]
            self.assertTrue(material.exists())
            self.assertEqual(athletics.playing_surface().write(path.parent),path)
            (root/"world.sdf").write_bytes(ET.tostring(world))
            before = build.scene_revision(world,root,{"frame":"odom"})
            material.write_text(material.read_text()+"# changed\n")
            self.assertNotEqual(build.scene_revision(world,root,{"frame":"odom"}),before)


if __name__ == "__main__":
    unittest.main()
