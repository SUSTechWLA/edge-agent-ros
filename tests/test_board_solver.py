import copy
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from calibration.board_solver import (detect_board, distinct_pose, fit_extrinsics,
                                      observe_lidar, verify_extrinsics)
from calibration.board_provider import detect, encode_frame, measure


PROFILE = dict(inner_corners=[6, 4], square_m=.08, margin_m=.04, camera_frame="optical",
               lidar_frames={"left": "left_lidar", "right": "right_lidar"})


def sample_planes():
    angles = [(0,0), (.31,0), (-.28,0), (0,-.35), (.36,.28), (-.34,.4), (.18,-.3), (-.16,-.3)]
    rotation = cv2.Rodrigues(np.array([-.25, 1.2, .05]))[0]
    offset = np.array([-.16,.044,.025])
    samples = []
    for i, (x, y) in enumerate(angles):
        normal = cv2.Rodrigues(np.array([x,y,0.]))[0][:, 2]
        center = np.array([.09,-.04,1.6+i*.025])
        def plane(n, c):
            return dict(normal=n.tolist(), center_m=c.tolist(), offset_m=float(n@c))
        lidar = plane(rotation.T@normal, rotation.T@(center-offset))
        samples.append(dict(id=str(i), camera=plane(normal,center), lidars=dict(left=lidar,right=lidar)))
    return samples, rotation, offset


class BoardCalibration(unittest.TestCase):
    def test_fit_then_verify_on_independent_poses(self):
        samples, rot, offset = sample_planes()
        candidate = fit_extrinsics(samples[:6], PROFILE)
        result = verify_extrinsics(candidate, samples[6:], PROFILE)
        self.assertEqual(result["status"], "VERIFIED")
        np.testing.assert_allclose(result["transforms"]["left"]["translation_m"], offset, atol=1e-8)
        np.testing.assert_allclose(candidate["transforms"]["left"]["rotation_xyzw"][:3],
                                   cv2.Rodrigues(rot)[0].ravel() / np.linalg.norm([-.25,1.2,.05])
                                   * np.sin(np.linalg.norm([-.25,1.2,.05])/2), atol=1e-8)

    def test_reject_shared_or_inconsistent_validation(self):
        samples, _, _ = sample_planes()
        candidate = fit_extrinsics(samples[:6], PROFILE)
        reused = copy.deepcopy(samples[:2])
        reused[0]["id"] = "another-frame"
        with self.assertRaisesRegex(ValueError,"repeated"):
            verify_extrinsics(candidate, reused, PROFILE)
        candidate["transforms"]["right"]["translation_m"][2] += .08
        with self.assertRaisesRegex(ValueError,"residual"):
            verify_extrinsics(candidate, samples[6:], PROFILE)

    def test_no_stale_or_degenerate_samples(self):
        samples, _, _ = sample_planes()
        self.assertFalse(distinct_pose(samples[0], samples[:1]))
        samples[0]["lidars"].pop("right")
        with self.assertRaisesRegex(ValueError,"missing"):
            fit_extrinsics(samples, PROFILE)
        samples, _, _ = sample_planes()
        for i, item in enumerate(samples):
            item["camera"].update(normal=[0,0,1], offset_m=i*.15+1, center_m=[0,0,i*.15+1])
        with self.assertRaisesRegex(ValueError,"independent plane normals"):
            fit_extrinsics(samples, PROFILE)

    def test_checker_gates_trigger(self):
        K = np.array([[400.,0,200],[0,400,140],[0,0,1]])
        rgb = np.full((280,400,3), 160, dtype=np.uint8)
        self.assertIsNone(detect_board(rgb,K,PROFILE))
        rgb[45:235,70:330] = 240
        for i in range(7):
            for j in range(5):
                rgb[60+j*32:92+j*32,88+i*32:120+i*32] = 255*((i+j)%2)
        result = detect_board(rgb,K,PROFILE)
        self.assertEqual(result["corner_count"],24)
        self.assertAlmostEqual(result["offset_m"],1.,places=2)

    def test_reject_ambiguous_or_wrong_size_clouds(self):
        y,z = np.meshgrid(np.linspace(-.32,.32,34), np.linspace(-.24,.24,24))
        panel = np.c_[np.full(y.size,1.7),y.ravel(),z.ravel()]
        self.assertIsNotNone(observe_lidar(panel,PROFILE))
        self.assertIsNone(observe_lidar(panel[:18],PROFILE))
        self.assertIsNone(observe_lidar(panel*[1.,2.,2.],PROFILE))
        self.assertIsNone(observe_lidar(np.concatenate([panel,panel+[.28,0,0]]),PROFILE))

    def test_same_frame_two_targets_and_registry_removal(self):
        rgb = np.full((280,400,3), 245, dtype=np.uint8)
        for i in range(7):
            for j in range(5):
                rgb[60+j*32:92+j*32,88+i*32:120+i*32] = 255*((i+j)%2)
        rgb[25:39,30:52] = [195,20,180]
        meta=dict(stamp_ns=123,stamps_ns={"camera":123},frames={"camera":"optical", **PROFILE["lidar_frames"]})
        frame=dict(rgb=rgb,k=[400.,0,200.,0,400.,140.,0,0,1],clouds={}, **meta)
        archive = encode_frame(frame)
        src = Path(__file__).resolve().parents[1]/"calibration/station/recognizers"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name in ("campus-sign.json","checkerboard.json"):
                (directory/name).write_bytes((src/name).read_bytes())
            def call(target):
                return detect(dict(archive=archive,target=target),PROFILE,directory)
            board, sign = call("checkerboard"), call("campus_sign")
            self.assertEqual(board["archive_sha256"],sign["archive_sha256"])
            self.assertEqual(sign["detections"][0]["bbox_xyxy"],[30,25,52,39])
            self.assertIn("checkerboard_plane",board["detections"][0]["features"])
            self.assertNotIn("features",sign["detections"][0])
            with self.assertRaisesRegex(ValueError,"binding_mismatch"):
                measure(dict(archive=archive,detections=sign),PROFILE)
            (directory/"checkerboard.json").unlink()
            self.assertEqual(sign, call("campus_sign"))
            with self.assertRaisesRegex(ValueError,"unavailable_detector"):
                call("checkerboard")


if __name__ == "__main__":
    unittest.main()
