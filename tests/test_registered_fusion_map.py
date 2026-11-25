"""Consume the two Gazebo station captures without running a simulator or accessing devices."""
from contextlib import ExitStack, closing
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import math
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import threading
import unittest
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from perception.scene_layers.core import SceneError
from perception.scene_layers.fusion_map import RegisteredMap
from perception.scene_layers.runtime import ReplaySite
from perception.scene_layers.workbench import Handler, Workbench


EVIDENCE = ROOT/"artifacts/verification/rgb-lidar-fusion-20261124"
STATION_FILES = ("capture.json", "calibration.json", "world.sdf", "rig-pose.json", "rgb.raw", "depth.f32")


def ref(raw):
    return "sha256:" + sha256(raw).hexdigest()


def write(path, value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    path.write_bytes(raw)
    return ref(raw)


def rot_z(xyz, yaw, dx, dy, dz):
    x, y, z = xyz
    return (math.cos(yaw)*x - math.sin(yaw)*y + dx, math.sin(yaw)*x + math.cos(yaw)*y + dy, z + dz)


class RegisteredMapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manifest = json.loads((EVIDENCE/"map.json").read_bytes())
        for station in ("station-a", "station-b"):
            folder = self.root/station
            folder.mkdir()
            with tarfile.open(EVIDENCE/(station+".tar.gz")) as archive:
                for name in STATION_FILES:
                    member = archive.getmember(station+"/"+name)
                    self.assertTrue(member.isfile() and 0 < member.size < 2*1024*1024)
                    with archive.extractfile(member) as stream:
                        (folder/name).write_bytes(stream.read())

    def registered(self):
        return RegisteredMap(self.root, write(self.root/"map.json", self.manifest))

    def test_world_coordinates_and_independent_clock_epochs(self):
        archive = self.registered()
        layer = archive.describe("fusion")
        data = archive.read("fusion", layer["layer_ref"])["data"]
        windows = data["windows"]
        self.assertEqual(layer["frame_id"], "world")
        self.assertEqual(layer["space_ref"], self.manifest["static_world_ref"])
        self.assertEqual(layer["stamp_basis"], "map_checkpoint")
        self.assertEqual(layer["clock_id"], "unix")
        self.assertEqual(layer["acquisition"], [str(self.manifest["checkpoint_unix_ns"])]*2)
        self.assertEqual([w["id"] for w in windows], ["station-a", "station-b"])
        self.assertEqual([w["clock_epoch"] for w in windows], ["station-a", "station-b"])
        self.assertEqual({w["clock_id"] for w in windows}, {"gazebo_sim"})
        self.assertEqual(windows[0]["acquisition"], windows[1]["acquisition"])
        self.assertNotEqual(windows[0]["acquisition"], layer["acquisition"])
        self.assertEqual(windows[0]["start"], 0)
        self.assertEqual(windows[1]["start"], windows[0]["count"])
        self.assertEqual(sum(w["count"] for w in windows), len(data["points"]))
        self.assertEqual(sum(w["colored"] for w in windows), data["stats"]["colored"])
        self.assertFalse(data["motion_authorized"])
        # Use the lidar mounting angles and station placements of the physical
        # simulation world directly, independent of the provider's 4x4 matrices.
        mounts = {"lidar_left": (.18, 0, .31, .75), "lidar_right": (-.16, 0, -.29, .99)}
        placements = [(0, 0, 0, 0), (.1, .32, -.48, 0)]
        for win, base in zip(windows, placements):
            folder = self.root/win["id"]
            raw = (folder/"capture.json").read_bytes()
            self.assertEqual(win["capture_ref"], ref(raw))
            self.assertEqual(win["pose_ref"], ref((folder/"rig-pose.json").read_bytes()))
            by_source = {s["source_id"]: s["points"] for s in json.loads(raw)["lidars"]}
            for idx in range(win["start"], win["start"] + win["count"], 97):
                sensor, origin = data["contributors"][idx]
                world = rot_z(rot_z(by_source[sensor][origin], *mounts[sensor]), *base)
                self.assertLess(math.dist(world, data["points"][idx]), 1e-6)

    def test_rebound_pose_witness_rejects_a_false_origin(self):
        entry = self.manifest["stations"][1]
        path = self.root/entry["id"]/"rig-pose.json"
        pose = json.loads(path.read_bytes())
        pose["rig"]["position"]["x"] += .25
        entry["pose_ref"] = write(path, pose)
        with self.assertRaises(SceneError) as error:
            self.registered()
        self.assertEqual(error.exception.code, "scene.missing_registration")

    def test_another_static_layout_is_refused_even_when_file_hashes_are_rebound(self):
        entry = self.manifest["stations"][1]
        folder = self.root/entry["id"]
        world_file = folder/"world.sdf"
        tree = ElementTree.parse(world_file)
        tree.find("world/model[@name='red_wall']/pose").text = "5.8 1.7 1.25 0 0 0"
        tree.write(world_file, encoding="utf-8", xml_declaration=True)
        calibration = json.loads((folder/"calibration.json").read_bytes())
        calibration["world_file_sha256"] = sha256(world_file.read_bytes()).hexdigest()
        capture = json.loads((folder/"capture.json").read_bytes())
        capture["files"]["calibration.json"] = write(folder/"calibration.json", calibration)
        entry["capture_ref"] = write(folder/"capture.json", capture)
        with self.assertRaises(SceneError) as error:
            self.registered()
        self.assertEqual(error.exception.code, "scene.missing_registration")

    def test_missing_station_pose_prevents_publication(self):
        (self.root/"station-b"/"rig-pose.json").unlink()
        with self.assertRaises(SceneError) as error:
            self.registered()
        self.assertEqual(error.exception.code, "scene.artifact_missing")

    def test_dsh_http_ply_and_frozen_binding_after_unload(self):
        pinned = write(self.root/"map.json", self.manifest)
        with ExitStack() as stack:
            site = stack.enter_context(ReplaySite(None, self.root/"state", fusion_directory=self.root,
                                                  fusion_manifest_ref=pinned))
            server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
            server.workbench = Workbench(site, None)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval":.025}, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)

            def get(path):
                with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=12)) as conn:
                    conn.request("GET", path)
                    response = conn.getresponse()
                    return response.status, response.read()

            code, body = get("/api/fusion/map")
            self.assertEqual(code, 200)
            held = json.loads(body)
            scene_ref, layer_ref = held["scene_ref"], held["layer_ref"]
            url = f"/api/fusion/{scene_ref[7:]}/{layer_ref[7:]}.ply"
            metadata = "/api/scenes/"+scene_ref[7:]
            code, raw = get(url)
            self.assertEqual(code, 200)
            header, payload = raw.split(b"end_header\n", 1)
            self.assertIn(b'comment station_ids ["station-a", "station-b"]', header)
            data = held["scene"]
            record = struct.Struct("<fffBBBBBIB")
            self.assertEqual(len(payload), len(data["points"])*record.size)
            for station, window in enumerate(data["windows"]):
                for i in range(window["start"], window["start"] + window["count"], 149):
                    x,y,z,r,g,b,valid,sensor,origin,slot = record.unpack_from(payload, i*record.size)
                    self.assertLess(math.dist([x,y,z], data["points"][i]), 1e-5)
                    self.assertEqual([r,g,b] if valid else None, data["colors"][i])
                    self.assertEqual([["lidar_left","lidar_right"][sensor],origin], data["contributors"][i])
                    self.assertEqual(slot, station)
            site.disable("fusion")
            self.assertEqual(get(metadata)[0], 200)
            code, body = get(url)
            self.assertEqual((code, json.loads(body)["code"]), (409, "plugin.service_missing"))
            site.enable("fusion")
            code, body = get(url)
            self.assertEqual((code, json.loads(body)["code"]), (409, "verification.binding_unavailable"))
            code, body = get("/api/fusion/map")
            self.assertEqual(code, 200)
            current = json.loads(body)
            self.assertEqual(current["layer_ref"], layer_ref)
            self.assertNotEqual(current["scene_ref"], scene_ref)


if __name__ == "__main__":
    unittest.main()
