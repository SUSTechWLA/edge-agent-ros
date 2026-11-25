"""SDD-057 registered point colours, admission failures and portable PLY evidence."""
from copy import deepcopy
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from perception.scene_layers.core import SceneError, canonical, digest, freeze_scene, validated_descriptor
from perception.scene_layers.lidar_rgb import FusionArchive, encode_ply, fuse_clouds


def example():
    # Optical right/down/forward to map forward/left/up, with a displaced camera centre.
    # The first lidar point reaches optical [0.5,0,2] => pixel (4,2), not the image centre.
    camera_pose = [[0,0,1,1],[-1,0,0,.25],[0,-1,0,.5],[0,0,0,1]]
    laser_pose = [[1,0,0,1],[0,1,0,-.25],[0,0,1,.5],[0,0,0,1]]
    camera = {"frame_id": "optical", "width": 7, "height": 5, "k": [4,0,3,0,4,2,0,0,1],
              "T_map_camera": camera_pose}
    lidar = {"source_id": "a", "frame_id": "a_mount", "T_map_lidar": laser_pose}
    second = {**lidar, "source_id": "b", "frame_id": "b_mount"}
    calibration = {"source": "surveyed_fixture", "frame_id": "world", "clock_id": "fixture",
                   "camera": camera, "lidars": [lidar, second]}
    capture = {"schema_version": 1, "clock_id": "fixture", "frame_id": "world",
               "camera": {**deepcopy(camera), "stamp_ns": 100_000_000, "depth_stamp_ns": 100_000_000,
                          "depth_frame_id": "optical", "clock_id": "fixture", "rgb_file": "rgb.raw",
                          "depth_file": "depth.f32", "projection_model":"rectified_pinhole",
                          "depth_convention":"optical_z_m"},
               "lidars": [{**deepcopy(lidar), "clock_id": "fixture", "stamp_ns": 101_000_000,
                           "points": [[2,0,0], [3,0,0], [1,0,0], [-2,0,0], [2,5,0]]},
                          {**deepcopy(second), "clock_id": "fixture", "stamp_ns": 99_000_000,
                           "points": [[2,.5,0]]}]}
    rgb = bytes(v for i in range(35) for v in (i,20+i,255-i))
    return capture, calibration, rgb, [2.] * 35


def write_capture(root, capture, calibration, rgb, depth):
    files = {"calibration.json": canonical(calibration), "rgb.raw": rgb,
             "depth.f32": struct.pack(f"<{len(depth)}f", *depth)}
    for name, data in files.items():
        (root/name).write_bytes(data)
    raw = canonical({**capture, "files": {k: digest(v) for k, v in files.items()}})
    (root/"capture.json").write_bytes(raw)
    return digest(raw)


class LidarRGBTests(unittest.TestCase):
    def setUp(self):
        self.capture, self.rig, self.rgb, self.depth = example()

    def fuse(self, **kw):
        return fuse_clouds(self.capture, self.rig, self.rgb, self.depth, **kw)

    def assert_refusal(self, code, operation):
        with self.assertRaises(SceneError) as result:
            operation()
        self.assertEqual(code, result.exception.code)

    def test_geometry_projects_measured_rgb_and_preserves_unknown_and_lineage(self):
        self.capture["lidars"][0]["points"].append([float("nan"),0,1])
        fused = self.fuse()
        self.assertEqual(fused["points"][0], [3,-.25,.5])
        self.assertEqual(fused["contributors"], [["a",0],["a",1],["a",2],["a",3],["a",4],["b",0]])
        self.assertEqual(fused["colors"], [[18,38,237],None,None,None,None,[17,37,238]])
        self.assertEqual(fused["stats"]["sources"]["a"], {"input":6, "finite":5, "colored":1})
        self.assertEqual(fused["stats"]["rejections"], {"occluded":1, "behind_camera":1, "outside_fov":1,
                                                      "depth_disagreement":1, "invalid_xyz":1})
        self.assertEqual(fused["acquisition"], ["99000000","101000000"])
        self.assertFalse(fused["motion_authorized"])

    def test_holes_and_silhouette_neighbors_stay_uncolored(self):
        self.depth[18] = float("nan")
        data = self.fuse()
        self.assertIsNone(data["colors"][0])
        self.assertIsNone(data["colors"][-1])
        self.assertGreaterEqual(data["stats"]["rejections"]["unknown_depth"], 1)
        self.assertGreaterEqual(data["stats"]["rejections"]["depth_boundary"], 1)
        self.depth[18] = 3.
        self.assertIsNone(self.fuse()["colors"][-1])

    def test_all_acquisition_stamps_and_clock_domains_participate(self):
        self.assert_refusal("scene.time_skew", lambda: self.fuse(max_skew_ms=1))
        self.capture["camera"]["depth_stamp_ns"] = "160000001"
        self.assert_refusal("scene.time_skew", self.fuse)
        self.capture["camera"]["depth_clock_id"] = "different"
        self.assert_refusal("scene.clock_mismatch", self.fuse)
        self.capture["camera"]["depth_clock_id"] = "fixture"
        self.capture["lidars"][1]["clock_id"] = "sensor"
        self.assert_refusal("scene.clock_mismatch", self.fuse)

    def test_missing_wrong_or_nonrigid_calibration_is_refused(self):
        c = self.capture["camera"]
        c["T_map_camera"][0][3] += 1.
        self.assert_refusal("scene.missing_registration", self.fuse)
        c["T_map_camera"] = deepcopy(self.rig["camera"]["T_map_camera"])
        # Matching digests alone cannot make a reflected optical transform rigid.
        c["T_map_camera"][0][2] *= -1
        self.rig["camera"]["T_map_camera"] = deepcopy(c["T_map_camera"])
        self.assert_refusal("scene.invalid_calibration", self.fuse)
        del c["T_map_camera"]
        self.assert_refusal("scene.missing_registration", self.fuse)

    def test_radial_depth_raw_distortion_and_unregistered_camera_are_refused(self):
        for field, value in (("depth_convention","range"), ("depth_frame_id","unregistered_depth"),
                             ("projection_model","fisheye"), ("d",[.1,0,0,0,0])):
            with self.subTest(field=field):
                cam = deepcopy(self.capture["camera"])
                self.capture["camera"][field] = value
                self.assert_refusal("scene.invalid_calibration", self.fuse)
                self.capture["camera"] = cam

    def test_pinned_files_frozen_revision_and_ply_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cap_ref = write_capture(root, self.capture, self.rig, self.rgb, self.depth)
            a = FusionArchive(root, cap_ref)
            layer = a.describe("fusion")
            self.assertEqual(validated_descriptor(layer)["calibration_ref"], digest((root/"calibration.json").read_bytes()))
            self.assertEqual(freeze_scene([layer])["status"], "ready")
            fused = a.read("fusion", layer["layer_ref"])["data"]
            self.assertEqual(layer["content_ref"], digest(canonical(fused)))
            lines, payload = encode_ply(fused).split(b"end_header\n", 1)
            self.assertIn(b"format binary_little_endian 1.0", lines)
            self.assertIn(b"element vertex 6", lines)
            rows = list(struct.iter_unpack("<fffBBBBBI", payload))
            self.assertEqual(rows[0], (3,-.25,.5,18,38,237,1,0,0))
            self.assertEqual(rows[1][6:], (0,0,1))
            self.assertEqual(rows[-1][6:], (1,1,0))
            self.assert_refusal("scene.layer_mismatch", lambda: a.read("fusion", "stale"))
            self.assert_refusal("scene.manifest_mismatch", lambda: FusionArchive(root, digest(b"another capture")))
            (root/"rgb.raw").write_bytes(self.rgb[::-1])
            self.assert_refusal("scene.artifact_mismatch", lambda: FusionArchive(root, cap_ref))
            self.assertEqual(a.read("fusion", layer["layer_ref"])["data"], fused)

    def test_multistation_ply_separates_repeated_input_indices_and_rejects_bad_ranges(self):
        data = self.fuse()
        n = len(data["points"])
        for key in ("points","colors","contributors"):
            data[key] *= 2
        windows = [{"id":"west", "start":0, "count":n}, {"id":"east", "start":n, "count":n}]
        header, body = encode_ply({**data, "windows":windows}).split(b"end_header\n")
        self.assertIn(b'comment station_ids ["west", "east"]', header)
        self.assertIn(b"property uchar station\n", header)
        records = list(struct.iter_unpack("<fffBBBBBIB", body))
        self.assertEqual([r[-1] for r in records], [0]*n + [1]*n)
        self.assertEqual(records[0][:-1], records[n][:-1])
        for override in ({"start":n-1}, {"start":n+1}, {"count":n-1}, {"count":n+1},
                         {"count":-1}, {"start":True}, {"id":"west"}):
            with self.subTest(override=override):
                malformed = [windows[0], {**windows[1], **override}]
                self.assert_refusal("scene.invalid_station", lambda: encode_ply({**data, "windows":malformed}))


if __name__ == "__main__":
    unittest.main()
