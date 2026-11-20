"""Body-frame evidence, time gates and pinned calibration for spatial fusion."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_contracts.spatial_facts import SpatialFactError
from perception.spatial_facts.image_capture import depth_png
from perception.spatial_facts.stationary_fusion import fuse_stationary, source_windows


NOW = 1_795_000_000_000_000_197


def put(store, data):
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    ref = "sha256:" + hashlib.sha256(data).hexdigest()
    store[ref] = data
    return ref


def scene():
    batch = {"schema_version": 1, "actor_ref": "rover", "basis": "published", "clock_id": "unix",
             "as_of_ns": str(NOW - 1), "samples": []}
    profile = {"actor_ref": "rover", "body_frame": "base_foot", "witness_source": "wheel.odometry", "sources": {}}
    witness = {"actor_ref": "rover", "source_id": "wheel.odometry", "frame_id": "odom",
               "child_frame_id": "base_foot", "clock_id": "unix", "topic": "/wheel/odometry",
               "stationary": True, "first_ns": NOW - 505_000_000, "last_ns": NOW,
               "epoch": "current", "sample_count": 12, "max_gap_ns": 50_000_000,
               "max_linear_mps": 0., "max_angular_radps": .01, "max_displacement_m": 0., "max_rotation_rad": .005}
    files = {}
    for i, side in enumerate(("front", "rear")):
        sid, frame = "lidar." + side, side + "_frame"
        end = NOW - (10 + i) * 10**6
        sign = 1 if side == "front" else -1
        points = [[sign*1.12, sign*.13, .38, 52], [sign*9., 1., 1., 3], [sign*1.2, 0., -.1, 5]]
        row = {"schema_version": 1, "type": "livox.mid360.preview", "source_id": sid, "frame_id": frame,
               "first_receive_ns": str(end-150_000_000), "last_receive_ns": str(end),
               "first_device_stamp_ns": "101", "last_device_stamp_ns": "345", "device_time_type": 0,
               "stamp_basis": "host_packet_receive", "sampled_point_count": len(points), "points": points,
               "point_fields": ["x_m", "y_m", "z_m", "reflectivity"]}
        batch["samples"].append({"actor_ref": "rover", "kind": "artifact", "clock_id": "unix",
                                 "source_id": sid, "frame_id": frame, "stamp_ns": str(end),
                                 "artifact_ref": put(files, row), "calibration_ref": "livox.native", "media_type": "application/json"})
        profile["sources"][sid] = {"kind": "livox", "frame_id": frame, "extrinsic_ref": "robot.fixture",
                                   "translation_m": [0,.01,.04], "quaternion_xyzw": [0,0,i,1-i]}
    return batch, files, profile, witness


class FusionTests(unittest.TestCase):
    def setUp(self):
        self.scene = scene()

    def evaluate(self, data=None, now=NOW):
        return fuse_stationary(*(data or self.scene), now_ns=now)

    def test_body_cells_overlap_account_for_native_coordinates(self):
        batch, files, profile, _ = self.scene
        result = self.evaluate()
        self.assertEqual({"lidar.front": (NOW-160_000_000, NOW-10_000_000),
                          "lidar.rear": (NOW-161_000_000, NOW-11_000_000)}, source_windows(batch, files, profile))
        self.assertEqual((1,1,1,1), tuple(result[k] for k in
                         ("occupied_cells", "overlap_cells", "occupied_voxels", "overlap_voxels")))
        self.assertEqual([[35, 30, 2, 3]], result["grid"]["cells"])
        self.assertAlmostEqual(result["sector_nearest_m"]["front"], (1.12**2 + .14**2)**.5)
        self.assertIsNone(result["sector_nearest_m"]["rear"])
        self.assertEqual(result["sources"]["lidar.front"]["retained_points"], 1)
        self.assertEqual(result["witness"]["topic"], "/wheel/odometry")
        self.assertEqual(result["stamp_ns"], str(NOW-10_000_000))

    def test_grid_unknown_and_vertical_overlap_separate(self):
        self.scene[2]["sources"]["lidar.rear"]["translation_m"][2] = .94
        result = self.evaluate()
        self.assertEqual(result["overlap_cells"], 1)
        self.assertEqual(result["overlap_voxels"], 0)
        self.assertEqual(result["grid"]["unknown"], "unobserved")

    def test_refuses_temporal_and_provenance_faults(self):
        for mutate in (
            lambda b,f,p,w: b.update(actor_ref="other"),
            lambda b,f,p,w: b.update(basis="replay"),
            lambda b,f,p,w: b["samples"][0].update(clock_id="sim"),
            lambda b,f,p,w: b["samples"][0].update(frame_id="wrong"),
            lambda b,f,p,w: b["samples"].append(dict(b["samples"][0])),
            lambda b,f,p,w: f.update({b["samples"][0]["artifact_ref"]: b"edited"}),
            lambda b,f,p,w: p.update(max_skew_ns=0),
            lambda b,f,p,w: w.update(first_ns=NOW-152_000_000),
            lambda b,f,p,w: w.update(last_ns=NOW-20_000_000),
            lambda b,f,p,w: w.update(stationary=False),
            lambda b,f,p,w: w.update(max_angular_radps=.4),
            lambda b,f,p,w: w.update(max_gap_ns=250_000_000),
            lambda b,f,p,w: w.update(child_frame_id="tool"),
            lambda b,f,p,w: p["sources"]["lidar.rear"].update(quaternion_xyzw=[0,0,0,0]),
        ):
            case = deepcopy(self.scene)
            mutate(*case)
            with self.subTest(mutate=mutate), self.assertRaises(SpatialFactError):
                self.evaluate(case)
        for now in (NOW+3_000_000_000, NOW-500_000_000):
            with self.assertRaises(SpatialFactError):
                self.evaluate(now=now)

    def test_optional_depth_needs_pinned_scale_intrinsics_and_transform(self):
        batch, files, profile, _ = self.scene
        depth = depth_png(SimpleNamespace(width=2, height=2, step=4, encoding="16UC1", is_bigendian=1,
                                          data=struct.pack(">4H", 0, 500, 500, 0)))
        cal = {"type":"sensor_msgs/msg/CameraInfo", "frame_id":"optical", "width":2, "height":2,
               "k":[100.,0.,.5,0.,100.,.5,0.,0.,1.], "d":[0.]*5, "distortion_model":"plumb_bob",
               "binning_x":0, "binning_y":0, "roi":{"x_offset":0,"y_offset":0,"width":0,"height":0}}
        batch["samples"].append({"actor_ref":"rover", "kind":"artifact", "clock_id":"unix",
                                 "source_id":"depth", "frame_id":"optical", "stamp_ns":str(NOW-10_000_000),
                                 "artifact_ref":put(files,depth), "calibration_ref":put(files,cal), "media_type":"image/png"})
        cfg = profile["sources"]["depth"] = {"kind":"depth16", "frame_id":"optical", "extrinsic_ref":"fixture.tf",
                                            "quaternion_xyzw":[0,0,0,1], "translation_m":[0,0,0],
                                            "depth_scale_m":.001, "depth_scale_ref":"fixture.scale"}
        result = self.evaluate()
        self.assertEqual(result["sources"]["depth"]["retained_points"], 2)
        self.assertEqual(result["source_ids"], ["depth","lidar.front","lidar.rear"])
        cfg.pop("depth_scale_ref")
        with self.assertRaisesRegex(SpatialFactError, "unverified_depth_scale"):
            self.evaluate()
        cfg["depth_scale_ref"] = "fixture.scale"
        cal["d"] = [.1]
        batch["samples"][-1]["calibration_ref"] = put(files,cal)
        with self.assertRaisesRegex(SpatialFactError, "intrinsics_unsupported"):
            self.evaluate()


if __name__ == "__main__":
    unittest.main()
