"""Local SDD-006 service discovery, policy and independent verifier wire contracts."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import runtime_plugins as runtime


class RegistryContracts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.services = root / "runtime"
        self.services.mkdir()
        self.policy = root / "verification-policy.json"
        env = patch.dict(os.environ, {"EDGE_PLUGIN_DIR":str(root), "EDGE_CONTRACT_DIR":str(root)})
        env.start()
        self.addCleanup(env.stop)

    def install(self, key, provides, requires=(), **changes):
        descriptor = {"schema_version":1, "id":key, "version":"1.0",
                      "endpoint":"http://local:8100/verify", "provides":provides, "requires":list(requires), **changes}
        path = self.services / (key + ".json")
        staging = path.with_suffix(".staging")
        staging.write_text(json.dumps(descriptor), encoding="utf-8")
        staging.replace(path)
        return path

    def test_graph_pin_follows_transitive_dependencies_and_recovery(self):
        self.install("proof", ["verify.image"], ["capture.fs","clock"])
        self.install("clock_impl", ["clock"])
        self.install("storage", ["capture.fs"], ["disk"])
        self.install("digest", ["verify.hash"], ["capture.fs"])
        disk = self.install("disk_impl", ["disk"])
        first = runtime.snapshot()
        pin = first.pin("verify.image")
        self.assertEqual(pin["plugin_id"], "proof")
        self.assertEqual(pin["plugin_version"], "1.0")
        self.assertRegex(pin["binding"], "^[a-f0-9]{64}$")
        self.assertEqual(pin["dependencies"]["capture.fs"],first.pin("verify.hash")["dependencies"]["capture.fs"])
        self.assertNotIn("dependencies",pin["dependencies"]["capture.fs"])
        self.assertEqual(pin["dependencies"]["capture.fs"]["endpoint"],"http://local:8100/verify")
        self.install("proof", ["verify.image"], ["clock","capture.fs"])
        self.assertEqual(pin, runtime.snapshot().pin("verify.image"))
        self.install("disk_impl", ["disk"], endpoint="http://another-disk/verify")
        changed = runtime.snapshot()
        self.assertNotEqual(pin["binding"], changed.pin("verify.image")["binding"])
        self.assertNotEqual(first.pin("verify.hash")["binding"], changed.pin("verify.hash")["binding"])
        disk.unlink()
        now = runtime.snapshot()
        with self.assertRaises(runtime.PluginUnavailable):
            now.pin("verify.image")
        self.assertEqual(now.pin("clock"), first.pin("clock"))
        self.assertEqual(first.pin("verify.image"), pin)
        self.install("disk_impl", ["disk"])
        self.assertEqual(runtime.snapshot().pin("verify.image"), pin)

    def test_conflicts_cycles_and_missing_are_isolated(self):
        self.install("first", ["speed"])
        self.install("second", ["speed"])
        self.install("speed_view", ["view"], ["speed"])
        self.install("cycle_a", ["loop.a"], ["loop.b"])
        self.install("cycle_b", ["loop.b"], ["loop.a"])
        self.install("loop_view", ["cycle.consumer"], ["loop.a"])
        self.install("self", ["self"], ["self"])
        self.install("needs_camera", ["configure"], ["camera"])
        self.install("independent", ["working"])
        snap = runtime.snapshot()
        for service, code in {"speed":"plugin.service_conflict", "view":"plugin.service_conflict",
                              "loop.a":"plugin.dependency_cycle", "loop.b":"plugin.dependency_cycle",
                              "cycle.consumer":"plugin.dependency_cycle", "self":"plugin.dependency_cycle",
                              "configure":"plugin.service_missing", "missing":"plugin.service_missing"}.items():
            with self.subTest(service=service), self.assertRaises(runtime.PluginUnavailable) as exc:
                snap.pin(service)
            self.assertEqual(exc.exception.code, code)
        self.assertEqual(snap.pin("working")["plugin_id"], "independent")
        rows = snap.public()["plugins"]
        self.assertEqual(len(rows),9)
        self.assertEqual([r["id"] for r in rows if r["status"] == "ready"], ["independent"])

    def test_draining_preserves_bound_work_and_propagates_through_dependencies(self):
        self.install("disk", ["storage"])
        self.install("fs", ["capture.fs"], ["storage"])
        self.install("image", ["verify.image"], ["capture.fs"])
        self.install("clock", ["time"])
        services = {"disk":"storage", "fs":"capture.fs", "image":"verify.image"}
        original = runtime.snapshot()
        expected = {key:original.pin(service) for key, service in services.items()}
        disk = self.install("disk", ["storage"], draining=True)
        snap = runtime.snapshot()
        rows = {row["id"]:row for row in snap.public()["plugins"]}
        for key, service in services.items():
            self.assertEqual(rows[key]["status"], "draining")
            binding = expected[key]["binding"]
            self.assertEqual(rows[key]["binding"], binding)
            with self.assertRaises(runtime.PluginUnavailable) as exc:
                snap.pin(service)
            self.assertEqual(exc.exception.code, "plugin.draining")
            self.assertEqual(snap.pin(service, binding), expected[key])
            with self.assertRaises(runtime.PluginUnavailable) as mismatch:
                snap.pin(service, "old")
            self.assertEqual(mismatch.exception.code, "verification.binding_unavailable")
        self.assertEqual(rows["clock"]["status"], "ready")
        self.assertEqual(snap.pin("time"), original.pin("time"))
        self.install("disk", ["storage"], draining=False)
        self.assertEqual(runtime.snapshot().pin("verify.image"), expected["image"])
        self.install("disk", ["storage"], draining=True, version="2")
        with self.assertRaises(runtime.PluginUnavailable) as upgraded:
            runtime.snapshot().pin("verify.image", expected["image"]["binding"])
        self.assertEqual(upgraded.exception.code, "verification.binding_unavailable")
        disk.unlink()
        with self.assertRaises(runtime.PluginUnavailable):
            runtime.snapshot().pin("verify.image", expected["image"]["binding"])

    def test_bad_descriptors_and_credentials_do_not_enter_catalog(self):
        self.install("valid", ["good"])
        path = self.install("broken", ["bad"])
        good = json.loads(path.read_text())
        for change in ({"id":"other"}, {"version":True}, {"schema_version":1.0}, {"draining":1}, {"draining":"false"},
                       {"requires":[{}]}, {"provides":[]}, {"provides":["bad","bad"]},
                       {"endpoint":"http://login:secret@host/v"}, {"endpoint":"https://host/?key=secret"},
                       {"endpoint":"http://host:65536"}, {"endpoint":"file://etc/passwd"}, {"command":"shell"}):
            path.write_text(json.dumps({**good, **change}))
            with self.subTest(change=change):
                snap = runtime.snapshot()
                self.assertEqual(snap.pin("good")["plugin_id"], "valid")
                with self.assertRaises(runtime.PluginUnavailable):
                    snap.pin("bad")
                rows = snap.public()["plugins"]
                self.assertEqual(rows[0]["status"], "invalid")
                self.assertNotIn("secret", json.dumps(rows))
        for raw in (b"x"*(runtime.MAX_CONFIG_BYTES+1), b'{"id":1,"id":2}', b"["*1500+b"]"*1500):
            path.write_bytes(raw)
            self.assertEqual(runtime.snapshot().public()["plugins"][0]["status"], "invalid")

    def test_policy_is_additive_and_malformed_presence_is_explicit(self):
        cap = {"id":"capture","outcomes":{"image":"image_uri", "depth":"range_uri"}}
        self.assertEqual(runtime.requirements(cap,runtime.load_policy()), ["verify.image"])
        policy = {"schema_version":1,"tools":{"capture":["verify.sha256"]},
                  "outcomes":{"depth":["verify.depth","verify.sha256"], "image":[]}}
        self.policy.write_text(json.dumps(policy))
        self.assertEqual(runtime.requirements(cap,runtime.load_policy()), ["verify.depth","verify.image","verify.sha256"])
        self.assertEqual(runtime.requirements({"id":"status"},runtime.load_policy()),[])
        for value in ({}, {"schema_version":True}, {**policy,"tools":{"any":"oops"}}, {**policy,"unexpected":1},
                      {**policy,"outcomes":{"depth":["..."]}}, {**policy,"tools":{"*": ["verify.sha256"]}}):
            self.policy.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(runtime.PolicyError):
                runtime.load_policy()
        self.policy.write_text('{"schema_version":1,"tools":{},"tools":{},"outcomes":{}}')
        with self.assertRaises(runtime.PolicyError):
            runtime.load_policy()


class VerificationProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                receipt = {k:body[k] for k in ("operation_id","service","plugin_id","plugin_version")}
                receipt.update(verdict="pass", evidence={"pixels":35, "api_key":"must_be_redacted"})
                case = body["arguments"]["case"]
                status = 200
                if case == "shared":
                    receipt["evidence"] = {"dependency":body["dependencies"]["capture.fs"]}
                if case == "deep":
                    for _ in range(27):
                        receipt["evidence"] = {"nested":receipt["evidence"]}
                if case in ("unknown","fail"):
                    receipt["verdict"] = case
                if case in ("operation_id","service","plugin_id","plugin_version","verdict"):
                    receipt[case] = "wrong"
                if case == "empty":
                    receipt["evidence"] = {}
                if case == "extra":
                    receipt["extra"] = 1
                if case == "slow":
                    time.sleep(.4)
                if case == "unavailable":
                    status = 503
                raw = json.dumps(receipt).encode()
                if case == "oversize":
                    raw += b" "*runtime.MAX_REPLY_BYTES
                if case == "nan":
                    raw = raw.replace(b'"pixels": 35', b'"pixels": NaN')
                if case == "duplicate":
                    raw = raw[:-1] + b',"verdict":"pass"}'
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)+(1 if case == "short" else 0)))
                self.end_headers()
                try:
                    if case == "trickle":
                        for chunk in raw:
                            self.wfile.write(bytes([chunk]))
                            time.sleep(.02)
                    else:
                        self.wfile.write(raw)
                except OSError:
                    pass

        cls.server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        dependency = {"service":"capture.fs","plugin_id":"local-storage","plugin_version":"1",
                      "endpoint":"http://storage/read","binding":"pinned-binding"}
        cls.pin = {"plugin_id":"checker","plugin_version":"2","service":"verify.image","binding":"digest",
                   "dependencies":{"capture.fs":dependency},
                   "endpoint":f"http://127.0.0.1:{cls.server.server_port}/verify"}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def check(self, case, trace=None):
        request = {"operation_id":"demo/step/1", "task_id":"demo", "trace_id":"trace-1", "tool":"camera",
                   "arguments":{"case":case},"outcomes":{"image":"image_uri"},"evidence":{"image_uri":"fixture"},
                   "started_at":1, "plugin_id":"untrusted_id", "dependencies":{"capture.fs":"untrusted"}}
        return runtime.verify(self.pin, request, trace)

    def test_pass_fail_replay_and_auditable_identity(self):
        trace = []
        first = self.check("pass", lambda kind, data: trace.append((kind, data)))
        self.assertEqual(first, self.check("pass"))
        self.assertEqual(first["evidence"], {"pixels":35,"api_key":"[REDACTED]"})
        self.assertEqual(first["plugin_id"], "checker")
        self.assertEqual(first["operation_id"], "demo/step/1")
        self.assertEqual(self.check("fail")["verdict"], "fail")
        self.assertEqual(self.check("shared")["evidence"]["dependency"], self.pin["dependencies"]["capture.fs"])
        self.assertEqual([k for k,_ in trace], ["verification.request","verification.response"])
        self.assertEqual(trace[0][1]["call_id"],trace[1][1]["call_id"])
        self.assertNotIn("must_be_redacted", json.dumps(trace))

    def test_malformed_receipts_and_unknown_are_not_success(self):
        for case in ("operation_id","service","plugin_id","plugin_version","verdict",
                     "empty","extra","oversize","deep","nan","duplicate","short","unavailable","unknown"):
            with self.subTest(case=case), self.assertRaises(runtime.PluginUnavailable) as exc:
                self.check(case)
            if case == "unknown":
                self.assertEqual(exc.exception.code, "plugin.verdict_unknown")
                self.assertEqual(exc.exception.details["pixels"], 35)
            elif case == "unavailable":
                self.assertEqual(exc.exception.code, "plugin.transport")
            else:
                self.assertEqual(exc.exception.code, "plugin.receipt_invalid")

    def test_timeout_is_retryable(self):
        for case in ("slow","trickle"):
            start = time.monotonic()
            with self.subTest(case=case), patch.object(runtime,"VERIFY_TIMEOUT",.1), self.assertRaises(runtime.PluginUnavailable) as exc:
                self.check(case)
            self.assertEqual(exc.exception.code,"plugin.transport")
            self.assertLess(time.monotonic()-start, .75)


if __name__ == "__main__":
    unittest.main()
