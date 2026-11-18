"""SDD-013. Use the author's actual private receipt bytes and its ModuleJournal envelope."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

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

    def execute(self, *args, **kwargs):
        return run(self.event, self.decision, self.private, self.journal, *args, **kwargs)

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

    def test_hot_contract_and_recovery_use_the_exact_original_snapshot(self):
        config = self.root/"context"
        config.mkdir()
        sample = Path(__file__).resolve().parents[1]/"contracts/design/result-verifier.json"
        target = config/"verify.json"
        target.write_bytes(sample.read_bytes())
        first = self.execute(contracts_dir=config, request_id="attempt-1")
        original = Path(first["path"]).read_bytes()
        draft = json.loads(original)
        self.assertEqual(draft["interfaces"]["result-verifier"]["version"], "1")
        snapshot = json.dumps(draft["interfaces"],sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()
        self.assertEqual(draft["contracts_sha256"], hashlib.sha256(snapshot).hexdigest())
        self.assertEqual(self.events()[-1]["data"]["contracts_sha256"], draft["contracts_sha256"])
        updated = {**json.loads(target.read_bytes()),"version":"2"}
        target.write_bytes(json_bytes(updated))
        resumed = self.execute(contracts_dir=config, request_id="attempt-1", recover_only=True)
        self.assertEqual(resumed["event_id"],first["event_id"])
        self.assertEqual(Path(resumed["path"]).read_bytes(),original)
        next_design = self.execute(contracts_dir=config)
        self.assertNotEqual(first["design_id"],next_design["design_id"])
        self.assertEqual(json.loads(Path(next_design["path"]).read_bytes())["interfaces"]["result-verifier"],updated)
        target.unlink()
        last = self.execute(contracts_dir=config)
        self.assertNotIn("result-verifier",json.loads(Path(last["path"]).read_bytes())["interfaces"])
        observed = [e for e in self.events() if e["kind"] == "plugin.design.contracts_observed"]
        self.assertEqual(len(observed),3)
        for old, new in zip(observed,observed[1:]):
            self.assertEqual(new["parent_event_id"],old["event_id"])
            self.assertEqual(new["data"]["previous_sha256"],old["data"]["contracts_sha256"])
        self.assertEqual(json.loads((self.private/".contexts"/(draft["contracts_sha256"]+".json")).read_bytes()),
                         draft["interfaces"])

    def test_invalid_contract_fails_before_inference_and_emits_only_public_diagnosis(self):
        config = self.root/"context"
        config.mkdir()
        source = Path(__file__).resolve().parents[1]/"contracts/design/ros-observation.json"
        contract = json.loads(source.read_bytes())
        target = config/"ros.json"
        with patch("plugin_design.bounded_http.read") as model:
            for data, code in ((dict(contract, reference="../private.txt"), "contract_invalid"),
                               (dict(contract, owner="private"*850), "contract_budget")):
                target.write_bytes(json_bytes(data))
                with self.assertRaisesRegex(DesignError,code):
                    self.execute("http://model","test",contracts_dir=config)
                self.assertEqual(self.events()[-1]["code"],"design."+code)
                self.assertNotIn("private.txt",json.dumps(self.events()))
            target.write_bytes(source.read_bytes())
            (config/"duplicate.json").write_bytes(source.read_bytes())
            with self.assertRaisesRegex(DesignError,"contract_conflict"):
                self.execute("http://model","test",contracts_dir=config)
            model.assert_not_called()
            self.assertFalse(list(self.private.glob("*.json")))

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

    def test_model_choice_is_bound_to_installed_contracts(self):
        from plugin_design import DESIGN_FIELDS
        config = self.root/"contracts"
        config.mkdir()
        sample = Path(__file__).resolve().parents[1]/"contracts/design/ros-observation.json"
        (config/sample.name).write_bytes(sample.read_bytes())
        choice = {key:"documented sample" for key in DESIGN_FIELDS}
        choice["interface"] = "ros-observation"
        def response(*args, **kwargs):
            return json_bytes({"done":True,"message":{"content":json.dumps(choice)}})
        with patch("plugin_design.bounded_http.read",side_effect=response) as endpoint:
            result = self.execute("http://model","fixture",contracts_dir=config)
            body = json.loads(endpoint.call_args.args[1])
            draft = json.loads(Path(result["path"]).read_bytes())
            self.assertEqual(json.loads(body["messages"][1]["content"])["contracts"],draft["interfaces"])
            self.assertIn("ros-observation",body["format"]["properties"]["interface"]["enum"])
            self.assertEqual(draft["development"]["selected_contract"],json.loads(sample.read_bytes()))
            self.assertEqual(draft["development"]["permissions_granted"],[])
            (config/sample.name).unlink()
            with self.assertRaisesRegex(DesignError,"model_reply_invalid"):
                self.execute("http://model","fixture",contracts_dir=config)


if __name__ == "__main__":
    unittest.main()
