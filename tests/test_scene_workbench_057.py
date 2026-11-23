"""Independent consumer checks for the local archive site and unchanged planning HTTP endpoint."""
from contextlib import ExitStack
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from perception.scene_layers.workbench import ARCHIVE_ID, DEFAULT_ARCHIVE, Handler, Workbench
from perception.scene_layers.replay_planning import ReplayPlanning
from perception.scene_layers.runtime import ReplaySite


class WorkbenchTests(unittest.TestCase):
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
