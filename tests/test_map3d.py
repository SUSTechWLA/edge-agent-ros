"""Geometry receives actual PointCloud2 layouts. No ROS or device dependencies."""
import math
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "perception" / "map3d"), str(ROOT)]
from geometry import decode, rotation, Surface, IDENTITY, MAX_BYTES, SAMPLE_LIMIT


def msg(points, width=2, order="<", fmt="f"):
    size = struct.calcsize(fmt)
    fields = [SimpleNamespace(name=k, offset=i*size, datatype=7 if fmt=="f" else 8, count=1)
              for i,k in enumerate("xyz")]
    step = size*3 + 4
    rows = bytearray()
    for i,p in enumerate(points):
        rows.extend(struct.pack(order+fmt*3,*p)+b"pad!")
        if i%width==width-1:
            rows.extend(b"tail")
    return SimpleNamespace(width=width,height=len(points)//width,point_step=step,
                           row_step=width*step+4,is_bigendian=order==">",data=rows,fields=fields)


class GeometryTests(unittest.TestCase):
    def test_organized_endian_translation_and_rotation(self):
        for fmt in ("d","f"):
            for order in (">","<"):
                raw=msg([(1,0,0),(0,1,0),(0,0,2),(float("nan"),1,2)],order=order,fmt=fmt)
                tf=rotation((1,2,3),(0,0,math.sqrt(.5),math.sqrt(.5)))
                got=decode(raw,tf)
                self.assertEqual([tuple(round(v,4) for v in p) for p in got],[(1,3,3),(0,2,3),(1,2,5)])

    def test_bounds_and_unrepresentable_geometry(self):
        cloud=msg([(3,5,7)]*10000)
        self.assertLessEqual(len(decode(cloud,IDENTITY)),SAMPLE_LIMIT)
        for setting,value in (("data",bytearray(MAX_BYTES+1)),("row_step",5),("point_step",0),("fields",[])):
            cloud=msg([(1,1,1)]*2)
            setattr(cloud,setting,value)
            with self.assertRaises(ValueError):
                decode(cloud,IDENTITY)
        cloud=msg([(0,0,0),(1,float("inf"),3)])
        self.assertEqual(decode(cloud,IDENTITY),[[0,0,0]])
        with self.assertRaises(ValueError):
            rotation((0,0,0),(0,0,0,0))

    def test_rolling_voxels_out_of_order_duplicates_and_capacity(self):
        s=Surface(limit=2)
        self.assertTrue(s.update(100,[(.01,0,0),(.02,0,0),(1,0,2)]))
        self.assertEqual(len(s.cells),2)
        self.assertFalse(s.update(99,[(3,0,7)]))
        self.assertFalse(s.update(100,[(3,0,7)]))
        self.assertTrue(s.update(200,[(2,0,3)]))
        self.assertEqual(list(s.cells.values()),[[1,0,2],[2,0,3]])
        self.assertEqual(s.evictions,1)
        invalid = decode(msg([(float("nan"), 0, 0)] * 2), IDENTITY)
        self.assertEqual(invalid, [])
        self.assertTrue(s.update(210, invalid))  # accept packet sequence without adding a surface
        self.assertEqual(list(s.cells.values()), [[1,0,2],[2,0,3]])
        self.assertFalse(s.update(205, [(4,0,5)]))
        self.assertTrue(s.update(215, [(4,0,5)]))


if __name__=="__main__":
    unittest.main()
