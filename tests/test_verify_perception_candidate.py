"""The independent release gate detects forged geometry, content and negative-test replies."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import verify_perception_candidate as gate
import test_visual_grounding as provider_fixture


class CandidateQualification(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        fixture = provider_fixture.VisualGroundingTest()
        fixture.setUp()
        self.batch, self.profile = fixture.batch, fixture.profile
        self.answer = fixture.result()
        self.profile["mounts"] = {}
        for s in self.batch["samples"]:
            sid, ref = s["source_id"], s["artifact_ref"]
            suffix, kind = (".json","calibration") if sid == "info" else (".png","image")
            self.profile["mounts"][sid] = {"root":sid, "format":kind}
            directory = self.root/sid/"artifacts"
            directory.mkdir(parents=True)
            (directory/(ref[7:]+suffix)).write_bytes(fixture.store[ref])
        self.settings = self.root/"profile.json"
        self.settings.write_text(json.dumps(self.profile))
        self.args = SimpleNamespace(profile=self.settings, query=provider_fixture.QUERY, inputs=self.root,
                                    actor="rover", timeout=2, backend_digest="sha256:"+"a"*64,
                                    candidate_digest="sha256:"+"d"*64)
        self.release = {"profile_digest":gate.sha(self.settings.read_bytes()),"backend_digest":self.args.backend_digest,
                        "result":self.answer}

    def check_result(self, result):
        return gate.verify_target(result,self.batch,self.profile,self.root,self.args.query,"rover")

    def call(self, negative=(422,{"error":{"code":"grounding.unsupported_target"}}), first=None):
        provider = [(200,self.batch)]
        if first:
            provider.extend([first,(200,self.batch)])
        provider.extend([(200,self.release),negative])
        with patch.object(gate,"exchange",side_effect=provider), patch.object(gate.time,"time_ns",return_value=3_000_000_000):
            return gate.qualify(self.args)

    def test_accepts_checked_rgbd_after_skewed_first_batch(self):
        result = self.call(first=(422,{"error":{"code":"grounding.unsynchronized_input"}}))
        self.assertEqual(result["checks"], gate.CHECKS)
        self.assertTrue(result["passed"])
        self.assertEqual(result["candidate_digest"], self.args.candidate_digest)
        self.assertEqual(result["raw_depth"],500)
        self.assertEqual(result["calibration_age_ns"],700_000_000)
        self.assertEqual(result["attempts"],2)
        self.assertTrue(gate.DIGEST.fullmatch(result["receipt_sha256"]))

    def test_detects_forged_source_geometry_query_or_mask(self):
        for mutate in (
            lambda a:a["point_camera_optical_m"].__setitem__(2,.72),
            lambda a:a.update(point_depth_raw=999),
            lambda a:a.update(point_pixel=[0,0]),
            lambda a:a.update(bbox_xyxy=[0,0,80,60]),
            lambda a:a["mask"]["runs"].append([0,0,1]),
            lambda a:a.update(query="unrelated"),
            lambda a:a.update(rgb_ref="sha256:"+"0"*64)):
            result=deepcopy(self.answer)
            mutate(result)
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):
                self.check_result(result)
        rgb_ref=self.answer["rgb_ref"][7:]
        (self.root/"color"/"artifacts"/(rgb_ref+".png")).write_bytes(b"replaced")
        with self.assertRaisesRegex(ValueError,"artifact_sha256"):
            self.check_result(self.answer)

    def test_negative_and_release_provenance_are_mandatory(self):
        with self.assertRaisesRegex(ValueError,"unsupported_target_accepted"):
            self.call(negative=(422,{"error":{"code":"perception.invalid_request"}}))
        self.release["backend_digest"]="sha256:"+"0"*64
        with self.assertRaisesRegex(ValueError,"release_mismatch"):
            self.call()


if __name__ == "__main__":
    unittest.main()
