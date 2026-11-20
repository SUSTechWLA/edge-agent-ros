"""SDD-051. Public observation and position-fusion service contracts on a replay clock."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edge_contracts.spatial_facts import SpatialFactError
from perception.spatial_facts import replay, weighted


class ServiceContext:
    def __init__(self, settings):
        self.settings = settings
        self.exports = {}

    def config(self):
        return {"settings": self.settings}

    def provide(self, name, invoke):
        self.exports[name] = invoke


class SpatialFactsTests(unittest.TestCase):
    def setUp(self):
        map_ref = "sha256:" + "1" * 64
        wheel = {"source_id": "wheel-fix", "calibration_ref": "wheel-v2", "kind": "position",
                 "map_ref": map_ref, "localization_epoch": "boot-1", "clock_id": "replay-a",
                 "frame_id": "map", "stamp_ns": 99, "position_m": [0, 3], "variance_m2": [1, 4]}
        lidar = {**wheel, "source_id": "laser-fix", "calibration_ref": "laser-v1", "stamp_ns": 100,
                 "position_m": [4, 5], "variance_m2": [3, 4]}
        image = {"source_id": "depth", "kind": "artifact", "calibration_ref": "depth-v1",
                 "clock_id": "camera", "frame_id": "depth_optical", "stamp_ns": 38,
                 "artifact_ref": "sha256:" + "2" * 64, "media_type": "image/png"}
        self.settings = {"actors": {"rover": {"as_of_ns": 101, "clock_id": "replay-a",
                                               "samples": [lidar, image, wheel]}}}
        self.policy = {"map_ref": map_ref, "localization_epoch": "boot-1", "frame_id": "map",
                       "clock_id": "replay-a", "max_age_ns": 10, "max_skew_ns": 2,
                       "sources": {"wheel-fix": "wheel-v2", "laser-fix": "laser-v1"}}

    def test_public_services_and_independent_image_consumer(self):
        source_ctx, fuse_ctx = ServiceContext(self.settings), ServiceContext({"actors": {"rover": self.policy}})
        replay.apply(source_ctx)
        weighted.apply(fuse_ctx)
        batch = source_ctx.exports["sensing.observations"]({"actor_ref": "rover"})
        image = next(s for s in batch["samples"] if s["kind"] == "artifact")
        self.assertEqual(("depth_optical", "camera", "image/png"),
                         (image["frame_id"], image["clock_id"], image["media_type"]))
        pose = fuse_ctx.exports["localization.position"](batch)
        self.assertEqual([1, 4], pose["position_m"])
        self.assertEqual([0.75, 2], pose["variance_m2"])
        self.assertEqual((99, 101, 109, "replay"),
                         (pose["stamp_ns"], pose["as_of_ns"], pose["valid_until_ns"], pose["basis"]))
        self.assertEqual(["laser-fix", "wheel-fix"], [s["source_id"] for s in pose["sources"]])
        self.assertNotIn("samples", pose)
        batch["samples"][0]["position_m"].clear()
        self.assertEqual([4, 5], self.settings["actors"]["rover"]["samples"][0]["position_m"])

    def test_nonplanar_estimates_and_alternative_producer(self):
        batch = replay.capture(self.settings, "rover")
        for sample in batch["samples"]:
            if sample["kind"] == "position":
                sample["position_m"].append(42)
                sample["variance_m2"].append(0.5)
        batch["basis"] = "published"
        result = weighted.localize(batch, self.policy)
        self.assertEqual([1, 4, 42], result["position_m"])
        self.assertEqual("published", result["basis"])
        # A registered single-source policy is explicit; missing required sources never falls back.
        self.policy["sources"] = {"laser-fix": "laser-v1"}
        self.assertEqual([4, 5, 42], weighted.localize(batch, self.policy)["position_m"])

    def test_mismatched_samples_fail_closed(self):
        batch = replay.capture(self.settings, "rover")
        for field, bad, reason in [
            ("actor_ref", "other", "sensing.actor_mismatch"),
            ("frame_id", "odom", "localization.frame_mismatch"),
            ("clock_id", "unsynced", "localization.clock_mismatch"),
            ("map_ref", "sha256:" + "3" * 64, "localization.map_mismatch"),
            ("localization_epoch", "boot-2", "localization.epoch_mismatch"),
            ("calibration_ref", "wheel-v3", "localization.calibration_mismatch"),
            ("stamp_ns", 0, "localization.stale"),
            ("stamp_ns", 102, "localization.future_sample"),
            ("stamp_ns", 95, "localization.time_skew"),
            ("position_m", [1, 500], "localization.inconsistent_sources"),
            ("variance_m2", [1, 0], "sensing.invalid_position"),
            ("position_m", [False, 0], "sensing.invalid_position"),
        ]:
            with self.subTest(field=field, bad=bad):
                faulty = deepcopy(batch)
                faulty["samples"][-1][field] = bad
                with self.assertRaises(SpatialFactError) as failure:
                    weighted.localize(faulty, self.policy)
                self.assertEqual(reason, failure.exception.code)

    def test_duplicate_missing_and_unequal_dimension(self):
        batch = replay.capture(self.settings, "rover")
        sample = batch["samples"].pop()
        with self.assertRaisesRegex(SpatialFactError, "source_missing"):
            weighted.localize(batch, self.policy)
        batch["samples"].extend([sample, sample])
        with self.assertRaisesRegex(SpatialFactError, "duplicate_source"):
            weighted.localize(batch, self.policy)
        batch["samples"].pop()
        sample["position_m"].append(0)
        sample["variance_m2"].append(1)
        with self.assertRaisesRegex(SpatialFactError, "dimension_mismatch"):
            weighted.localize(batch, self.policy)

    def test_replay_and_policy_scope(self):
        with self.assertRaisesRegex(SpatialFactError, "actor_unavailable"):
            replay.capture(self.settings, "other")
        batch = replay.capture(self.settings, "rover")
        batch["clock_id"] = "different-clock"
        with self.assertRaisesRegex(SpatialFactError, "clock_mismatch"):
            weighted.localize(batch, self.policy)
        with self.assertRaisesRegex(SpatialFactError, "invalid_policy"):
            weighted.localize(batch, {**self.policy, "sources": {}})


if __name__ == "__main__":
    unittest.main()
