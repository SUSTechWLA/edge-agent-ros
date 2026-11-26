import hashlib
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_contracts.image_frames import raw_frame
from edge_contracts.spatial_facts import SpatialFactError
from perception.spatial_facts.image_capture import depth_png
from perception.spatial_facts.visual_grounding import ground_target


QUERY = "识别画面右侧的大块浅灰色平面，给出二维边框和三维位置"


class VisualGroundingTest(unittest.TestCase):
    def setUp(self):
        self.w, self.h = 80, 60
        self.now = 3_000_000_000
        self.stamp = self.now - 100_000_000
        self.rgb = bytearray()
        self.depths = []
        for y in range(self.h):
            for x in range(self.w):
                gray = x >= 45 and 5 <= y < 55
                self.rgb.extend((150, 157, 153) if gray else (25, 30, 31))
                self.depths.append(500 if gray else 2300)
        self.cal = {"type": "sensor_msgs/msg/CameraInfo", "width": self.w, "height": self.h,
                    "frame_id": "color_optical", "k": [100, 0, 39.5, 0, 100, 29.5, 0, 0, 1],
                    "p": [100, 0, 39.5, 0, 0, 100, 29.5, 0, 0, 0, 1, 0], "r": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                    "distortion_model": "plumb_bob", "d": [0, 0, 0, 0, 0], "binning_x": 0, "binning_y": 0,
                    "roi": {"x_offset": 0, "y_offset": 0, "width": 0, "height": 0, "do_rectify": False}}
        self.profile = {"actor_ref": "rover", "sources": {"aligned": {"kind": "depth16", "frame_id": "color_optical",
                        "depth_scale_m": .001, "depth_scale_ref": "device.test_calibration"}},
                        "grounding": {"rgb_source": "color", "calibration_source": "info", "depth_source": "aligned"}}
        self.render()

    def render(self):
        image = raw_frame(SimpleNamespace(width=self.w, height=self.h, step=self.w * 3,
                          data=self.rgb, encoding="rgb8"))[0]
        depth = depth_png(SimpleNamespace(width=self.w, height=self.h, step=self.w * 2, encoding="16UC1",
                          is_bigendian=False, data=struct.pack("<" + str(len(self.depths)) + "H", *self.depths)))
        cal = json.dumps(self.cal).encode()
        self.store = {"sha256:" + hashlib.sha256(a).hexdigest(): a for a in (image, depth, cal)}
        color_ref, depth_ref, info_ref = self.store
        samples = [{"kind": "artifact", "actor_ref": "rover", "clock_id": "unix", "source_id": sid,
                    "frame_id": "color_optical", "stamp_ns": str(self.stamp + dt),
                    "artifact_ref": ref, "calibration_ref": info_ref, "media_type": mt}
                   for sid, dt, ref, mt in (("color", 0, color_ref, "image/png"),
                       ("aligned", -33_000_000, depth_ref, "image/png"), ("info", -600_000_000, info_ref, "application/json"))]
        self.batch = {"schema_version": 1, "actor_ref": "rover", "clock_id": "unix",
                      "basis": "published", "as_of_ns": str(self.now), "samples": samples}

    def result(self, query=QUERY):
        return ground_target(query, self.batch, self.store, self.profile, now_ns=self.now)

    def test_measured_point_mask_and_optical_projection(self):
        result = self.result()
        self.assertEqual(result["bbox_xyxy"], [45, 5, 80, 55])
        u, v = result["point_pixel"]
        self.assertTrue(any(y == v and a <= u < b for y, a, b in result["mask"]["runs"]))
        self.assertEqual(result["point_depth_raw"], 500)
        self.assertEqual(result["point_camera_optical_m"], [(u - 39.5) / 100 * .5, (v - 29.5) / 100 * .5, .5])
        self.assertEqual(result["quality"]["plane_inlier_fraction"], 1.)
        self.assertEqual(result["calibration_stamp_ns"], self.batch["samples"][2]["stamp_ns"])
        self.assertEqual(result["rgb_depth_skew_ns"], 33_000_000)
        self.assertTrue(all(result[n + "_ref"] in self.store for n in ("depth", "rgb", "calibration")))

    def test_unsupported_or_absent_descriptions(self):
        for q, code in (("寻找身边的灭火器", "unsupported_target"), ("不要定位右侧的大块浅灰色平面", "unsupported_target"),
                        (QUERY.replace("右侧", "左侧"), "target_absent")):
            with self.subTest(q=q), self.assertRaisesRegex(SpatialFactError, code):
                self.result(q)

    def test_rejects_skewed_images_despite_equal_calibration_and_fresh_batch(self):
        self.batch["samples"][1]["stamp_ns"] = self.stamp - 40_000_001
        with self.assertRaisesRegex(SpatialFactError, "unsynchronized_input"):
            self.result()

    def test_calibration_time_is_independent_but_must_be_fresh(self):
        self.batch["samples"][2]["stamp_ns"] = self.now - 2_000_000_001
        with self.assertRaisesRegex(SpatialFactError, "stale_input"):
            self.result()

    def test_requires_depth_scale_intrinsics_and_content_binding(self):
        self.profile["sources"]["aligned"]["depth_scale_ref"] = ""
        with self.assertRaisesRegex(SpatialFactError, "unverified_depth_scale"):
            self.result()
        self.profile["sources"]["aligned"]["depth_scale_ref"] = "test.scale"
        self.batch["samples"][1]["frame_id"] = "depth_optical"
        with self.assertRaisesRegex(SpatialFactError, "registration_mismatch"):
            self.result()
        self.cal["d"][0] = .1
        self.render()
        with self.assertRaisesRegex(SpatialFactError, "intrinsics_unsupported"):
            self.result()
        self.store[self.batch["samples"][2]["artifact_ref"]] = b"{}"
        with self.assertRaisesRegex(SpatialFactError, "artifact_digest_mismatch"):
            self.result()

    def test_fails_without_depth_or_planarity(self):
        self.depths = [0] * (self.w * self.h)
        self.render()
        with self.assertRaisesRegex(SpatialFactError, "depth_insufficient"):
            self.result()
        self.depths = [400 + (n % 5) * 160 for n in range(self.w * self.h)]
        self.render()
        with self.assertRaisesRegex(SpatialFactError, "plane_unverified"):
            self.result()


if __name__ == "__main__":
    unittest.main()
