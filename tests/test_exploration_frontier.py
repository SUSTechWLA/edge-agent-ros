"""Software contracts for exploration strategies and exported map geometry."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from exploration.frontier import plan_frontier
from exploration.map_artifact import save_map


def square(size, cells):
    return {"frame":"odom","width":size,"height":size,"origin":[0,0,0],
            "resolution":1.0,"cells":cells}


class FrontierTests(unittest.TestCase):
    def test_unknown_room_observed_iteratively(self):
        size = 15
        grid = square(size,[-1]*(size*size))
        xy = [3.5,7.5]
        visited, counts = [], []
        for _ in range(200):
            # Independent square-range sensor in an obstacle-free, bounded room.
            x,y = map(math.floor,xy)
            for j in range(max(0,y-3),min(size,y+4)):
                for i in range(max(0,x-3),min(size,x+4)):
                    grid["cells"][j*size+i]=0
            options = dict(bounds=[0,0,size,size],footprint_radius=.28,max_step=2.1,visited=visited)
            plan = plan_frontier(grid,xy,**options)
            self.assertEqual(plan,plan_frontier(grid,xy,**options))
            counts.append(plan["known_cells"])
            if plan["state"]!="WAYPOINT":
                break
            goal = plan["goal"]
            self.assertLessEqual(math.dist(xy,goal),2.1)
            self.assertTrue(xy[0]==goal[0] or xy[1]==goal[1])
            for wx,wy in plan["path"]:
                cx,cy=math.floor(wx),math.floor(wy)
                self.assertTrue(all(grid["cells"][j*size+i]==0
                                    for j in range(cy-1,cy+2) for i in range(cx-1,cx+2)))
            visited.append(goal)
            xy=goal
        self.assertGreater(len(visited),2)
        self.assertEqual(counts,sorted(counts))
        self.assertEqual((plan["state"],counts[-1]),("COMPLETE",225))

    def test_obstacle_and_unknown_gap_cannot_be_crossed(self):
        size=15
        for barrier in (1,100):
            # The only unknown region is on the other side of a solid wall.
            grid=square(size,[0 if x<7 else (barrier if x==7 else -1)
                              for _ in range(size) for x in range(size)])
            plan=plan_frontier(grid,[3.5,7.5],bounds=[0,0,15,15],max_step=2)
            self.assertEqual((plan["state"],plan["goal"]),("PARTIAL",None))
            self.assertLess(plan["known_cells"],plan["total_cells"])
            # One-cell opening is narrower than the conservative footprint mask.
            grid["cells"][7*size+7]=0
            plan=plan_frontier(grid,[3.5,7.5],bounds=[0,0,15,15],max_step=2)
            if plan["goal"] is not None:
                self.assertLess(plan["goal"][0],7)

    def test_stuck_budget_unknown_and_wrong_extent(self):
        g=square(8,[0 if x<6 else -1 for _ in range(8) for x in range(8)])
        args=dict(bounds=[0,0,8,8],footprint_radius=.2,max_step=1.2)
        self.assertEqual(plan_frontier(g,[2.5,3.5],**args)["state"],"WAYPOINT")
        self.assertEqual(plan_frontier(g,[2.5,3.5],**(args|{"max_step":.03}))["reason"],"step_below_grid_spacing")
        self.assertEqual(plan_frontier(g,[2.5,3.5],**(args|{"bounds":[0,0,12,8]}))["reason"],"bounds_outside_grid")
        self.assertEqual(plan_frontier(g,[2.5,3.5],**args,visited=[
            [i+.5,j+.5] for j in range(8) for i in range(8)])["state"],"PARTIAL")
        g["cells"]=[-1]*64
        self.assertEqual(plan_frontier(g,[3.5,3.5],**args)["state"],"BLOCKED")
        self.assertEqual(plan_frontier(g,[math.nan,3.5],**args)["state"],"BLOCKED")

    def test_new_obstacle_replans_and_off_center_segment_has_clearance(self):
        g=square(15,[0 if x<11 else -1 for _ in range(15) for x in range(15)])
        pose=[3.85,7.22]
        options=dict(bounds=[0,0,15,15],footprint_radius=.32,max_step=1.8)
        first=plan_frontier(g,pose,**options)
        self.assertEqual(first["state"],"WAYPOINT")
        gx,gy=map(math.floor,first["goal"])
        g["cells"][gy*15+gx]=100
        next_plan=plan_frontier(g,pose,**options)
        self.assertEqual(next_plan["state"],"WAYPOINT")
        self.assertNotEqual(next_plan["goal"],first["goal"])
        dx,dy=(v-u for u,v in zip(pose,next_plan["goal"]))
        for t in range(101):
            x,y=pose[0]+t*dx/100,pose[1]+t*dy/100
            # Minimum distance from the unsnapped continuous path to the newly occupied square.
            self.assertGreaterEqual(math.hypot(max(gx-x,0,x-gx-1),max(gy-y,0,y-gy-1)),.32)

    def test_rotated_grid_and_malformed_map(self):
        g=square(10,[0 if x<7 else -1 for _ in range(10) for x in range(10)])
        p=plan_frontier(g,[2.5,5.5],bounds=[0,0,10,10],max_step=2)
        self.assertEqual(p["state"],"WAYPOINT")
        rot=g|{"origin":[10,0,math.pi/2]}
        q=plan_frontier(rot,[4.5,2.5],bounds=[0,0,10,10],max_step=2)
        self.assertAlmostEqual(q["goal"][0],10-p["goal"][1])
        self.assertAlmostEqual(q["goal"][1],p["goal"][0])
        for change in ({"width":0},{"cells":[]},{"origin_z":1},{"cells":[101]*100}):
            bad=plan_frontier(g|change,[2,5],bounds=[0,0,10,10])
            self.assertEqual((bad["state"],bad["goal"]),("BLOCKED",None))


class MapTests(unittest.TestCase):
    def test_orientation_original_values_hash_and_replay(self):
        g=square(2,[0,-1,100,25])|{"origin":[3,-2,.8]}
        meta=dict(robot_id="rover",calibration_id="v1",operation_id="attempt",stamp_ns=100,
                  state="PARTIAL",reason="step_limit")
        with tempfile.TemporaryDirectory() as folder:
            dst=Path(folder)/"export"
            with ThreadPoolExecutor(max_workers=6) as pool:
                exports=list(pool.map(lambda _:save_map(dst,g,metadata=meta),range(12)))
            self.assertTrue(all(m==exports[0] for m in exports))
            manifest=json.loads((dst/"manifest.json").read_bytes())
            self.assertEqual(manifest["known_cells"],3)
            pgm=(dst/"map.pgm").read_bytes()
            self.assertEqual(pgm,b"P5\n2 2\n255\n"+bytes([0,0,254,205]))
            self.assertIn("origin: [3, -2, 0.8]",(dst/"map.yaml").read_text())
            self.assertEqual(json.loads((dst/"occupancy.json").read_bytes())["cells"],g["cells"])
            self.assertEqual(hashlib.sha256(pgm).hexdigest(),manifest["sha256"]["map.pgm"])
            for other in (meta|{"operation_id":"another"},meta|{"stamp_ns":99},meta|{"robot_id":""}):
                with self.assertRaises(ValueError):
                    save_map(dst,g,metadata=other)
            (dst/"map.pgm").write_bytes(b"damaged")
            with self.assertRaisesRegex(ValueError,"map_artifact_conflict"):
                save_map(dst,g,metadata=meta)
            self.assertEqual((dst/"map.pgm").read_bytes(),b"damaged")


if __name__ == "__main__":
    unittest.main()
