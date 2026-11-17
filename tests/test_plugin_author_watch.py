import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from plugin_author_watch import Watch


class Fixture:
    def __init__(self):
        self.tasks = {}
        self.events = []
        self.calls = []
        self.temp_unavailable = set()
        self.pruned = 0
        self.queries = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body):
                wire = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

            def do_GET(self):
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                fixture.queries.append((parts.path, query))
                if parts.path == "/api/events":
                    after, limit = int(query["after"][0]), int(query["limit"][0])
                    items = [r for r in fixture.events if r["seq"] > max(after, fixture.pruned)
                             and r["kind"] == query["kind"][0]]
                    return self.reply(200, {"archive_id":"fixture", "events":items[:limit], "cursor":len(fixture.events),
                                            "pruned_through_seq":fixture.pruned, "has_more":len(items)>limit})
                if parts.path == "/agent/tasks":
                    key = query["id"][0]
                    if key in fixture.temp_unavailable:
                        return self.reply(503, {})
                    if key in fixture.tasks:
                        return self.reply(200, fixture.tasks[key])
                self.reply(404, {})

            def do_POST(self):
                if self.path != "/api/chat":
                    raise AssertionError("unexpected write: " + self.path)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                prompt = json.loads(body["messages"][1]["content"])
                fixture.calls.append(prompt)
                if prompt["request"] == "power down":
                    result = {"field":"unsupported", "unit":"none", "op":"read", "threshold":0}
                else:
                    result = {"field":"speed", "unit":"km/h", "op":"read", "threshold":0}
                self.reply(200, {"message":{"content":json.dumps(result)}})

        self.server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval":0.05})
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def add(self, key, text="read speed in km/h", code="planner.capability_missing"):
        event_id, trace_id = "blocked-"+key, "trace-"+key
        trace = {"trace_id":trace_id, "event_id":event_id, "layer":"planner"}
        journal = {"task_id":key, "event_id":event_id, "operation_id":None, "kind":"blocked",
                   "payload":json.dumps({"trace":trace, "diagnostic":{"code":code}})}
        self.tasks[key] = {"id":key, "trace_id":trace_id, "text":text, "state":"BLOCKED", "steps":[],
                           "events":[journal], "events_has_more":False}
        row = {"seq":len(self.events)+1, "kind":"agent.blocked", "source":"mission_agent/tasks", "task_id":key,
               "trace_id":trace_id, "event_id":event_id, "operation_id":None, "code":code}
        self.events.append(row)
        return row


class TestPluginAuthorWatch(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.out = Path(workspace.name)
        self.watch = self.open()
        self.addCleanup(lambda: self.watch.close())

    def open(self):
        return Watch(self.fixture.url, self.fixture.url, "fixture-model", self.out)

    def decisions(self):
        return {r["source"]["seq"]:r for r in
                (json.loads(p.read_text(encoding="utf-8")) for p in (self.out/"decisions").glob("*.json"))}

    def test_proposal_restart_and_interrupted_sql_commit(self):
        tag = self.fixture.add("sample", code="planner.not_configured")
        with self.assertRaises(sqlite3.OperationalError):
            self.open()
        self.assertEqual(self.watch.poll()[0]["proposed"], 1)
        record = self.decisions()[1]
        self.assertEqual(record["source"], {"archive_id":"fixture", "seq":1, "task_id":"sample",
                         "trace_id":tag["trace_id"], "event_id":tag["event_id"], "reason_code":tag["code"]})
        self.assertEqual(record["status"], "proposed")
        self.assertRegex(record["definition"]["id"], r"^derived\.task_[0-9a-f]{32}$")
        self.assertEqual(record["definition"]["unit"], "km/h")
        self.assertEqual(record["model"]["source_task"]["trace_id"], tag["trace_id"])
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(self.fixture.calls[0]["request"], "read speed in km/h")
        self.assertEqual(self.fixture.calls[0]["reason_code"], "planner.not_configured")
        self.assertIn("km/h", self.fixture.calls[0]["allowed_units"]["speed"])
        self.assertEqual(self.fixture.tasks["sample"]["state"], "BLOCKED")
        self.watch.close()
        self.watch = self.open()
        self.assertFalse(any(self.watch.poll()[0].values()))
        with self.watch.sink.db:
            self.watch.sink.db.execute("UPDATE author_work SET finished=0")
        self.assertEqual(self.watch.poll()[0]["recovered"], 1)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_unsupported_mismatched_missing_and_dispatched_tasks_are_final(self):
        self.fixture.add("unsupported", text="power down")
        self.fixture.add("dispatched")
        self.fixture.tasks["dispatched"]["steps"] = [{"operation_id":"already-issued"}]
        self.fixture.add("different-trace")["trace_id"] = "previous-trace"
        self.fixture.add("gone")
        del self.fixture.tasks["gone"]
        counts, _ = self.watch.poll()
        self.assertEqual(counts["declined"], 4)
        self.assertEqual(len(self.fixture.calls), 1)
        records = self.decisions()
        self.assertTrue(all(r["reason"] and "definition" not in r for r in records.values()))
        self.assertEqual(records[1]["model"]["reply"]["field"], "unsupported")
        self.assertFalse(any(self.watch.poll()[0].values()))

    def test_an_untrusted_projection_does_not_claim_the_native_event(self):
        projection = self.fixture.add("same-task")
        projection["source"] = "ros:/edge/events"
        self.fixture.events.append(dict(projection, seq=2, source="mission_agent/tasks"))
        counts, _ = self.watch.poll()
        self.assertEqual((counts["declined"], counts["proposed"]), (1, 1))
        self.assertEqual(self.decisions()[2]["source"]["task_id"], "same-task")

    def test_deferred_task_does_not_block_following_event_and_retry_survives_restart(self):
        self.fixture.add("temporarily-unavailable")
        self.fixture.add("ready")
        self.fixture.temp_unavailable.add("temporarily-unavailable")
        counts, _ = self.watch.poll()
        self.assertEqual((counts["deferred"], counts["proposed"]), (1,1))
        self.assertEqual(set(self.decisions()), {2})
        self.watch.close()
        self.watch = self.open()
        self.fixture.temp_unavailable.clear()
        with self.watch.sink.db:
            self.watch.sink.db.execute("UPDATE author_work SET due=0 WHERE finished=0")
        self.assertEqual(self.watch.poll()[0]["proposed"], 1)
        self.assertEqual(set(self.decisions()), {1,2})
        self.assertEqual(self.watch.sink.db.execute("SELECT attempts FROM author_work WHERE seq=1").fetchone(), (2,))

    def test_retention_gap_and_bounded_batch(self):
        for i in range(36):
            self.fixture.add(str(i), code="navigation.timeout")
        self.fixture.pruned = 2
        self.assertEqual(self.watch.poll(), (dict(proposed=0,declined=32,recovered=0,deferred=0), True))
        self.assertEqual(self.watch.poll()[0]["declined"], 2)
        self.assertEqual(self.watch.sink.db.execute("SELECT pruned_through_seq FROM retention_notices").fetchone(), (2,))
        self.assertEqual(len(self.decisions()), 34)
        self.assertEqual(self.fixture.calls, [])
        self.assertTrue(all(q["kind"] == ["agent.blocked"] for path,q in self.fixture.queries if path == "/api/events"))


if __name__ == "__main__":
    unittest.main()
