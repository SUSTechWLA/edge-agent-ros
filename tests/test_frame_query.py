"""Verify frame queries independently on known RGB and calibrated depth pixels."""
import unittest

import test_visual_grounding
from edge_contracts.spatial_facts import SpatialFactError
from perception.spatial_facts.frame_query import execute, parse


class FrameQueryTest(unittest.TestCase):
    def setUp(self):
        self.f = test_visual_grounding.VisualGroundingTest()
        self.f.setUp()

    def read(self, program):
        f = self.f
        return execute(program, f.batch, f.store, f.profile, now_ns=f.now)

    def test_crop_and_percentile(self):
        result = self.read("depth | crop(.5,0,1,1) | grid(1,2) | quantile(0.5)")
        self.assertEqual([r["value"] for r in result["regions"]], [.5, .5])
        self.assertEqual(result["regions"][0]["bounds_xyxy"], [40, 0, 60, 60])
        self.assertEqual(result["minimum_indices"], [0, 1])
        self.assertEqual(result["depth_ref"], self.f.batch["samples"][1]["artifact_ref"])
        self.assertEqual(self.read("red | grid(2,2) | min")["regions"][0]["value"], 25)
        self.assertAlmostEqual(self.read("brightness | max")["regions"][0]["value"], .299*150+.587*157+.114*153)

    def test_invalid_pixels_excluded_except_for_coverage_denominator(self):
        f = self.f
        f.depths = [v for _ in range(f.w*f.h//4) for v in (0, 400, 800, 65535)]
        f.render()
        for stat, expected in (("min",.4), ("max",.8), ("mean",.6), ("median",.6), ("quantile(.25)",.4), ("coverage",50)):
            row = self.read("depth | "+stat)["regions"][0]
            self.assertAlmostEqual(row["value"], expected)
            self.assertEqual(row["valid_pixels"]*2, row["pixels"])
        f.depths = [0] * (f.w*f.h)
        f.render()
        self.assertEqual(self.read("depth | coverage")["regions"][0]["value"], 0)
        with self.assertRaisesRegex(SpatialFactError, "no_valid_pixels"):
            self.read("depth | median")

    def test_unsupported_programs_and_empty_regions(self):
        for text in ("", "depth", "depth | grid(8,8) | mean", "depth | grid(0,2) | min", "depth | quantile(9)",
                     "depth | crop(1,0,0,1) | min", "red | import(os)", "depth | min | grid(2,2)", "depth | "+"0"*128):
            with self.subTest(program=text), self.assertRaisesRegex(SpatialFactError, "invalid_program"):
                parse(text)
        with self.assertRaisesRegex(SpatialFactError, "empty_region"):
            self.read("depth | crop(0,0,.001,1) | grid(1,3) | mean")


if __name__ == "__main__":
    unittest.main()
