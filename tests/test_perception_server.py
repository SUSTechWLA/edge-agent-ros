"""Private HTTP backend pins geometry and only reads published sensor ring entries."""
import http.client
from http.server import HTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_contracts.spatial_facts import SpatialFactError
from perception.spatial_facts.artifact_ring import ArtifactRing
from perception.spatial_facts.perception_server import Perception, handler
from test_stationary_fusion import NOW, scene


DIGEST = "sha256:" + "a"*64


class Backend(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        batch, files, profile, _ = scene()
        self.profile, self.batch = profile, batch
        self.request = {"actor_ref": "rover", "observations": batch}
        profile["schema_version"] = 1
        profile["mounts"] = {}
        for sample in batch["samples"]:
            sid = sample["source_id"]
            profile["mounts"][sid] = {"root": sid, "format": "livox"}
            directory = self.root/sid
            directory.mkdir()
            store = ArtifactRing(directory, "rover", sid, media_type="application/json", suffix=".json")
            store.publish({**sample, "stamp_ns": int(sample["stamp_ns"])}, files[sample["artifact_ref"]])
        history = {"schema_version": 1, "actor_ref": "rover", "frame_id": "odom", "source_id": "wheel.odometry",
                   "child_frame_id": "base_foot", "clock_id": "unix", "epoch": "boot1",
                   "localization_epoch": "site1", "topic": "/wheel", "samples": []}
        for i in range(16):
            history["samples"].append({"stamp_ns": str(NOW-(15-i)*50_000_000), "position_m": [0.,0.,0.],
                                       "orientation_xyzw": [0.,0.,0.,1.], "angular_radps": [0.,0.,0.],
                                       "linear_mps": [0.,0.,0.]})
        (self.root/"witness").mkdir()
        (self.root/"witness/history.json").write_text(json.dumps(history))
        self.settings = self.root/"settings.json"
        self.save()
        self.fixed_time = patch("perception.spatial_facts.perception_server.time.time_ns", return_value=NOW)
        self.fixed_time.start()
        self.addCleanup(self.fixed_time.stop)

    def save(self):
        self.settings.write_text(json.dumps(self.profile))
        self.backend = Perception(self.settings, self.root, DIGEST)

    def test_live_http_fusion_and_invalid_requests(self):
        with HTTPServer(("127.0.0.1", 0), handler(self.backend)) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                for path, body, status in (("/fuse",self.request,200), ("/fuse",{},400), ("/unknown",{},404),
                                           ("/ground",{**self.request, "target":"wall"},400)):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                    conn.request("POST", path, body=json.dumps(body), headers={"Content-Type":"application/json"})
                    reply = conn.getresponse()
                    data = json.loads(reply.read())
                    conn.close()
                    self.assertEqual(reply.status, status, data)
                    if status == 200:
                        self.assertEqual(set(data), {"result","profile_digest","backend_digest"})
                        self.assertEqual(data["result"]["overlap_voxels"], 1)
                        self.assertEqual(data["result"]["witness"]["epoch"], "boot1")
                        self.assertEqual(data["backend_digest"], DIGEST)
                    else:
                        self.assertIsInstance(data["error"]["code"], str)
            finally:
                server.shutdown()
                worker.join(2)

    def test_unindexed_or_changed_evidence_fails_closed(self):
        first = self.batch["samples"][0]
        original = first["stamp_ns"]
        first["stamp_ns"] = str(int(first["stamp_ns"]) - 1)
        with self.assertRaisesRegex(SpatialFactError, "sample_unavailable"):
            self.backend.evaluate("/fuse", self.request)
        first["stamp_ns"] = original
        ref = first["artifact_ref"][7:]
        (self.root/first["source_id"]/"artifacts"/(ref+".json")).write_text("{}")
        with self.assertRaisesRegex(SpatialFactError, "digest_mismatch"):
            self.backend.evaluate("/fuse", self.request)

    def test_profiles_reject_path_traversal_and_shared_mount(self):
        for value in ("../other", "/etc", "name//root", "lidar.rear"):
            self.profile["mounts"]["lidar.front"]["root"] = value
            with self.subTest(value=value), self.assertRaises(SpatialFactError):
                self.save()

    def test_calibration_bytes_bound_to_site_writer(self):
        info = "d435.info"
        self.profile["mounts"][info] = {"root": "cam", "format": "calibration"}
        self.save()
        original = self.batch["samples"][0]
        sample = {**original, "source_id": info, "stamp_ns": int(original["stamp_ns"]),
                  "calibration_ref": original["artifact_ref"]}
        cap = self.root/"cam"
        cap.mkdir()
        (cap/"artifacts").mkdir()
        ref = sample["artifact_ref"][7:] + ".json"
        (cap/"artifacts"/ref).write_bytes((self.root/"lidar.front"/"artifacts"/ref).read_bytes())
        current = {"actor_ref":"rover", "basis":"published","samples":[sample]}
        (cap/"observations.json").write_text(json.dumps(current))
        self.assertIn(sample["artifact_ref"],self.backend.artifacts({info:sample},[info]))
        current["samples"][0] = {**sample, "frame_id":"other"}
        (cap/"observations.json").write_text(json.dumps(current))
        with self.assertRaisesRegex(SpatialFactError, "calibration_binding_mismatch"):
            self.backend.artifacts({info:sample},[info])

    def test_site_profile_identifies_optical_and_body_sources(self):
        site = Path(__file__).resolve().parents[1]/"examples/stationary-perception/jetson-rover.json"
        backend = Perception(site, self.root, DIGEST)
        self.assertEqual(set(backend.profile["sources"]), {"mid360.front", "mid360.rear"})
        self.assertEqual(backend.ground_profile["sources"]["d435.aligned.depth"]["frame_id"],
                         "camera_color_optical_frame")


if __name__ == "__main__":
    unittest.main()
