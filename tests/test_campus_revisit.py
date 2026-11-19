"""Admission, evidence provenance and selection checks for inspecting a previously observed site."""
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent/"system"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campus_revisit import select_inspection_site


class InspectionSite(unittest.TestCase):
    def setUp(self):
        self.grid = dict(frame="odom", width=60, height=60, resolution=.1,
                         origin=[-3, -3, 0], cells=[0]*3600)
        self.bounds = [-3, -3, 3, 3]
        self.xy = [-1.2, .05]
        self.home = [0, 0]

    def select(self, steps):
        return select_inspection_site(self.grid, self.xy, self.home, steps, self.bounds)

    def receipt(self, index, xy):
        return dict(index=index, measured_xy=xy, accepted=True, core_state="SUCCEEDED",
                    mode="survey", goal=[99, 100])

    def test_selects_measured_stop_with_largest_margin(self):
        edge = self.receipt(1, [1.05, .05])
        best = self.receipt(2, [.65, .05])
        station = self.select([edge, best])
        self.assertEqual((station["target"], station["source_step"]), (best["measured_xy"], 2))
        self.assertGreaterEqual(station["path_m"], 1.8)
        self.assertAlmostEqual(station["clearance_m"], 2.35)
        self.assertAlmostEqual(station["distance_from_home_m"], math.hypot(.65,.05))

    def test_only_stopped_distinct_survey_receipts(self):
        steps = [self.receipt(1, self.xy), self.receipt(2, self.home),
                 {**self.receipt(3, [1, 1]), "accepted": False},
                 {**self.receipt(4, [1, 1]), "core_state": "CANCELED"},
                 {**self.receipt(5, [1, 1]), "mode": "return"},
                 {**self.receipt(6, [1, 1]), "measured_xy": [float("nan"), 0]}]
        self.assertIsNone(self.select(steps))

    def test_changed_map_unknown_and_occupied_cut_off_stop(self):
        stop = self.receipt(1, [1, 0])
        self.assertIsNotNone(self.select([stop]))
        for value in (100, -1, 50):
            for row in range(60):
                self.grid["cells"][row*60+35] = value
            self.assertIsNone(self.select([stop]))

    def test_station_must_support_return_home(self):
        for row in range(60):
            self.grid["cells"][row*60+35] = -1
        self.home = [1.5, .05]
        self.assertIsNone(self.select([self.receipt(1, [-.5, 0])]))

    def test_extra_parking_margin_and_frame(self):
        self.assertIsNone(self.select([self.receipt(1, [2.66, 0])]))
        self.grid["frame"] = "world"
        with self.assertRaisesRegex(ValueError, "station_requires_odom_frame"):
            self.select([])


if __name__ == "__main__":
    unittest.main()
