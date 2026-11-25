"""SDD-057 stage 1: immutable source layers, query admission and a portable Nav2 map."""
from copy import deepcopy
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from edge_contracts.image_frames import chunk
from edge_contracts.occupancy import save_map
from perception.rover_plan_field.rgb_scene import build_rgbd
from perception.rover_plan_field.scene import build_scene
from perception.scene_layers.core import Archive, GridArchive, SceneError, canonical, check_read, digest, freeze_scene, make_layer


SITE_ARCHIVE = ROOT/"artifacts/verification/jetson-field-scene-20261123/sensor-evidence"


def small_png(depth, width=9, height=9, pixel_at=None):
    default = struct.pack(">H", 620) if depth else bytes((70, 80, 90))
    rows = [b"\0" + b"".join(pixel_at(x, y) if pixel_at else default for x in range(width))
            for y in range(height)]
    header = struct.pack(">IIBBBBB", width, height, 16 if depth else 8, 0 if depth else 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def write_fixture(root, *, width=9, height=9, stride=8, colour=None, depth=None):
    # A small geometrically reproducible archive is always available in CI.
    files = {}
    def put(name, record):
        raw = record if isinstance(record, bytes) else canonical(record)
        files[name] = digest(raw)
        (root/name).write_bytes(raw)
        return files[name]
    stamp = "900000000000"
    calibration_ref = digest(b"test lidar mount")
    calibration = {"sources": {}, "model_evidence": {"digest": calibration_ref}}
    profile = {"front_m": .25, "rear_m": .25, "left_m": .2, "right_m": .2}
    sources, clouds = [], {}
    for k, y in (("front", .2), ("rear", -.2)):
        source_id = "mid360." + k
        cloud = {"source_id": source_id, "frame_id": k, "type": "livox.mid360.preview",
                 "last_receive_ns": int(stamp), "points": [[1., 0., .2, 5], [1., y, .35, 5]]}
        cloud_ref = put(source_id + ".json", cloud)
        clouds[cloud_ref] = canonical(cloud)
        sources.append({"source_id": source_id, "frame_id": k, "stamp_ns": stamp,
                        "stamp_basis": "host_packet_receive", "sampled_points": 2, "artifact_ref": cloud_ref})
        calibration["sources"][source_id] = {"frame_id": k, "extrinsic_ref": calibration_ref,
                                             "translation_m": [0., y, .1], "quaternion_xyzw": [0., 0., 0., 1.]}
    put("lidar-calibration.json", calibration)
    put("chassis-profile.json", profile)
    snapshot = {"source_id": "dual", "base_frame": "base_foot", "scan_stamp_ns": stamp,
                "calibration_ref": calibration_ref, "source_evidence": {"lidars": sources}}
    snap_ref = put("lidar-snapshot.json", snapshot)
    put("occupancy-scene.json", build_scene(snapshot, calibration, clouds.__getitem__,
                                             footprint=profile, snapshot_ref=snap_ref))

    stamp_rgb = str(int(stamp) + 9_000_000_000)
    optical = "camera_color_optical_frame"
    intrinsics = {"type": "sensor_msgs/msg/CameraInfo", "frame_id": optical, "width": width, "height": height,
                  "distortion_model": "plumb_bob", "d": [0]*5,
                  "k": [100, 0, (width-1)/2, 0, 100, (height-1)/2, 0, 0, 1]}
    if colour is None:
        colour = small_png(False, width, height)
    if depth is None:
        depth = small_png(True, width, height)
    refs = {"color": put("rgbd-color.png", colour), "depth": put("rgbd-depth.png", depth),
            "calibration": put("rgbd-calibration.json", intrinsics)}
    mesh = build_rgbd(colour, depth, intrinsics, optical, stride=stride)
    mesh.update(stamp_ns=stamp_rgb, skew_ms=0, texture_url="/camera/" + refs["color"][7:] + ".png")
    mesh["source_evidence"] = {key: {"frame_id": optical, "stamp_ns": stamp_rgb, "artifact_ref": ref}
                               for key, ref in refs.items()}
    mesh["source_evidence"].update(actor_ref="test-rover", clock_id="unix")
    mesh_ref = put("rgbd-scene.json", mesh)
    manifest = {"schema_version": 1, "lidar_snapshot_ref": snap_ref, "rgbd_scene_ref": mesh_ref,
                "lidar_stamp_ns": stamp, "rgbd_stamp_ns": stamp_rgb, "relative_pose_between_scenes": None,
                "files": files}
    (root/"manifest.json").write_bytes(canonical(manifest))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        write_fixture(self.root)
        self.archive = Archive(self.root)

    def test_independent_layers_and_freeze_membership(self):
        occ, tex = (self.archive.describe(k) for k in ("occupancy", "texture"))
        self.assertEqual(occ["quality"]["resolution_m"], .1)
        self.assertEqual(tex["quality"]["stride_px"], 8)
        self.assertIsNone(tex["quality"]["resolution_m"])
        self.assertEqual(len(self.archive.read("texture", tex["layer_ref"])["data"]["triangles"]), 2)
        self.assertEqual(digest(self.archive.texture_png(tex["layer_ref"])), tex["source_refs"]["color"])
        both = freeze_scene([tex, occ])
        self.assertEqual({r["code"] for r in both["reasons"]}, {"scene.missing_registration", "scene.time_skew"})
        self.assertIsNone(both["scene_ref"])
        self.assertFalse(both["motion_authorized"])
        with self.assertRaises(SceneError):
            freeze_scene([occ, occ])
        ready = freeze_scene([occ])
        self.assertEqual(ready["scene_ref"], freeze_scene([occ])["scene_ref"])
        check_read(ready["scene_ref"], ready["scene"], "occupancy", occ["layer_ref"])
        with self.assertRaises(SceneError):
            check_read(ready["scene_ref"], ready["scene"], "texture", tex["layer_ref"])
        modified = deepcopy(ready["scene"])
        modified["frame_id"] = "world"
        with self.assertRaises(SceneError):
            check_read(ready["scene_ref"], modified, "occupancy", occ["layer_ref"])
        with self.assertRaises(SceneError):
            self.archive.read("texture", occ["layer_ref"])

    def test_texture_uv_matches_source_pixels_and_keeps_unobserved_tiles_open(self):
        # An asymmetric image and a right margin shorter than one stride detect flips and stretching.
        width, height, stride = 11, 9, 4
        def pixel(x, y):
            return bytes((x * 11, y * 17, x + y))
        def depth(x, y):
            return struct.pack(">H", {(0, 0): 0, (6, 2): 1800}.get((x, y), 620))
        colour = small_png(False, width, height, pixel)
        write_fixture(self.root, width=width, height=height, stride=stride, colour=colour,
                      depth=small_png(True, width, height, depth))
        archive = Archive(self.root)
        desc = archive.describe("texture")
        mesh = archive.read("texture", desc["layer_ref"])["data"]
        mapping = mesh["texture_mapping"]
        self.assertEqual((mapping["schema_version"], mapping["origin"], mapping["coordinates"]),
                         (1, "top_left", "normalized_pixel_centers"))
        self.assertEqual(mapping["image_ref"], digest(archive.texture_png(desc["layer_ref"])))
        self.assertEqual(desc["content_ref"], digest(canonical(mesh)))
        self.assertEqual(desc["source_refs"]["surface"], digest((self.root / "rgbd-scene.json").read_bytes()))
        self.assertEqual(len(mapping["uv"]), len(mesh["vertices"]))
        self.assertIsNone(mapping["uv"][0])
        for index, (coord, vertex) in enumerate(zip(mapping["uv"], mesh["vertices"])):
            if vertex is None:
                continue
            row, col = divmod(index, 3)
            x, y = col * stride, row * stride
            self.assertAlmostEqual(coord[0] * width, x + .5)
            self.assertAlmostEqual(coord[1] * height, y + .5)
            self.assertEqual(vertex[3:], list(pixel(x, y)))
        # A missing corner removes the upper left tile. The depth jump lies between the
        # four valid samples of the upper right tile. Only the two lower tiles can be drawn.
        self.assertEqual(mesh["triangles"], [[3, 6, 4], [4, 6, 7], [4, 7, 5], [5, 7, 8]])

    def test_colour_detail_between_vertices_is_bound_to_the_texture_revision(self):
        before = self.archive.describe("texture")
        previous = self.archive.read("texture", before["layer_ref"])["data"]
        colour = small_png(False, pixel_at=lambda x, y: bytes((255, 0, 10) if (x, y) == (4, 4) else (70, 80, 90)))
        write_fixture(self.root, colour=colour)
        archive = Archive(self.root)
        after = archive.describe("texture")
        current = archive.read("texture", after["layer_ref"])["data"]
        for key in ("vertices", "triangles"):
            self.assertEqual(previous[key], current[key])
        self.assertEqual(previous["texture_mapping"]["uv"], current["texture_mapping"]["uv"])
        self.assertEqual(current["texture_mapping"]["image_ref"], digest(colour))
        self.assertNotEqual(before["content_ref"], after["content_ref"])
        self.assertNotEqual(before["layer_ref"], after["layer_ref"])
        self.assertEqual(archive.texture_png(after["layer_ref"]), colour)
        with self.assertRaises(SceneError):
            archive.texture_png(before["layer_ref"])

    def test_independent_of_unrelated_bundle_files_but_provider_binding_is_frozen(self):
        occ = self.archive.describe("occupancy")
        # Replacing an unrelated resource can change the archive identity, yet cannot
        # invalidate the frozen geometry. The immutable required sources still match.
        path = self.root/"manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["files"]["other.txt"] = digest(b"unrelated")
        path.write_bytes(canonical(manifest))
        (self.root/"rgbd-color.png").unlink()
        another = Archive(self.root)
        second = another.describe("occupancy")
        self.assertNotEqual(occ["archive_ref"], second["archive_ref"])
        self.assertEqual(freeze_scene([occ])["scene_ref"], freeze_scene([second])["scene_ref"])
        with self.assertRaises(SceneError):
            Archive(self.root, manifest_ref=self.archive.archive_ref)
        with self.assertRaises(SceneError):
            another.describe("texture")
        first = freeze_scene([{**occ, "provider": {"binding": "A"}}])
        new = freeze_scene([{**occ, "provider": {"binding": "B"}}])
        self.assertNotEqual(first["scene_ref"], new["scene_ref"])
        self.assertEqual(first["scene"]["layers"][0]["provider"]["binding"], "A")

    def test_hash_failures_and_false_derived_geometry(self):
        (self.root/"mid360.rear.json").write_bytes(b"{}")
        with self.assertRaises(SceneError) as corrupt:
            self.archive.describe("occupancy")
        self.assertEqual(corrupt.exception.code, "scene.artifact_mismatch")
        self.assertEqual(self.archive.describe("texture")["frame_id"], "camera_color_optical_frame")
        # Even rewriting the mesh digest in a new manifest does not bypass raw RGB-D reproduction.
        path = self.root/"rgbd-scene.json"
        mesh = json.loads(path.read_bytes())
        mesh["triangles"].pop()
        path.write_bytes(canonical(mesh))
        path = self.root/"manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["files"]["rgbd-scene.json"] = manifest["rgbd_scene_ref"] = digest(canonical(mesh))
        path.write_bytes(canonical(manifest))
        with self.assertRaises(SceneError) as mismatch:
            Archive(self.root).describe("texture")
        self.assertEqual(mismatch.exception.code, "scene.derived_mismatch")

    def test_two_already_aligned_representations_share_a_snapshot(self):
        # A second trusted producer can expose the measured ground plane of the same observation.
        occ = self.archive.describe("occupancy")
        scene = self.archive.read("occupancy", occ["layer_ref"])["data"]
        cells = {key: scene[key] for key in ("observed_ground", "occupied_ground")}
        meta = {k: v for k, v in occ.items() if k not in ("layer_ref", "archive_ref")}
        meta.update(kind="ground_plane", representation="observed_ground_cells",
                    content_ref=digest(canonical(cells)), source_refs={"parent": occ["layer_ref"]})
        plane = make_layer(meta, occ["archive_ref"])
        result = freeze_scene([plane, occ], frame_id="base_foot")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["scene_ref"], freeze_scene([occ, plane])["scene_ref"])
        shifted = make_layer({**meta, "clock_id": "other"}, occ["archive_ref"])
        self.assertEqual([r["code"] for r in freeze_scene([occ, shifted])["reasons"]], ["scene.clock_mismatch"])
        same_frame_elsewhere = make_layer({**meta, "space_ref": digest(b"other capture")}, occ["archive_ref"])
        self.assertEqual([r["code"] for r in freeze_scene([occ, same_frame_elsewhere])["reasons"]],
                         ["scene.missing_registration"])

    def test_precise_or_unseen_region_refused(self):
        occ = self.archive.describe("occupancy")
        outside = {"x": [123., 125.], "y": [0, 1], "z": [0, 1]}
        denied = freeze_scene([occ], max_resolution_m=.01, max_error_m=.005, roi_m=outside)
        self.assertEqual({r["code"] for r in denied["reasons"]},
                         {"scene.resolution_unavailable", "scene.accuracy_unverified", "scene.outside_coverage"})
        self.assertEqual(freeze_scene([occ], frame_id="elsewhere")["reasons"][0]["code"],
                         "scene.missing_registration")
        with self.assertRaises(SceneError):
            freeze_scene([occ], max_skew_ms=float("nan"))
        with self.assertRaises(SceneError):
            freeze_scene([{**occ, "stamp_ns": "1"}])

    @unittest.skipUnless((SITE_ARCHIVE/"manifest.json").is_file(), "Jetson archive is local evidence")
    def test_jetson_manifest_retains_real_capture_gap_and_triangle_source(self):
        archive = Archive(SITE_ARCHIVE)
        occ, tex = (archive.describe(k) for k in ("occupancy", "texture"))
        dual = freeze_scene([occ, tex])
        self.assertEqual((occ["stats"]["voxel_count"], tex["stats"]["triangles"]), (330, 3824))
        self.assertAlmostEqual(dual["time_span_ms"], 1_217_418.100749)
        narrow = freeze_scene([occ], max_skew_ms=50)
        self.assertEqual(narrow["reasons"][0]["code"], "scene.time_skew")
        self.assertAlmostEqual(narrow["acquisition_span_ms"], 121.991154)
        self.assertIsNone(dual["scene_ref"])


class GridArchiveTests(unittest.TestCase):
    def test_original_nav2_artifacts_before_and_after_tamper(self):
        grid = {"frame": "map", "origin": [0, 0, 0], "width": 2, "height": 2,
                "resolution": .05, "cells": [0, -1, 100, 0]}
        meta = {"robot_id": "fixture", "calibration_id": "lab-1", "operation_id": "historical-map",
                "state": "RECORDED", "reason": "test", "stamp_ns": 123,
                "map_identity": {"clock_domain": "lab_time"}}
        with tempfile.TemporaryDirectory() as folder:
            site = Path(folder)/"saved-map"
            manifest = save_map(site, grid, metadata=meta)
            original = digest((site/"manifest.json").read_bytes())
            stored = GridArchive(site, original)
            layer = stored.describe("grid2d")
            self.assertEqual(layer["source_refs"]["map_manifest"], original)
            self.assertEqual(layer["content_ref"], "sha256:" + manifest["sha256"]["occupancy.json"])
            self.assertEqual((layer["stats"]["known_cells"], layer["clock_id"]), (3, "lab_time"))
            self.assertEqual(stored.read("grid2d", layer["layer_ref"])["data"]["cells"], grid["cells"])
            self.assertEqual(freeze_scene([layer])["status"], "ready")
            (site/"map.pgm").write_bytes(b"corrupted")
            with self.assertRaises(ValueError):
                GridArchive(site, original)


if __name__ == "__main__":
    unittest.main()
