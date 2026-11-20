"""Multi-source read-only transport, camera calibration identity, and position-only fusion."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_contracts.spatial_facts import SpatialFactError
from perception.spatial_facts.camera_info_capture import camera_info, publish
from perception.spatial_facts.gateway import published_batch
from perception.spatial_facts.weighted import localize


def camera_message(stamp=101):
    return SimpleNamespace(header=SimpleNamespace(frame_id="color_optical", stamp=SimpleNamespace(sec=0, nanosec=stamp)),
              width=640, height=480, distortion_model="plumb_bob", binning_x=0, binning_y=0,
              k=[510.,0.,320.,0.,511.,240.,0.,0.,1.], r=[1.,0.,0.,0.,1.,0.,0.,0.,1.],
              d=[0.0]*5, p=[510.,0.,320.,0.,0.,511.,240.,0.,0.,0.,1.,0.],
              roi=SimpleNamespace(x_offset=0,y_offset=0,width=0,height=0,do_rectify=False))


class SpatialGateway(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.required = self.root / "odom.json"
        self.camera = self.root / "camera"
        self.camera.mkdir()
        self.source = "camera.color.info"
        self.sample = {"kind":"position", "actor_ref":"rover", "source_id":"wheel", "frame_id":"odom",
                       "clock_id":"unix", "stamp_ns":101, "map_ref":"sha256:"+"9"*64, "localization_epoch":"one",
                       "calibration_ref":"wheel.v1", "position_m":[1.0,2.0], "variance_m2":[0.2,0.3]}
        self.required.write_text(json.dumps({"schema_version":1,"basis":"published","actor_ref":"rover",
                                             "clock_id":"unix","samples":[self.sample]}), encoding="utf8")

    def capture(self, stamp=101):
        sample, data = camera_info(camera_message(stamp), "rover", self.source, "color_optical")
        publish(self.camera, sample, data)
        return sample, data

    def collect(self, now=105, path=None):
        return published_batch(self.required, "rover", now, 8, [(self.source, path or self.camera/"observations.json")])

    def test_distinct_clocks_frames_and_independent_consumers(self):
        sample, data = self.capture()
        batch = self.collect()
        self.assertEqual(["wheel", self.source], [s["source_id"] for s in batch["samples"]])
        self.assertNotIn("source_gaps", batch)
        self.assertEqual(sample, batch["samples"][1])
        self.assertEqual("color_optical", sample["frame_id"])
        self.assertNotIn("position_m", sample)
        self.assertEqual(data, (self.camera/"artifacts"/(sample["artifact_ref"][7:]+".json")).read_bytes())
        self.assertEqual(hashlib.sha256(data).hexdigest(), sample["calibration_ref"][7:])
        self.assertEqual(510, json.loads(data)["k"][0])
        policy = {**{k:self.sample[k] for k in ("map_ref","frame_id","localization_epoch","clock_id")},
                  "max_age_ns":8, "max_skew_ns":2, "sources":{"wheel":"wheel.v1"}}
        pose = localize(batch, policy)
        self.assertEqual(self.sample["position_m"], pose["position_m"])
        self.assertEqual(["wheel"], [s["source_id"] for s in pose["sources"]])

    def test_artifact_stable_across_stamps_changes_with_intrinsics_and_has_quota(self):
        first, _ = self.capture(100)
        second, _ = self.capture(102)
        self.assertEqual(first["artifact_ref"], second["artifact_ref"])
        self.assertNotEqual(first["stamp_ns"], second["stamp_ns"])
        self.assertEqual(1, len(list((self.camera / "artifacts").iterdir())))
        msg = camera_message()
        msg.k[0] += 2.
        sample, data = camera_info(msg, "rover", self.source, "color_optical")
        self.assertNotEqual(first["artifact_ref"], sample["artifact_ref"])
        for index in range(31):
            (self.camera / "artifacts" / str(index)).touch()
        with self.assertRaisesRegex(SpatialFactError, "artifact_limit"):
            publish(self.camera, sample, data)
        self.capture(103)

    def test_optional_gaps_are_explicit_and_required_staleness_blocks(self):
        missing = self.collect()
        self.assertEqual([{"source_id":self.source, "reason":"sensing.source_missing"}], missing["source_gaps"])
        self.capture(90)
        stale = self.collect()
        self.assertEqual([{"source_id":self.source, "reason":"sensing.sample_stale"}], stale["source_gaps"])
        self.assertEqual([self.sample], stale["samples"])
        self.capture(119)
        with self.assertRaisesRegex(SpatialFactError, "sample_stale"):
            self.collect(now=120)

    def test_reject_other_actor_future_input_duplicate_and_private_metadata(self):
        self.capture()
        path = self.camera / "observations.json"
        original = json.loads(path.read_bytes())
        with self.assertRaisesRegex(SpatialFactError, "duplicate_source"):
            published_batch(self.required, "rover", 105, 8, [("wheel", path)])
        for patch in ({"actor_ref":"other"}, {"private_debug":"operator secret"}):
            with self.subTest(patch=patch), self.assertRaises(SpatialFactError):
                path.write_text(json.dumps(dict(original, **patch)), encoding="utf8")
                self.collect()
        original["samples"][0]["source_id"] = "another"
        path.write_text(json.dumps(original), encoding="utf8")
        with self.assertRaisesRegex(SpatialFactError, "source_mismatch"):
            self.collect()
        with self.assertRaisesRegex(SpatialFactError, "sample_stale"):
            self.collect(now=50)

    def test_wrong_frame_and_uncalibrated_camera_rejected(self):
        msg = camera_message()
        class RosFloat(float):
            pass
        msg.k = [RosFloat(value) for value in msg.k]
        self.assertEqual(510, json.loads(camera_info(msg, "rover", self.source, "color_optical")[1])["k"][0])
        with self.assertRaisesRegex(SpatialFactError, "binding_mismatch"):
            camera_info(msg, "rover", self.source, "different_frame")
        msg.k[0] = 0
        with self.assertRaisesRegex(SpatialFactError, "uncalibrated"):
            camera_info(msg, "rover", self.source, "color_optical")

    @unittest.skipUnless(os.name=="posix", "POSIX gateway filesystem")
    def test_optional_symlink_and_fifo_rejected(self):
        self.capture()
        link = self.root / "unapproved"
        link.symlink_to(self.camera / "observations.json")
        with self.assertRaisesRegex(SpatialFactError, "invalid_source_file"):
            self.collect(path=link)
        link.unlink()
        os.mkfifo(link)
        with self.assertRaisesRegex(SpatialFactError, "invalid_source_file"):
            self.collect(path=link)


if __name__ == "__main__":
    unittest.main()
