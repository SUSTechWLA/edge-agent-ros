"""SDD-007 public author/release evidence across private receipts, files and SQLite."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_plugin_author_watch import Fixture, Watch
from test_author_tool import AGENT, speed_case
import author_tool


def events(path):
    db = sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)
    try:
        return [json.loads(row[0]) for row in db.execute("SELECT event FROM module_bus_events ORDER BY seq")]
    finally:
        db.close()


class AuthorEvents(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.public = self.root/"public"/"module-events.sqlite"
        env = patch.dict(os.environ, EDGE_AUTHOR_EVENT_DB=str(self.public))
        env.start()
        self.addCleanup(env.stop)
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.watch = self.open()
        self.addCleanup(lambda: self.watch.close())

    def open(self):
        return Watch(self.fixture.url, self.fixture.url, "fixture-model", self.root/"private")

    def test_public_commit_before_private_ack_replays_same_decision(self):
        source = self.fixture.add("speed", text="private operation for this robot")
        db = self.watch.sink.db
        db.execute("""CREATE TRIGGER inject_crash BEFORE DELETE ON author_outbox BEGIN
                      SELECT RAISE(ABORT,'injected ack failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError,"injected ack failure"):
            self.watch.poll()
        first = events(self.public)
        self.assertEqual(len(first), 1)
        receipt = next((self.watch.out/"decisions").glob("*.json"))
        fact = first[0]
        self.assertEqual(fact["kind"], "plugin.author.proposed")
        self.assertEqual(fact["parent_event_id"], source["event_id"])
        self.assertEqual(fact["trace_id"], source["trace_id"])
        self.assertEqual(fact["task_id"], "speed")
        self.assertEqual(fact["data"]["receipt_sha256"], hashlib.sha256(receipt.read_bytes()).hexdigest())
        self.assertEqual(fact["data"]["candidate_version"],
                         author_tool.capability(json.loads(receipt.read_bytes())["definition"])["version"])
        self.assertNotIn("private operation", json.dumps(first))
        self.assertNotIn("fixture-model", json.dumps(first))
        db.execute("DROP TRIGGER inject_crash")
        self.watch.close()
        self.watch = self.open()
        self.watch.poll()
        # Also exercise crash after the private file was written, before work completion.
        with self.watch.sink.db:
            self.watch.sink.db.execute("UPDATE author_work SET finished=0")
        self.assertEqual(self.watch.poll()[0]["recovered"], 1)
        self.assertEqual(events(self.public), first)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_deferred_attempts_decline_and_proposal_have_public_typed_evidence(self):
        self.fixture.add("later")
        self.fixture.add("decline", text="power down")
        self.fixture.temp_unavailable.add("later")
        self.watch.poll()
        with self.watch.sink.db:
            self.watch.sink.db.execute("UPDATE author_work SET due=0 WHERE finished=0")
        self.watch.poll()
        self.fixture.temp_unavailable.clear()
        with self.watch.sink.db:
            self.watch.sink.db.execute("UPDATE author_work SET due=0 WHERE finished=0")
        self.watch.poll()
        rows = events(self.public)
        delays = [r for r in rows if r["kind"]=="plugin.author.deferred"]
        self.assertEqual([(r["data"]["attempt"],r["data"]["http_status"]) for r in delays], [(1,503),(2,503)])
        self.assertTrue(all(r["code"]=="author.dependency_unavailable" for r in delays))
        self.assertEqual([r["code"] for r in rows if r["task_id"]=="decline"], ["author.proposal_rejected"])
        self.assertEqual([r["kind"] for r in rows if r["task_id"]=="later"][-1], "plugin.author.proposed")
        self.assertNotIn("power down", json.dumps(rows))
        self.assertNotIn(self.fixture.url, json.dumps(rows))

    def test_invalid_external_references_are_redacted_and_do_not_block_following_work(self):
        bad = self.fixture.add("bad")
        bad.update(trace_id="https://private-machine:9000", event_id="ghp_abcdefghijklmnop")
        self.fixture.add("ok")
        self.assertEqual(self.watch.poll()[0]["proposed"],1)
        rows = events(self.public)
        self.assertEqual(rows[0]["code"], "author.source_ineligible")
        self.assertEqual(rows[0]["trace_id"],rows[0]["data"]["receipt_id"])
        self.assertIsNone(rows[0].get("parent_event_id"))
        self.assertTrue(all(s not in json.dumps(rows) for s in ("private-machine","ghp_abc")))
        self.assertEqual(len(self.fixture.calls),1)

    def test_corrupt_private_receipt_quarantines_one_decision_without_model_retry(self):
        self.fixture.add("unknown")
        self.fixture.add("ok")
        receipt_id = hashlib.sha256(author_tool.json_bytes(["fixture",1])).hexdigest()
        path = self.watch.out/"decisions"/(receipt_id+".json")
        path.parent.mkdir()
        path.write_bytes(b"unreadable private data")
        counts, _ = self.watch.poll()
        self.assertEqual((counts["declined"],counts["proposed"]), (1,1))
        self.assertEqual(len(self.fixture.calls),1)
        rows = events(self.public)
        self.assertEqual(rows[0]["code"],"author.receipt_invalid")
        self.assertEqual(path.read_bytes(), b"unreadable private data")
        self.assertNotIn("unreadable private data", json.dumps(rows))


class PublisherEvents(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.registry = self.root/"registry"
        self.public = self.registry/".history"/"module-events.sqlite"
        self.name = "derived.speed"
        self.private = "confidential deployment description"
        definition = {"schema_version":1, "id":self.name, "description":self.private,
                      "unit":"km/h", "field":"speed"}
        self.definition = self.root/"private.json"
        self.definition.write_bytes(author_tool.json_bytes(definition))
        self.version = author_tool.capability(definition)["version"]
        self.cases = self.root/"test-cases.json"
        self.cases.write_bytes(author_tool.json_bytes([speed_case(1,3.6),speed_case(-.5,-1.8)]))
        self.active = self.registry/(self.name+".json")

    def command(self, action, expect, crash=None, trace="mission-trace", parent="proposal-42"):
        argv = [action, self.name] if action=="remove" else [action, str(self.definition), "--cases", str(self.cases)]
        argv += ["--expect", expect, "--registry", str(self.registry),
                 "--trace-id",trace,"--parent-event-id",parent]
        if crash:
            # Exit outside Python exception handling. The next real CLI process must reconcile.
            script = """import os,sys
sys.path.insert(0,sys.argv.pop(1))
import author_tool
stage=sys.argv.pop(1)
if stage == 'before':
    author_tool.write_atomic=lambda *args: os._exit(42)
else:
    author_tool.finish_release=lambda *args, **kw: os._exit(42)
author_tool.main()
"""
            command = [sys.executable, "-c", script, str(AGENT), crash, *argv]
        else:
            command = [sys.executable, str(AGENT/"author_tool.py"), *argv]
        return subprocess.run(command, capture_output=True, timeout=20)

    def test_cli_publish_reject_and_remove_link_exact_evidence(self):
        process = self.command("publish","absent")
        self.assertEqual(process.returncode,0,process.stderr)
        fact = json.loads(process.stdout)
        self.assertEqual(self.command("remove","absent").returncode, 1)
        self.assertEqual(json.loads(self.command("remove",self.version).stdout)["previous"], self.version)
        rows = events(self.public)
        self.assertEqual([r["kind"] for r in rows], ["plugin.release."+s for s in
                         ("requested","applied","requested","rejected","requested","applied")])
        for request, result in zip(rows[::2],rows[1::2]):
            self.assertEqual(request["parent_event_id"], "proposal-42")
            self.assertEqual(result["parent_event_id"],request["event_id"])
            self.assertEqual(result["data"]["change_id"],request["data"]["change_id"])
            self.assertEqual(result["trace_id"],"mission-trace")
        self.assertEqual(rows[1]["event_id"],fact["event_id"])
        self.assertEqual(rows[1]["data"]["change_id"],fact["change_id"])
        self.assertEqual(rows[1]["data"]["observed_version"],self.version)
        self.assertEqual(rows[1]["data"]["cases_sha256"],hashlib.sha256(self.cases.read_bytes()).hexdigest())
        self.assertEqual(rows[3]["code"],"release.version_conflict")
        self.assertEqual(rows[5]["data"]["observed_version"],"absent")
        self.assertNotIn(self.private, json.dumps(rows))
        self.assertNotIn(str(self.root), json.dumps(rows))

    def test_process_exit_before_mutation_never_replays_the_original_write(self):
        self.assertEqual(self.command("publish","absent","before").returncode,42)
        self.assertFalse(self.active.exists())
        self.assertEqual(len(events(self.public)),1)
        self.assertEqual(self.command("remove","absent").returncode,1)
        self.assertFalse(self.active.exists())
        pending, recovered, _, rejected = events(self.public)
        self.assertEqual(recovered["data"]["observation"],"previous_present")
        self.assertEqual(recovered["data"]["writer_attribution"],"unknown")
        self.assertEqual(recovered["parent_event_id"],pending["event_id"])
        self.assertEqual(rejected["code"],"release.already_absent")

    def test_invalid_public_reference_records_rejection_without_leaking_or_mutating(self):
        process = self.command("publish","absent",trace="https://private-service", parent="private value")
        self.assertEqual(process.returncode,1)
        self.assertFalse(self.active.exists())
        requested, rejected = events(self.public)
        self.assertEqual(rejected["code"],"release.invalid_reference")
        self.assertIsNone(requested.get("parent_event_id"))
        self.assertEqual(requested["trace_id"],requested["data"]["change_id"])
        self.assertNotIn("private-service", json.dumps([requested,rejected]))
        self.assertNotIn("private value",json.dumps([requested,rejected]))

    def test_recursively_nested_cases_leave_a_private_input_rejection(self):
        self.cases.write_bytes(b"["*2000+b"]"*2000)
        process = self.command("publish","absent")
        self.assertEqual(process.returncode,1)
        self.assertNotIn(b"Traceback",process.stderr)
        self.assertFalse(self.active.exists())
        requested, rejected = events(self.public)
        self.assertEqual(rejected["code"],"release.input_invalid")
        self.assertEqual(rejected["parent_event_id"],requested["event_id"])
        self.assertNotIn(str(self.cases), json.dumps([requested,rejected]))

    def test_process_exit_after_replace_with_external_change_is_not_claimed(self):
        self.assertEqual(self.command("publish","absent","after").returncode,42)
        self.assertTrue(self.active.exists())
        manual = dict(json.loads(self.active.read_bytes()), description="external edit")
        self.active.write_bytes(author_tool.json_bytes(manual))
        self.assertEqual(self.command("remove",self.version).returncode,1)
        _, recovered, _, rejected = events(self.public)
        self.assertEqual(recovered["kind"],"plugin.release.reconciled")
        self.assertEqual(recovered["data"]["observation"],"diverged")
        self.assertEqual(recovered["data"]["observed_version"],author_tool.capability(manual)["version"])
        self.assertEqual(recovered["data"]["writer_attribution"],"unknown")
        self.assertEqual(rejected["code"],"release.version_conflict")
        self.assertEqual(json.loads(self.active.read_bytes()),manual)

    def test_process_exit_after_replace_without_external_change_records_observation(self):
        self.assertEqual(self.command("publish","absent","after").returncode,42)
        self.assertEqual(self.command("remove",self.version).returncode,0)
        _, recovered, _, applied = events(self.public)
        self.assertEqual(recovered["code"],"release.target_present")
        self.assertEqual(recovered["data"]["writer_attribution"],"unknown")
        self.assertEqual(applied["data"]["action"],"remove")


if __name__ == "__main__":
    unittest.main()
