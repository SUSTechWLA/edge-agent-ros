"""SDD-040. Independently known rigid transforms and invalid fixture captures."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calibration.targets import fit


def fixture(rotation):
    offset = np.array([.32, -.18, .54])
    pts = [[0,0,0], [1,0,0], [0,1,0], [0,0,1], [.2,.5,.1], [.7,.3,.3], [.6,.8,.7]]
    rows = [dict(id=str(i), sensor=p, base=(rotation @ p + offset).tolist()) for i,p in enumerate(pts)]
    return dict(unit="m", robot_id="fixture-car", serial="test-lidar", parent_frame="base_link",
                child_frame="lidar", reference="fixture-test", fit=rows[:4], validation=rows[4:])


class Targets(unittest.TestCase):
    def test_known_transforms(self):
        for angle in (.7, np.pi):
            c,s = np.cos(angle), np.sin(angle)
            rotation = np.array([[1,0,0], [0,c,-s], [0,s,c]])
            raw = json.dumps(fixture(rotation)).encode()
            result = fit(raw)
            np.testing.assert_allclose(result["translation_m"], [.32,-.18,.54], atol=1e-12)
            q = np.asarray(result["rotation_xyzw"])
            wanted = [np.sin(angle/2),0,0,np.cos(angle/2)]
            self.assertAlmostEqual(abs(np.dot(q,wanted)), 1)
            self.assertLess(result["validation_max_m"],1e-12)
            self.assertEqual(result["input_sha256"], hashlib.sha256(raw).hexdigest())

    def test_holdout_not_reused_and_errors_fail_closed(self):
        for case in ("other_sensor","changed_id","same_id","collinear","units","reflection","nan"):
            with self.subTest(case=case):
                doc = fixture(np.eye(3))
                if case == "other_sensor": doc["validation"][0]["base"][0] += .12
                if case == "changed_id": doc["validation"][0]["sensor"] = copy.copy(doc["fit"][0]["sensor"])
                if case == "same_id": doc["validation"][0]["id"] = doc["fit"][0]["id"]
                if case == "collinear":
                    for i,row in enumerate(doc["fit"]): row["sensor"] = [i,0,0]
                if case == "units": doc["unit"] = "mm"
                if case == "reflection": doc = fixture(np.diag([-1,1,1]))
                if case == "nan": doc["fit"][0]["base"][0] = float("nan")
                with self.assertRaises(ValueError):
                    fit(json.dumps(doc).encode())


if __name__ == "__main__":
    unittest.main()
