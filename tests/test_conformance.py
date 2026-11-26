"""Real HTTP and process boundaries for the independent read-only provider probe."""
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import unittest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples/timestamp-audit"
Handler = runpy.run_path(str(SAMPLE / "provider.py"))["Handler"]


class Sample(Handler):
    def log_message(self, *args):
        pass


@contextmanager
def server(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as http:
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{http.server_port}"
        finally:
            http.shutdown()
            thread.join()


class ContractTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cap = json.loads((SAMPLE / "tool.json").read_bytes())
        self.cases = json.loads((SAMPLE / "cases.json").read_bytes())

    def probe(self, url, *, manifest=None, cases=None, process=None, legacy=False, success=True):
        cap_path, case_path = self.tmp / "tool.json", self.tmp / "cases.json"
        cap_path.write_text(manifest if isinstance(manifest,str) else json.dumps(self.cap if manifest is None else manifest))
        case_path.write_text(json.dumps(self.cases if cases is None else cases))
        entry = [str(ROOT / "tests/system/provider_contract.py")] if legacy else ["-m", "edge_contracts.conformance"]
        args = [sys.executable, *entry, "--manifest", str(cap_path), "--url", url, "--cases", str(case_path)]
        if legacy:
            args += ["--out", str(self.tmp / "report.json")]
        if process is not None:
            args += ["--process", *process]
        result = subprocess.run(args, cwd=ROOT, timeout=40, capture_output=True)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0 if success else 1, report)
        self.assertIs(report["passed"], success)
        return report

    def test_three_arguments_remote_tool_wire_and_unmanaged_lifecycle(self):
        with server(Sample) as base:
            report = self.probe(base + "/invoke")
        self.assertEqual(len(self.cases), len(report["cases"]))
        self.assertEqual(report["checks"]["lifecycle"]["status"], "not_run")
        self.assertFalse(report["all_checks_passed"])
        self.assertEqual((report["status"], report["complete"]), ("partial",False))
        for i, case in enumerate(report["cases"]):
            self.assertEqual(self.cases[i], case["fixture"])
            a, b = case["attempts"]
            self.assertEqual(a, b)
            self.assertEqual(a["receipt"]["operation_id"], case["operation_id"])
        checks = report["checks"]
        self.assertEqual(checks["version"]["attempts"][0]["receipt"]["error"]["code"], "timestamp.version_mismatch")
        self.assertEqual(checks["invalid_arguments"]["attempts"][0]["receipt"]["error"]["code"], "timestamp.invalid_series")
        fault = checks["timeout"]
        self.assertEqual(fault["client_result"], "timeout")
        self.assertEqual(fault["upstream"], fault["recovery"])
        self.assertEqual(fault["operation_id"], fault["recovery"][0]["receipt"]["operation_id"])

    def test_forced_exit_and_restarted_provider_reconcile_the_same_operation(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = [sys.executable, str(SAMPLE / "provider.py"), "--bind", "127.0.0.1", "--port", str(port)]
        report = self.probe(f"http://127.0.0.1:{port}/invoke", process=process)
        self.assertTrue(report["all_checks_passed"])
        self.assertEqual((report["status"], report["complete"]), ("passed",True))
        life = report["checks"]["lifecycle"]
        self.assertEqual(life["status"], "passed")
        self.assertTrue(life["endpoint_closed"])
        self.assertNotEqual(life["exit_code"], 0)
        self.assertNotEqual(life["terminated_pid"], life["replacement_pid"])
        self.assertEqual(report["cases"][0]["operation_id"], life["operation_id"])
        self.assertEqual(report["cases"][0]["attempts"][0], life["recovery"][0])

    def test_old_registry_is_still_exercised_and_legacy_report_preserved(self):
        with server(Sample) as base:
            report = self.probe(base + "/invoke", legacy=True)
            self.assertEqual(json.loads((self.tmp / "report.json").read_bytes()), report)
            self.assertEqual(len(report["cases"]), 8)
            for item in report["cases"]:
                self.assertTrue(item["passed"])
                self.assertEqual(item["attempts"][0], item["attempts"][1])
            invalid = {**self.cap, "utterances":["registry rejects this"]}
            result = self.probe(base + "/invoke", manifest=invalid, legacy=True, success=False)
            self.assertIn("production registry", result["failure"])

    def test_only_explicit_read_before_process_or_network(self):
        calls = []
        marker = self.tmp / "process-ran"
        proc = [sys.executable, "-c", "from pathlib import Path; Path("+repr(str(marker))+").touch()"]
        class Unexpected(Sample):
            def do_GET(self):
                calls.append("GET")
                super().do_GET()
            def do_POST(self):
                calls.append("POST")
                super().do_POST()
        with server(Unexpected) as base:
            for effect in (None, "idempotent", ["read"], "READ"):
                cap = {**self.cap, "effect":effect, "command":proc}
                result = self.probe(base+"/invoke", manifest=cap, process=proc, success=False)
                self.assertIn("read-only", result["failure"])
            cap = {**self.cap, "effects":"actuation"}
            self.assertIn("read-only", self.probe(base+"/invoke", manifest=cap, success=False)["failure"])
            duplicate = json.dumps(self.cap).replace('"effect": "read"', '"effect":"actuation","effect":"read"')
            self.assertIn("duplicate", self.probe(base+"/invoke", manifest=duplicate, success=False)["failure"])
            # An external process already listening cannot be claimed as the managed child.
            result = self.probe(base+"/invoke", process=proc, success=False)
            self.assertIn("already has a listener", result["failure"])
            self.assertEqual([], calls)
            # An executable appearing only inside an otherwise valid manifest is never launched.
            self.probe(base+"/invoke", manifest={**self.cap, "command":proc})
        self.assertFalse(marker.exists())

    def test_get_view_only_checks_declared_fields_and_read_methods(self):
        requests = []
        class View(Sample):
            def do_GET(self):
                requests.append((self.command,self.path,self.headers.get("X-Operation-Id")))
                if self.path == "/health":
                    self.reply(200, {"state":"ready", "version":"1"})
                elif self.path == "/archive/17":
                    self.reply(200, {"state":"recorded", "poll_count":len(requests)})
                else:
                    self.reply(400, {"error":"invalid archive"})
            def do_POST(self):
                requests.append((self.command,self.path,None))
                self.reply(405, {})
        cap = {"id":"archive.read", "version":"1", "effect":"read", "protocol":"http-get"}
        suite = {"health":{"path":"/health","expected":{"state":"ready"}},
                 "version_check":{"path":"/health","field":["version"]},
                 "cases":[{"path":"/archive/17","expected":{"state":"recorded"}},
                          {"path":"/archive/invalid","http_status":400,"expected":{"error":"invalid archive"}}]}
        with server(View) as base:
            report = self.probe(base, manifest=cap, cases=suite)
            op = report["cases"][0]["operation_id"]
            self.assertEqual(requests.count(("GET","/archive/17",op)), 2)
            self.assertEqual(report["checks"]["version"]["mode"], "client_binding")
            bad = self.probe(base, manifest={**cap,"version":"2"}, cases=suite, success=False)
            self.assertEqual(bad["checks"]["version"]["status"], "failed")
        self.assertEqual({r[0] for r in requests}, {"GET"})

    def test_faulty_provider_cannot_pass_success_refusal_health_and_replay_checks(self):
        class Faulty(Sample):
            def do_GET(self):
                self.reply(503, {"ok":False})
            def do_POST(self):
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.count = getattr(self.server,"count",0) + 1
                self.reply(200, {"operation_id":msg["operation_id"], "state":"SUCCEEDED",
                                 "evidence":{"sample_count":4, "counter":self.server.count}})
        case = {"arguments":{"timestamps_ms":"[1,2,3,4]"}, "expected":{"sample_count":4}}
        with server(Faulty) as base:
            result = self.probe(base + "/invoke", cases=[case], success=False)
        self.assertIn("changed its terminal receipt", result["cases"][0]["failure"])
        for name in ("health","invalid_arguments","version","timeout"):
            self.assertEqual(result["checks"][name]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
