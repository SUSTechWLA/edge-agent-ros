import json
import math
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from perception.rover_preview.planner import plan
from perception.rover_preview.provider import finish_evidence,Provider

CFG=json.loads((ROOT/"perception/rover_preview/lingao.json").read_bytes())


def snapshot(*,known=True,box=False):
    t=str(time.time_ns())
    rays=[]
    pts=[]
    for i in range(360):
        a=i*math.pi/180
        c,s=math.cos(a),math.sin(a)
        hit=None
        if box and c>0 and abs(1.45*s/c)<=.22:
            hit=1.45/c
            pts.append((c*hit,s*hit))
        rays.append({"angle_rad":a,"hit_m":hit,"free_to_m":(hit if hit is not None else 5.) if known else None})
    return {"source_id":"test.laser","base_frame":"base_link","scan_stamp_ns":t,"rays":rays,
            "hits_body_m":pts,"calibration_ref":"synthetic","odom":{"stamp_ns":t,"frame_id":"odom","child_frame_id":"base_link",
            "x_m":3.,"y_m":-2.,"yaw_rad":math.pi/2,"linear_mps":0.,"angular_rps":0.,"epoch":"e"}}


class PreviewTests(unittest.TestCase):
    def test_clear_and_box_and_unknown(self):
        free=plan(snapshot(),CFG)
        self.assertEqual(free["status"],"planned")
        self.assertEqual(len(free["path_body_m"]),2)
        room={**CFG,"horizon_m":3.5}
        box=plan(snapshot(box=True),room)
        self.assertGreater(len(box["path_body_m"]),2)
        self.assertGreaterEqual(box["minimum_clearance_m"],CFG["margin_m"])
        self.assertFalse(box["motion_authorized"])
        blind=plan(snapshot(known=False,box=True),room)
        self.assertEqual(blind["status"],"partial")
        self.assertEqual(blind["coverage_fraction_on_path"],0)
        self.assertEqual(plan(snapshot(known=False),CFG)["status"],"blocked")

    def test_odom_coordinates_are_measurements(self):
        samples=[snapshot() for _ in range(5)]
        t=time.time_ns()
        for i,s in enumerate(samples):
            s["scan_stamp_ns"]=s["odom"]["stamp_ns"]=str(t+i*120000000)
        r=finish_evidence(plan(samples[0],CFG),samples)
        self.assertAlmostEqual(r["planned_endpoint"]["x_m"],3.)
        self.assertAlmostEqual(r["planned_endpoint"]["y_m"],-2.+CFG["horizon_m"])
        self.assertEqual(r["end_observed_pose"]["y_m"],-2.)
        self.assertTrue(r["stationary"]["verified"])
        samples[-1]["odom"]["epoch"]="different"
        self.assertFalse(finish_evidence(plan(samples[0],CFG),samples)["stationary"]["verified"])
        samples[0]["odom"]=None
        r=finish_evidence(plan(samples[0],CFG),samples)
        self.assertEqual(r["path_odom_m"],[])
        self.assertEqual(r["planned_endpoint"]["frame_id"],"base_link")
        self.assertIsNone(r["map_anchor"])

    def test_occupied_returns_block_every_route(self):
        snap=snapshot()
        snap["hits_body_m"]=[[.3,0.]]
        result=plan(snap,CFG)
        self.assertEqual(result["status"],"blocked")
        self.assertEqual(result["reason"],"current_footprint_below_margin")
        self.assertEqual(result["recommendation"],"hold_position")
        self.assertAlmostEqual(result["present_min_clearance_m"],.3-CFG["front_m"])
        self.assertAlmostEqual(result["front_clearance_to_surface_m"],.3-CFG["front_m"])
        self.assertEqual(result["path_body_m"],[])
        self.assertIsNone(result["planned_endpoint"])
        self.assertIsNone(result["minimum_clearance_m"])

    def test_body_only_rays_are_not_a_known_corridor(self):
        snap=snapshot()
        for ray in snap["rays"]:
            ray["free_to_m"]=.12
        result=plan(snap,CFG)
        self.assertEqual(result["coverage"]["known_sectors"],0)
        self.assertIsNone(result["coverage_fraction_on_path"])
        self.assertEqual(result["status"],"blocked")
        self.assertFalse(result["path_body_m"])

    def test_short_horizon_refuses_instead_of_pivoting_at_sharp_corners(self):
        result=plan(snapshot(box=True),CFG)
        self.assertEqual(result["status"],"blocked")
        self.assertEqual(result["path_body_m"],[])
        self.assertIsNone(result["trajectory"])
        self.assertGreater(result["search"]["curvature_rejected"],0)
        self.assertGreater(result["search"]["clearance_rejected"],0)

    def test_curve_and_sweep_match_independent_dense_geometry(self):
        obs=snapshot(box=True)
        result=plan(obs,{**CFG,"horizon_m":3.5})
        curve=result["trajectory"]
        self.assertEqual(curve["continuity"],"C2")
        self.assertEqual([[s["x_m"],s["y_m"]] for s in curve["samples_body"]],result["path_body_m"])
        dense_min=math.inf
        last=(0.,0.)
        for seg in curve["segments_body"]:
            self.assertEqual((seg["x0_m"],seg["y0_m"]),last)
            dx,dy=seg["x1_m"]-seg["x0_m"],seg["y1_m"]-seg["y0_m"]
            for idx in range(201):
                u=idx/200
                x=seg["x0_m"]+dx*u
                y=seg["y0_m"]+dy*(10*u**3-15*u**4+6*u**5)
                slope=dy/dx*(30*u**2-60*u**3+30*u**4)
                accel=dy/(dx*dx)*(60*u-180*u**2+120*u**3)
                self.assertLessEqual(abs(accel)/(1+slope*slope)**1.5,curve["max_curvature_bound_inv_m"]+1e-9)
                self.assertLessEqual(curve["max_curvature_bound_inv_m"],CFG["curvature_limit_inv_m"])
                if idx in (0,200):
                    self.assertAlmostEqual(slope,0)
                    self.assertAlmostEqual(accel,0)
                c=1/math.hypot(1,slope)
                s=c*slope
                for px,py in obs["hits_body_m"]:
                    bx=(px-x)*c+(py-y)*s
                    by=(py-y)*c-(px-x)*s
                    d=math.hypot(max(0,-CFG["rear_m"]-bx,bx-CFG["front_m"]),
                                 max(0,-CFG["right_m"]-by,by-CFG["left_m"]))
                    dense_min=min(dense_min,d)
            last=seg["x1_m"],seg["y1_m"]
        self.assertGreaterEqual(dense_min,result["minimum_clearance_m"])
        self.assertGreaterEqual(result["minimum_clearance_m"],CFG["margin_m"])

    def test_forward_subgoals_and_preferred_side(self):
        cfg={**CFG,"horizon_m":3.5}
        direct=plan(snapshot(),cfg,(3.,-.75))
        self.assertEqual(direct["status"],"planned")
        self.assertEqual(direct["path_body_m"][-1],[3.,-.75])
        for goal in ((-1.,0.),(3.6,0.),(2.,2.),(math.nan,0.)):
            with self.assertRaises(ValueError):
                plan(snapshot(),cfg,goal)
        for side in ("left","right"):
            same=plan(snapshot(box=True),cfg,preferred_side=side)
            self.assertEqual(same["trajectory"]["side"],side)
        obstacle=snapshot(box=True)
        obstacle["hits_body_m"].extend((i/10,.9) for i in range(5,24))
        new=plan(obstacle,cfg,preferred_side="left")
        self.assertEqual(new["trajectory"]["side"],"right")
        # An occupied local endpoint cannot be silently substituted.
        obstacle["hits_body_m"].append((3.5,0.))
        self.assertFalse(plan(obstacle,cfg)["path_body_m"])

    def test_store_freezes_completion_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp,patch("perception.rover_preview.provider.read_snapshot",side_effect=lambda url:snapshot(box=True)) as reads:
            req={"operation_id":"test/step/2","arguments":{}}
            p=Provider(Path(tmp),CFG,"http://unused")
            a=p.invoke(req)
            self.assertEqual(a["state"],"SUCCEEDED")
            n=reads.call_count
            b=Provider(Path(tmp),CFG,"http://unused").invoke(req)
            self.assertEqual(reads.call_count,n)
            self.assertEqual(b,json.loads(json.dumps(a)))
            self.assertTrue(int(a["evidence"]["end_observed_pose"]["stamp_ns"])>int(a["evidence"]["initial_pose"]["stamp_ns"]))


if __name__=="__main__":
    unittest.main()
