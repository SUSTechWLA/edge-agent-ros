"""Actual loopback admission, DSH management, process removal and frozen scene reads."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from perception.scene_layers import core
from perception.scene_layers.runtime import ReplaySite, managed_change
from perception.scene_layers.transport import SERVICES, SceneRuntimeError, call, pin_service
from perception.scene_layers.worker import Handler, Query, Snapshots, WorkerServer


ARCHIVE = Path(__file__).resolve().parents[1]/"artifacts/verification/jetson-field-scene-20261123/sensor-evidence"


@contextmanager
def service(state, kind, export, backend):
    with WorkerServer(("127.0.0.1", 0), Handler) as server:
        server.directory = state/"runtime"
        server.endpoint = f"http://127.0.0.1:{server.server_port}/invoke"
        server.implementation_ref = core.digest(b"test provider source")
        server.kind, server.backend = kind, backend
        descriptor = {"id":export, "provides":[export], "version":"1.0.0", "schema_version":1,
                      "requires":[], "endpoint":server.endpoint}
        installed = managed_change(state, descriptor,"publish","absent")
        handler = threading.Thread(target=server.serve_forever, kwargs={"poll_interval":.01}, daemon=True)
        handler.start()
        try:
            yield descriptor, installed
        finally:
            server.shutdown()
            handler.join()


def direct(pin, operation, args):
    url = urlsplit(pin["endpoint"])
    connection = HTTPConnection(url.hostname, url.port, timeout=3)
    try:
        data = {"binding":pin["binding"], "operation":operation, "arguments":args}
        connection.request("POST", url.path, json.dumps(data), {"Content-Type":"application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class SceneRuntimeTests(unittest.TestCase):
    def test_partial_archive_startup_recovers_texture_and_persists_operator_intent(self):
        from test_scene_layers_057 import write_fixture
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive, state = root/"archive", root/"state"
            archive.mkdir()
            write_fixture(archive)
            color = archive/"rgbd-color.png"
            original = color.read_bytes()
            color.write_bytes(b"damaged picture")
            with ReplaySite(archive,state) as site:
                rows = {row["kind"]:row for row in site.catalog()["plugins"]}
                self.assertEqual(rows["texture"]["status"],"offline")
                self.assertEqual(rows["texture"]["code"],"scene.artifact_mismatch")
                self.assertEqual(site.freeze(["occupancy"])["status"],"ready")
                with self.assertRaises(SceneRuntimeError):
                    site.pin("texture")
                color.write_bytes(original)
                self.assertEqual(site.enable("texture")["state"],"ready")
                self.assertEqual(site.freeze(["texture"])["status"],"ready")
                site.disable("texture")
            with ReplaySite(archive,state) as restarted:
                self.assertEqual([r["kind"] for r in restarted.catalog()["plugins"]], ["occupancy","query"])
                self.assertEqual(restarted.freeze(["occupancy"])["status"],"ready")
                restarted.enable("texture")
            with ReplaySite(archive,state) as restored:
                self.assertEqual(restored.freeze(["texture"])["status"],"ready")

    def test_draining_rejects_new_and_stale_url_while_admitted_call_completes(self):
        entered, proceed = threading.Event(), threading.Event()

        class Layer:
            def describe(self, kind):
                entered.set()
                if not proceed.wait(3):
                    raise RuntimeError("test supervisor did not release this request")
                return {"kind":kind}

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            with service(state, "texture", SERVICES["texture"], Layer()) as (desc, installed):
                pin = pin_service(state/"runtime", "texture")
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(call, state/"runtime", "texture", "describe")
                    try:
                        self.assertTrue(entered.wait(2))
                        drain = managed_change(state, {**desc,"draining":True}, "publish", installed["version"])
                        self.assertEqual(direct(pin,"describe",{})[1]["error"]["code"], "plugin.draining")
                        with self.assertRaises(SceneRuntimeError) as error:
                            pin_service(state/"runtime","texture")
                        self.assertEqual(error.exception.code,"plugin.draining")
                    finally:
                        proceed.set()
                    self.assertEqual(pending.result(2)["provider"]["binding"], pin["binding"])
                managed_change(state, desc, "remove", drain["version"])
                self.assertEqual(direct(pin,"describe",{})[1]["error"]["code"],"plugin.service_missing")

    def test_third_party_geometry_is_resolved_by_station_projection(self):
        class NewLayer:
            def describe(self, kind):
                return core.make_layer({"schema_version":1,"kind":kind,"frame_id":"map","stamp_ns":"1",
                                        "clock_id":"offline","acquisition":["1","1"],
                                        "space_ref":core.digest(b"room"),"quality":{},
                                        "content_ref":core.digest(b"test")},
                                       core.digest(b"archive"))

            def read(self, kind, ref):
                stale = self.describe(kind)
                stale["quality"] = {"error_bound_m": .0001}
                return {"layer":stale, "data":{}}

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            (state/"scene-layer-services.json").write_text(json.dumps(
                {"schema_version":1,"layers":{"usersurface":"vendor.tabletop.mesh"}}),encoding="utf-8")
            with service(state,"usersurface","vendor.tabletop.mesh", NewLayer()) as (desc, activate):
                query = Query(state/"runtime", Snapshots(state/"scenes"))
                result = query.request("freeze", {"kinds":["usersurface"]})
                self.assertEqual(result["status"], "ready")
                layer = result["scene"]["layers"][0]
                self.assertEqual(layer["provider"]["service"], desc["id"])
                with self.assertRaises(core.SceneError) as wrong_quality:
                    query.request("read", {"scene_ref":result["scene_ref"], "kind":"usersurface",
                                           "layer_ref":layer["layer_ref"]})
                self.assertEqual(wrong_quality.exception.code,"scene.layer_mismatch")
                managed_change(state,desc,"remove",activate["version"])
                self.assertEqual(query.request("snapshot",{"scene_ref":result["scene_ref"]}), result["scene"])
                with self.assertRaises(SceneRuntimeError) as error:
                    query.request("freeze",{"kinds":["usersurface"]})
                self.assertEqual(error.exception.code,"plugin.service_missing")

    @unittest.skipUnless((ARCHIVE/"manifest.json").exists(), "Jetson field archive is optional")
    def test_real_bundle_query_then_unplug_and_reinstall(self):
        with tempfile.TemporaryDirectory() as folder, ReplaySite(ARCHIVE,folder) as site:
            with self.assertRaises(SceneRuntimeError) as busy:
                ReplaySite(ARCHIVE,folder)
            self.assertEqual(busy.exception.code,"scene.site_in_use")
            occ = site.pin("occupancy")
            texture = site.pin("texture")
            frozen = site.freeze(["texture"])
            ref = frozen["scene"]["layers"][0]["layer_ref"]
            self.assertEqual(len(site.read("texture",ref,scene_ref=frozen["scene_ref"])["data"]["triangles"]),3824)
            self.assertTrue(site.texture_png(ref).startswith(b"\x89PNG"))
            self.assertEqual(site.freeze(["occupancy","texture"])["status"],"refused")
            stopped = site.disable("texture")
            self.assertEqual(stopped["exit_code"],0)
            self.assertNotEqual(stopped["remove"]["event_id"],stopped["drain"]["event_id"])
            self.assertNotIn(texture["plugin_id"],{row["id"] for row in site.catalog()["plugins"]})
            self.assertEqual(occ,site.pin("occupancy"))
            self.assertEqual(site.freeze(["occupancy"])["status"],"ready")
            for read in (lambda: site.read("texture",ref), lambda: site.texture_png(ref),
                         lambda: site.read("texture",ref,scene_ref=frozen["scene_ref"])):
                with self.assertRaises(SceneRuntimeError) as unavailable:
                    read()
                self.assertEqual(unavailable.exception.code,"plugin.service_missing")
            self.assertEqual(site.snapshot(frozen["scene_ref"]),frozen["scene"])
            new = site.enable("texture")
            self.assertEqual(new["state"],"ready")
            self.assertEqual(site.describe("texture")["layer_ref"],ref)
            if new["binding"] != texture["binding"]:
                with self.assertRaises(SceneRuntimeError) as stale:
                    site.read("texture",ref,scene_ref=frozen["scene_ref"])
                self.assertEqual(stale.exception.code,"verification.binding_unavailable")


if __name__ == "__main__":
    unittest.main()
