"""Wheel history brackets the sensor interval and rejects gaps, stale data and excursions."""
import copy
import math
from pathlib import Path
import sys
from types import SimpleNamespace as Row
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_contracts.spatial_facts import SpatialFactError
from perception.spatial_facts.stationary_odom import OdomHistory, stationary_window


T0 = 1_795_155_690_741_852_963
DT = 50_000_000
CFG = {"actor_ref":"rover", "source_id":"wheel.odometry", "frame_id":"odom", "child_frame_id":"base_foot",
       "topic":"/lingao/odom", "clock_id":"unix", "localization_epoch":"site-1"}


def msg(index, *, xy=0., angle=0., flip=False, speed=0., roll=False):
    stamp = T0 + DT * index
    v = lambda x, y, z: Row(x=x, y=y, z=z)
    scale = -1 if flip else 1
    joint = scale*math.sin(angle/2)
    q = Row(x=joint if roll else 0., y=0., z=0. if roll else joint, w=scale*math.cos(angle/2))
    return Row(header=Row(frame_id="odom", stamp=Row(sec=stamp//10**9, nanosec=stamp%10**9)), child_frame_id="base_foot",
               pose=Row(pose=Row(position=v(xy, 0., 0.), orientation=q)),
               twist=Row(twist=Row(linear=v(0., speed, 0.), angular=v(0., 0., 0.))))


class StationaryOdom(unittest.TestCase):
    def history(self, changes=None):
        h = OdomHistory(CFG)
        for i in range(15):
            sample = msg(i, **(changes or {}).get(i, {}))
            h.append(sample, T0+DT*i+1)
        return h

    def report(self, snap, **kwargs):
        settings = {"start_ns":T0+5*DT, "end_ns":T0+13*DT-1, "now_ns":T0+14*DT+1,
                    "actor_ref":"rover", "frame_id":"odom", "child_frame_id":"base_foot"}
        return stationary_window(snap, **{**settings, **kwargs})

    def test_bracketing_and_precision(self):
        h = self.history()
        report = self.report(h.snapshot())
        self.assertTrue(report["stationary"])
        self.assertLessEqual(report["first_ns"], T0+5*DT)
        self.assertGreaterEqual(report["last_ns"], T0+13*DT-1)
        self.assertGreaterEqual(report["last_ns"] - report["first_ns"], 450_000_000)
        self.assertEqual(report["max_gap_ns"], DT)
        self.assertEqual(report["max_rotation_rad"], 0)
        self.assertEqual(report["epoch"], h.epoch)
        self.assertEqual(str(T0), h.snapshot()["samples"][0]["stamp_ns"])

    def test_orientation_sign_yaw_wrap_and_roll_excursion(self):
        h = OdomHistory(CFG)
        for i, (degrees, negative) in enumerate([(179.8,False),(-179.9,True),(-179.7,False)]):
            h.append(msg(i, angle=math.radians(degrees), flip=negative), T0+DT*i)
        rows = h.snapshot()["samples"]
        self.assertAlmostEqual(180.3, math.degrees(rows[-1]["yaw_unwrapped_rad"]), places=8)
        self.assertGreater(sum(a*b for a,b in zip(rows[0]["orientation_xyzw"], rows[1]["orientation_xyzw"])), 0)
        with self.assertRaisesRegex(SpatialFactError, "motion_detected"):
            self.report(self.history({8:{"angle":0.06,"roll":True}}).snapshot())

    def test_moving_in_middle_even_if_start_and_end_match(self):
        for excursion in ({"xy":0.021}, {"angle":0.045}, {"speed":0.04}):
            with self.subTest(excursion=excursion), self.assertRaisesRegex(SpatialFactError, "motion_detected"):
                self.report(self.history({10:excursion}).snapshot())
        self.assertEqual(self.report(self.history({10:{"flip":True}}).snapshot())["max_rotation_rad"], 0)

    def test_future_stale_missing_bound_and_different_actor(self):
        snap = self.history().snapshot()
        for code, change in (("odometry_stale", {"now_ns":T0+30*DT}),
                             ("coverage_missing", {"start_ns":T0-1}),
                             ("coverage_missing", {"end_ns":T0+14*DT+1}),
                             ("binding_mismatch", {"actor_ref":"another"})):
            with self.subTest(reason=code), self.assertRaisesRegex(SpatialFactError, code):
                self.report(snap, **change)
        broken = copy.deepcopy(snap)
        del broken["samples"][3:7]
        with self.assertRaisesRegex(SpatialFactError, "odometry_gap"):
            self.report(broken)

    def test_reader_resets_continuity_on_gap_backwards_or_invalid(self):
        h = self.history()
        old = h.epoch
        self.assertFalse(h.append(msg(14), T0+DT*14))
        self.assertEqual(len(h.samples), 15)
        h.append(msg(22), T0+DT*22)
        self.assertNotEqual(old, h.epoch)
        self.assertEqual(len(h.samples), 1)
        old = h.epoch
        h.append(msg(21), T0+DT*22)
        self.assertNotEqual(old, h.epoch)
        bad = msg(22)
        bad.pose.pose.orientation.w = 0
        with self.assertRaisesRegex(SpatialFactError, "invalid_odometry_quaternion"):
            h.append(bad, T0+DT*22)
        self.assertEqual(len(h.samples), 0)


if __name__ == "__main__":
    unittest.main()
