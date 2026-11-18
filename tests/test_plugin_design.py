"""SDD-013. Use the author's actual private receipt bytes and its ModuleJournal envelope."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_plugin_author_watch import Fixture, Watch
from plugin_design import DesignError, ModuleJournal, run
from author_tool import json_bytes


class PluginDesign(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.fixture.add("blocked-task", text="power down")
        watch = Watch(self.fixture.url, self.fixture.url, "fixture", self.root / "author")
        try:
            self.assertEqual(watch.poll()[0]["declined"], 1)
            self.decision = next((watch.out / "decisions").glob("*.json"))
            self.row = json.loads(watch.journal.db.execute("SELECT event FROM module_bus_events").fetchone()[0])
        finally:
            watch.close()
        self.row["source"] = "journal:plugin.author"
        self.event = self.root / "event.json"
        self.event.write_bytes(json_bytes(self.row))
        self.journal = ModuleJournal(self.root / "public" / "module-events.sqlite")
        self.addCleanup(self.journal.close)
        self.private = self.root / "drafts"

    def execute(self, *args):
        return run(self.event, self.decision, self.private, self.journal, *args)

    def events(self):
        return [json.loads(row[0]) for row in self.journal.db.execute("SELECT event FROM module_bus_events ORDER BY seq")]

    def test_real_receipt_to_private_brief_and_public_causal_chain(self):
        result = self.execute()
        path = Path(result["path"])
        draft = json.loads(path.read_bytes())
        self.assertEqual(draft["goal"], "power down")
        self.assertIsNone(draft["model_design"])
        self.assertEqual(draft["development"]["permissions_granted"], [])
        self.assertEqual(draft["source"]["event_id"], "blocked-blocked-task")
        requested, generated = self.events()
        self.assertEqual(requested["parent_event_id"], self.row["event_id"])
        self.assertEqual(generated["parent_event_id"], requested["event_id"])
        self.assertEqual(generated["data"]["draft_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(generated["trace_id"], self.row["trace_id"])
        self.assertNotIn("power down", json.dumps(self.events()))
        self.assertEqual(self.execute()["draft_sha256"], result["draft_sha256"])
        draft["goal"] = "changed"
        path.write_bytes(json_bytes(draft))
        with self.assertRaisesRegex(DesignError, "draft_conflict"):
            self.execute()
        path.unlink()
        with self.assertRaisesRegex(DesignError, "draft_missing"):
            self.execute()

    def test_untrusted_event_missing_goal_and_modified_receipt(self):
        for field, value, reason in (("source", "ros:/edge/events", "source_ineligible"),
                                     ("trace_id", "other", "source_mismatch"),
                                     ("parent_event_id", "other", "source_mismatch"),
                                     ("code", "author.source_ineligible", "source_ineligible")):
            with self.subTest(field=field):
                self.event.write_bytes(json_bytes(dict(self.row, **{field:value})))
                with self.assertRaisesRegex(DesignError, reason):
                    self.execute()
        self.event.write_bytes(json_bytes(self.row))
        receipt = json.loads(self.decision.read_bytes())
        receipt.pop("model")
        self.decision.write_bytes(json_bytes(receipt))
        with self.assertRaisesRegex(DesignError, "receipt_digest_mismatch"):
            self.execute()
        self.row["data"]["receipt_sha256"] = hashlib.sha256(self.decision.read_bytes()).hexdigest()
        self.event.write_bytes(json_bytes(self.row))
        with self.assertRaisesRegex(DesignError, "goal_unavailable") as rejected:
            self.execute()
        self.assertEqual(rejected.exception.event_id, self.events()[-1]["event_id"])
        self.assertEqual(rejected.exception.request_id, self.events()[-1]["call_id"])
        self.assertFalse(self.private.exists())
        self.assertTrue(all(e["kind"] == "plugin.design.rejected" for e in self.events()))

    def test_ollama_protocol_rejects_malformed_reply_and_reuses_valid_draft(self):
        proposal = {"interface": "Service", "behavior": "design a capability",
                    "inputs": "typed request", "outputs": "None", "dependencies": "device owner",
                    "permissions": "requires review", "example_input": "known fixture", "expected_result": "known observation",
                    "rollback": "drain requests and restore the previous provider"}
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                content = dict(proposal, extra="unexpected") if len(calls) == 1 else proposal
                wire = json.dumps({"done": True, "message": {"content": json.dumps(content)}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(wire)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_port)
        try:
            with self.assertRaisesRegex(DesignError, "model_reply_invalid"):
                self.execute(url, "fixture")
            self.assertIn("response_sha256", self.events()[-1]["data"])
            result = self.execute(url, "fixture")
            self.assertEqual(self.execute(url, "fixture")["design_id"], result["design_id"])
            self.assertEqual(len(calls), 2)
            suggestion = json.loads(Path(result["path"]).read_bytes())["model_design"]
            self.assertEqual(suggestion["proposal"], proposal)
            self.assertEqual(suggestion["unresolved_fields"], ["outputs"])
            self.assertEqual(self.events()[-1]["data"]["review_state"], "unverified")
            self.assertEqual(suggestion["request_sha256"], hashlib.sha256(json_bytes(calls[-1])).hexdigest())
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        with self.assertRaisesRegex(DesignError, "model_unavailable"):
            self.execute(url, "another-model")
        self.assertEqual(self.events()[-1]["code"], "design.model_unavailable")


if __name__ == "__main__":
    unittest.main()
