import json
from concurrent.futures import Future
from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import runtime_plugins
from perception.rover_preview.evaluator import EvaluationError, SERVICE, SnapshotEvaluator, VERSION
from perception.rover_preview.provider import Handler, Provider
from perception.rover_preview.status import StatusProvider


DIRECTORY = ROOT / "perception/rover_preview"
PROFILE = json.loads((DIRECTORY / "lingao.json").read_bytes())


def request(now, goal=None):
    aim = {"kind": "static_local_path", "frame_id": "base_link"}
    if goal is not None:
        aim["goal_body_m"] = goal
    snap = {"source_id": "laser", "base_frame": "base_link", "calibration_ref": "test",
            "scan_stamp_ns": str(now), "hits_body_m": [], "rays": [
                {"angle_rad": i*math.tau/180, "free_to_m": 6.0, "hit_m": None} for i in range(180)]}
    return {"schema_version": 1, "provider_version": VERSION, "clock_id": "unix",
            "objective": aim, "snapshot": snap}


class SnapshotServiceTests(unittest.TestCase):
    def test_worker_releases_slot_when_original_waiter_has_timed_out(self):
        for kind in (Provider, StatusProvider):
            with self.subTest(kind=kind.__name__), tempfile.TemporaryDirectory() as folder:
                state = Path(folder)
                provider = kind(state, PROFILE, "http://unused") if kind is Provider else kind(state, "http://unused")
                entered, release = threading.Event(), threading.Event()
                calls = []
                def observed(op):
                    calls.append(op)
                    if op == "old":
                        entered.set()
                        release.wait(3)
                    receipt = {"operation_id":op,"state":"SUCCEEDED","evidence":{"sequence":len(calls)}}
                    provider.update(op,"complete",receipt)
                    return receipt
                def client_timeout(future, timeout):
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(future.done())
                    raise TimeoutError()
                old = {"operation_id":"old","arguments":{},"version":provider.version}
                try:
                    with patch.object(provider, "run", side_effect=observed):
                        with patch.object(Future, "result", client_timeout):
                            self.assertEqual(provider.invoke(old)["state"], "RUNNING")
                        with provider.lock:
                            future = provider.active["old"]
                        release.set()
                        first = future.result(timeout=2)
                        # No new invoke, polling client or receipt read cleaned this up.
                        with provider.lock:
                            self.assertEqual(provider.active,{})
                        self.assertEqual(provider.invoke({**old,"operation_id":"next"})["state"], "SUCCEEDED")
                        self.assertEqual(provider.invoke(old), first)
                        self.assertEqual(calls, ["old","next"])
                finally:
                    release.set()
                    provider.pool.shutdown()

    def test_discovery_and_http_do_not_capture_or_persist_operations(self):
        with patch.dict(os.environ, {"EDGE_PLUGIN_DIR": str(DIRECTORY)}):
            binding = runtime_plugins.snapshot().pin(SERVICE)
        self.assertEqual(binding["plugin_version"], VERSION)
        self.assertEqual(binding["endpoint"], "http://rover-preview:8149/evaluate")
        self.assertEqual(json.loads((DIRECTORY / "tool.json").read_bytes())["version"], VERSION)
        with tempfile.TemporaryDirectory() as folder, patch("perception.rover_preview.provider.read_snapshot",
                                                            side_effect=AssertionError("upstream capture")):
            provider = Provider(Path(folder), PROFILE, "http://unused")
            now = 10**18
            provider.evaluator = SnapshotEvaluator(PROFILE, wall=lambda: now, monotonic=lambda: 0)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.provider = provider
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = "http://127.0.0.1:%d/evaluate" % server.server_port
            def post(body):
                return urlopen(Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"}), timeout=2)
            try:
                r = request(now, [2.4, .1])
                with post(r) as answer:
                    result = json.load(answer)
                self.assertEqual(result["service"], SERVICE)
                self.assertEqual(result["path_body_m"][-1], [2.4, .1])
                self.assertFalse(result["motion_authorized"])
                self.assertEqual(result["scan_stamp_ns"], r["snapshot"]["scan_stamp_ns"])
                with closing(sqlite3.connect(provider.path)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)
                r["objective"]["kind"] = "dynamic_local_path"
                with self.assertRaises(HTTPError) as rejected:
                    post(r)
                self.assertEqual(rejected.exception.code, 422)
                with rejected.exception as error:
                    self.assertEqual(json.load(error)["path_body_m"], [])
            finally:
                server.shutdown()
                server.server_close()
                provider.pool.shutdown()

    def test_new_input_never_inherits_a_cached_static_answer(self):
        now = 10**18
        engine = SnapshotEvaluator(PROFILE, wall=lambda: now, monotonic=lambda: 0)
        self.assertEqual(engine.evaluate(request(now))["status"], "planned")
        old = request(now-1)
        cases = [
            (old, "out_of_order"), (request(now-251_000_000), "stale"), (request(now+51_000_000), "stale"),
            ({**request(now), "provider_version": "1.0.0"}, "binding_mismatch"),
            ({**request(now), "profile_id": "other.chassis"}, "profile_mismatch"),
            ({**request(now), "clock_id": "ros"}, "unsupported"),
            ({**request(now), "objective": {"kind": "static_local_path", "frame_id": "map"}}, "frame_mismatch"),
            ({**request(now), "objective": {"kind": "explore", "frame_id": "base"}}, "unsupported"),
            ({**request(now), "objective": {"kind": "static_local_path", "frame_id": "base", "execute": True}}, "unsupported"),
            (request(now, [.1, 0., .5]), "invalid"), (request(now, [-1, 0]), "invalid"),
        ]
        for key in ("dynamic_obstacles", "occupancy_3d"):
            r = request(now)
            r["snapshot"][key] = []
            cases.append((r, "unsupported"))
        bad_frame = request(now)
        bad_frame["snapshot"]["base_frame"] = bad_frame["objective"]["frame_id"] = "map"
        cases.append((bad_frame, "frame_mismatch"))
        too_big = request(now)
        too_big["snapshot"]["rays"] *= 13
        cases.append((too_big, "input_budget"))
        for r, code in cases:
            with self.subTest(code=code), self.assertRaises(EvaluationError) as error:
                engine.evaluate(r)
            self.assertEqual(error.exception.code, "planning."+code)
            self.assertIsNone(error.exception.response()["planned_endpoint"])

    def test_busy_and_late_computations_release_capacity_without_old_path(self):
        now = 10**18
        counter = iter([0, 0, 60_000_000, 60_000_000, 65_000_000])
        entered, release = threading.Event(), threading.Event()
        def slow(snap, profile, **kwargs):
            entered.set()
            release.wait(2)
            return {"motion_authorized": False, "path_body_m": []}
        engine = SnapshotEvaluator(PROFILE, solve=slow, wall=lambda: now, monotonic=lambda: next(counter))
        errors = []
        def call():
            try:
                engine.evaluate(request(now))
            except EvaluationError as error:
                errors.append(error.code)
        thread = threading.Thread(target=call)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(EvaluationError) as busy:
                engine.evaluate(request(now))
            self.assertEqual(busy.exception.code, "planning.busy")
        finally:
            release.set()
            thread.join(2)
        self.assertEqual(errors, ["planning.deadline"])
        self.assertEqual(engine.evaluate(request(now))["elapsed_ms"], 5)
        clock = iter([now, now+260_000_000])
        stale = SnapshotEvaluator(PROFILE, solve=slow, wall=lambda: next(clock), monotonic=lambda: 0)
        with self.assertRaises(EvaluationError) as expired:
            stale.evaluate(request(now))
        self.assertEqual(expired.exception.code, "planning.stale")

    def test_prior_bound_tool_receipts_remain_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            original = {"operation_id":"old","state":"SUCCEEDED","evidence":{"path_body_m":[]}}
            with closing(sqlite3.connect(Path(folder) / "operations.sqlite")) as db, db:
                db.execute("CREATE TABLE operations (op TEXT PRIMARY KEY,phase TEXT NOT NULL,result TEXT)")
                db.execute("INSERT INTO operations VALUES ('old','complete',?)", (json.dumps(original),))
            provider = Provider(Path(folder), PROFILE, "http://unused")
            try:
                old = {"operation_id":"old","arguments":{},"version":"1.0.0"}
                self.assertEqual(provider.invoke(old), original)
                self.assertEqual(provider.invoke({**old, "version":VERSION})["error"]["code"], "planning.binding_mismatch")
                self.assertEqual(provider.invoke({**old, "operation_id":"new"})["error"]["code"], "planning.binding_mismatch")
                with self.assertRaises(ValueError):
                    provider.invoke({**old, "motion_resource":"chassis"})
                provider.active["occupied"] = Future()
                other = {"operation_id":"other","arguments":{},"version":VERSION}
                refused = provider.invoke(other)
                self.assertEqual(refused["error"]["code"], "planning.busy")
                provider.active.clear()
                self.assertEqual(provider.invoke(other), refused)
            finally:
                provider.pool.shutdown()


if __name__ == "__main__":
    unittest.main()
