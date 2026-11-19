"""SDD-043 decision and receipt tests, independent of Gazebo and site geography."""
import math
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"tests/system")]
from campus_survey_worker import fresh,home_leg,measured


class OnlineDecision(unittest.TestCase):
    def test_live_map_changes_home_route(self):
        size=40
        grid=dict(frame="odom",width=size,height=size,resolution=.1,
                  origin=[-2,-2,0],cells=[0]*size**2)
        xy=[-1.,0.]
        home=[.95,0.]
        bounds=[-2.,-2.,2.,2.]
        direct=home_leg(grid,xy,home,bounds)
        self.assertGreater(direct[0],xy[0])
        self.assertLess(abs(direct[1]),.1)
        self.assertLessEqual(math.dist(xy,direct),.65)
        for y in range(size):
            grid["cells"][y*size+20]=-1
        with self.assertRaisesRegex(ValueError,"no_current_free_route_home"):
            home_leg(grid,xy,home,bounds)
        # An observed gap in the upper half opens a safe route around the wall.
        for y in range(30,size):
            grid["cells"][y*size+20]=0
        cur=xy
        highest=cur[1]
        for _ in range(30):
            nxt=home_leg(grid,cur,home,bounds)
            if nxt is None: break
            self.assertLessEqual(math.dist(cur,nxt),.65)
            cur=nxt
            highest=max(highest,cur[1])
        self.assertLessEqual(math.dist(cur,home),.13)
        self.assertGreater(highest,1.1)

    def test_no_shortcut_on_unknown_start_even_when_close_to_home(self):
        size=40
        raw=dict(frame="odom",width=size,height=size,resolution=.1,
                 origin=[-2,-2,0],cells=[0]*size**2)
        self.assertIsNone(home_leg(raw,[.1,0],[0,0],[-2,-2,2,2]))
        raw["cells"][20*size+20]=-1
        with self.assertRaisesRegex(ValueError,"clearance"):
            home_leg(raw,[.1,0],[0,0],[-2,-2,2,2])

    def test_time_domain_and_source_windows(self):
        samples={k:(98.99,50_000_000_000,None) for k in ("pose","map","clock")}
        self.assertFalse(fresh({},100.))
        self.assertFalse(fresh(samples,100.))
        samples.update(clock=(99.99,52_000_000_000,None),pose=(99.98,52_000_000_000,None))
        self.assertTrue(fresh(samples,100.))
        samples["map"]=(99.99,53_000_000_000,None)
        self.assertFalse(fresh(samples,100.))
        samples["map"]=(99.99,49_000_000_000,None)
        self.assertFalse(fresh(samples,100.))

    def test_receipt_must_contain_one_finite_measured_point(self):
        ok=SimpleNamespace(state="SUCCEEDED",verified_count=1,evidence=[SimpleNamespace(x=.35,y=.01)],detail="test")
        self.assertEqual(measured(ok,[.3,0]),[.35,.01])
        for state,count,x in (("CANCELED",1,.35),("SUCCEEDED",0,.35),("SUCCEEDED",1,math.nan),("SUCCEEDED",1,1.)):
            bad=SimpleNamespace(state=state,verified_count=count,evidence=[SimpleNamespace(x=x,y=0)],detail="test")
            with self.assertRaises(RuntimeError):
                measured(bad,[.3,0])


if __name__=="__main__":
    unittest.main()
