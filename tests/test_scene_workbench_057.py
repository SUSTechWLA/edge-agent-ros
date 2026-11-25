"""Independent consumer checks for the local archive site and unchanged planning HTTP endpoint."""
from contextlib import ExitStack, closing
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from edge_contracts.service_directory import ServiceSnapshot
from perception.scene_layers.replay_planning import PlanningFixture, ReplayPlanning, display_scene
from perception.scene_layers.runtime import ReplaySite
from perception.scene_layers.transport import SceneRuntimeError, call
from perception.scene_layers.workbench import ARCHIVE_ID, DEFAULT_ARCHIVE, Handler, Workbench
from scripts.export_field_scene_example import PROFILE, read_recorded_scan


class WorkbenchTests(unittest.TestCase):
    def test_optional_planning_backdrop_must_match_source_frame_and_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "example.json"
            snapshot = {"scan_stamp_ns": "400", "base_frame": "base_link"}
            source = "ab" * 32
            scene = {"frame_id": "base_link", "stamp_ns": "400"}
            self.assertIsNone(display_scene(path, "sha256:" + source, snapshot))
            for frame, stamp, archive_hash, matches in (("map", "400", source, False), ("base_link", "405", source, False),
                                                       ("base_link", "400", "00" * 32, False), ("base_link", "400", source, True)):
                with self.subTest(frame=frame, stamp=stamp, archive_hash=archive_hash):
                    path.write_text(json.dumps({"scene": {"frame_id": frame, "stamp_ns": stamp},
                                                "provenance": {"archive_sha256": archive_hash}}))
                    self.assertEqual(display_scene(path, "sha256:" + source, snapshot), scene if matches else None)

    def test_local_path_from_recorded_scan_without_any_map_providers(self):
        archive = ROOT / "artifacts/verification/lingao-footprint-20261123/local/footprint-clear.tar.gz"
        window = read_recorded_scan(archive, PROFILE)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            planner = PlanningFixture(stack, Path(tmp), window.snapshot, window.profile, {"source_clock": "simulation"})
            catalog = ServiceSnapshot(planner.directory)
            self.assertEqual(set(catalog.owners), {"planning.local_path"})
            with self.assertRaises(SceneRuntimeError) as error:
                call(planner.directory, "query", "freeze", kinds=["occupancy"], limits={})
            self.assertEqual(error.exception.code, "plugin.service_missing")

            receipt = planner.call()
            plan = receipt["plan"]
            self.assertEqual(receipt["binding"], catalog.pin("planning.local_path")["binding"])
            self.assertEqual(receipt["evaluation_seq"], 1)
            self.assertEqual(receipt["source_stamp_ns"], str(window.scan["stamp_ns"]))
            self.assertEqual(plan["source_id"], "gazebo.rover.scan")
            self.assertEqual(plan["calibration_ref"], window.snapshot["calibration_ref"])
            self.assertEqual(plan["base_frame"], "base_link")
            self.assertEqual(plan["status"], "planned")
            self.assertGreaterEqual(len(plan["path_body_m"]), 2)
            endpoint = plan["path_body_m"][-1]
            self.assertAlmostEqual(endpoint[0], window.goal[0], places=2)
            self.assertAlmostEqual(endpoint[1], window.goal[1], places=2)
            self.assertIsNone(plan["map_anchor"])
            self.assertIs(plan["motion_authorized"], False)

    def test_export_contains_frozen_rgb_geometry_and_obeys_unload(self):
        from test_scene_layers_057 import write_fixture

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            write_fixture(root)
            site = stack.enter_context(ReplaySite(root, root / "state"))
            web = Workbench(site, None)
            code, result = web.layer("texture")
            self.assertEqual(code, 200)
            self.assertEqual(result["descriptor"]["provider"]["plugin_version"], "1.1.0")
            server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
            server.workbench = web
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .025}, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)

            def download(scene_ref):
                url = f"/api/maps/{scene_ref[7:]}/{result['layer_ref'][7:]}.glb"
                with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=6)) as conn:
                    conn.request("GET", url)
                    response = conn.getresponse()
                    return response.status, response.getheader("Content-Type"), response.read()

            status, mime, raw = download(result["scene_ref"])
            self.assertEqual((status, mime), (200, "model/gltf-binary"))
            self.assertEqual(struct.unpack_from("<4sII", raw), (b"glTF", 2, len(raw)))
            length, tag = struct.unpack_from("<I4s", raw, 12)
            self.assertEqual(tag, b"JSON")
            model = json.loads(raw[20:20 + length])
            data = raw[28 + length:]
            self.assertEqual(struct.unpack_from("<I4s", raw, 20 + length), (len(data), b"BIN\0"))
            self.assertEqual(model["asset"]["extras"]["layer_ref"], result["layer_ref"])
            self.assertEqual(model["asset"]["extras"]["scene_ref"], result["scene_ref"])
            self.assertTrue(all(v["byteOffset"] % 4 == 0 for v in model["bufferViews"]))

            def view(i):
                v = model["bufferViews"][i]
                return data[v["byteOffset"]:v["byteOffset"] + v["byteLength"]]

            material = model["materials"][0]["pbrMetallicRoughness"]
            image = model["textures"][material["baseColorTexture"]["index"]]["source"]
            self.assertEqual(view(model["images"][image]["bufferView"]), (root / "rgbd-color.png").read_bytes())
            primitive = model["meshes"][0]["primitives"][0]
            def accessor(index, fmt):
                return list(struct.iter_unpack(fmt, view(model["accessors"][index]["bufferView"])))

            xyz = accessor(primitive["attributes"]["POSITION"], "<fff")
            uv = accessor(primitive["attributes"]["TEXCOORD_0"], "<ff")
            indices = accessor(primitive["indices"], "<I")
            surface = result["scene"]
            source_indices = [v for face in surface["triangles"] for v in face]
            self.assertEqual(len(indices), len(source_indices))
            for (actual,), source in zip(indices, source_indices):
                x, y, z = surface["vertices"][source][:3]
                expected = (x, -y, -z, *surface["texture_mapping"]["uv"][source])
                for got, value in zip(xyz[actual] + uv[actual], expected):
                    self.assertAlmostEqual(got, value, places=6)
            _, occupancy = web.layer("occupancy")
            self.assertEqual(download(occupancy["scene_ref"])[0], 409)
            site.disable("texture")
            self.assertEqual(json.loads(download(result["scene_ref"])[2])["code"], "plugin.service_missing")
            site.enable("texture")
            self.assertEqual(json.loads(download(result["scene_ref"])[2])["code"], "verification.binding_unavailable")

    @unittest.skipUnless((DEFAULT_ARCHIVE / "manifest.json").is_file(), "requires the Jetson recording")
    def test_live_unload_preserves_frozen_metadata_and_new_planning_calls(self):
        with ExitStack() as stack:
            state = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            site = stack.enter_context(ReplaySite(DEFAULT_ARCHIVE, state))
            server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
            server.workbench = Workbench(site, ReplayPlanning(stack, DEFAULT_ARCHIVE, state))
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .025}, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)

            def api(path, body=None):
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=6)
                try:
                    conn.request("POST" if body is not None else "GET", path,
                                 json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
                    answer = conn.getresponse()
                    return answer.status, json.loads(answer.read())
                finally:
                    conn.close()

            _, before = api("/api/state")
            frozen = {}
            for kind in ("occupancy", "texture"):
                code, row = api("/api/layers", {"archive": ARCHIVE_ID, "layer": kind})
                self.assertEqual(code, 200, row)
                frozen[kind] = {k: row[k] for k in ("scene_ref", "layer_ref")}
            self.assertNotEqual(frozen["occupancy"], frozen["texture"])
            code, refusal = api("/api/compose", {"archive": ARCHIVE_ID, "layers": ["occupancy", "texture"]})
            self.assertEqual(code, 422)
            self.assertIsNone(refusal["scene_ref"])
            self.assertEqual({r["code"] for r in refusal["reasons"]}, {"scene.missing_registration", "scene.time_skew"})
            self.assertAlmostEqual(refusal["time_span_ms"]/1000, 1217.4181, places=3)
            cases = {"field_hold": ("blocked", 0), "clear": ("planned", 2), "centered": ("partial", 123)}
            baseline = {}
            for case in cases:
                code, baseline[case] = api("/api/plan", {"case": case})
                self.assertEqual(code, 200, baseline[case])
            code, disabled = api("/api/texture", {"enabled": False})
            self.assertEqual(code, 200)
            self.assertFalse(disabled["texture_enabled"])
            pid = next(row["pid"] for row in before["plugins"] if row.get("kind") == "texture")
            self.assertEqual(disabled["change"]["pid"], pid)
            self.assertEqual(disabled["change"]["exit_code"], 0)
            self.assertTrue(disabled["change"]["remove"]["event_id"])
            record = frozen["texture"]
            self.assertEqual(api("/api/scenes/" + record["scene_ref"][7:])[0], 200)
            self.assertEqual(api("/api/layers", {"archive": ARCHIVE_ID, "layer": "texture", **record})[1]["code"],
                             "plugin.service_missing")
            self.assertEqual(api("/api/layers", {"archive": ARCHIVE_ID, "layer": "occupancy",
                                                **frozen["occupancy"]})[0], 200)
            for case, (status, count) in cases.items():
                code, current = api("/api/plan", {"case": case})
                previous = baseline[case]
                self.assertEqual(code, 200, current)
                self.assertEqual(current["plan"]["status"], status)
                self.assertEqual(len(current["plan"]["path_body_m"]), count)
                self.assertFalse(current["plan"]["motion_authorized"])
                for key in ("binding", "snapshot_ref", "geometry_ref", "source_stamp_ns"):
                    self.assertEqual(current[key], previous[key])
                self.assertNotEqual(current["request_id"], previous["request_id"])
                self.assertGreater(current["evaluation_seq"], previous["evaluation_seq"])
                self.assertEqual(current["plan"]["path_body_m"], previous["plan"]["path_body_m"])


if __name__ == "__main__":
    unittest.main()
