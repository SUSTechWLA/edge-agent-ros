"""Console task facts over the real HTTP handlers, including pre-trigger SQLite data."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "console/server"), str(ROOT)]

TASK = "wp4-legacy-survey"
TEXT = "Review the recorded position"
STEP = {"tool": "status", "version": "1", "status": "SUCCEEDED", "args": {},
        "operation_id": TASK + "/step/1", "evidence": {"x": 2, "y": 3}}
FINAL_DETAIL = "Historical observation verified"


def seed_legacy(directory):
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(directory / "tasks.db")) as db:
        db.executescript("""
            CREATE TABLE tasks(id TEXT PRIMARY KEY,text TEXT,state TEXT,detail TEXT,
                provider TEXT,steps TEXT,cursor INTEGER,retries INTEGER,due REAL,created TEXT,updated TEXT);
            CREATE TABLE events(seq INTEGER PRIMARY KEY,utc TEXT,task_id TEXT,step INTEGER,
                kind TEXT,detail TEXT,operation_id TEXT);
        """)
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (TASK, TEXT, "SUCCEEDED", "Position recorded", "local", json.dumps([STEP]), 1, 0, 0, stamp, stamp))
        db.executemany("INSERT INTO events VALUES(?,?,?,?,?,?,?)", [
            (7, stamp, TASK, 0, "planned", "Existing plan", None),
            (13, stamp, TASK, 0, "dispatch", "Status read", STEP["operation_id"]),
            (21, stamp, TASK, 0, "completed", "Position recorded", STEP["operation_id"])])
        db.commit()


def seed_readonly_snapshot(directory, pending_wal, detail=FINAL_DETAIL):
    """Copy a quiescent writer's files before or after the final WAL checkpoint."""
    directory.mkdir()
    with tempfile.TemporaryDirectory() as temp:
        writer = Path(temp)
        seed_legacy(writer)
        with closing(sqlite3.connect(writer / "tasks.db")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("UPDATE tasks SET detail=? WHERE id=?", (detail, TASK))
            stamp = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO events VALUES(34,?,?,0,'verified',?,?)",
                       (stamp, TASK, FINAL_DETAIL, STEP["operation_id"]))
            db.commit()
            if pending_wal:
                for name in ("tasks.db", "tasks.db-wal", "tasks.db-shm"):
                    shutil.copy2(writer / name, directory / name)
        if not pending_wal:
            if (writer / "tasks.db-wal").exists() or (writer / "tasks.db-shm").exists():
                raise AssertionError("cleanly closed fixture still has sidecars")
            shutil.copy2(writer / "tasks.db", directory / "tasks.db")


@contextmanager
def serving(server):
    with server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


class ConsoleFactsTest(unittest.TestCase):
    def setUp(self):
        from service import Handler as AgentHandler, Journal
        from main import Handler as ConsoleHandler
        from history import History
        from task_data import AgentFacts

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name) / "owner"
        self.directory = directory
        seed_legacy(directory)
        self.journal = Journal(directory)
        self.addCleanup(self.journal.owner.close)
        self.addCleanup(self.journal.db.close)
        agent = ThreadingHTTPServer(("127.0.0.1", 0), AgentHandler)
        agent.journal = self.journal
        agent.task_db = directory / "tasks.db"
        agent.registry_db = directory / "module-events.sqlite"
        self.agent = self.enterContext(serving(agent))
        self.facts = AgentFacts(self.agent)
        # The production Console handler receives only a facts client. Providing a
        # local task path here would hide regressions in review and session routing.
        history = History(Path(temp.name) / "console-history.db")
        self.addCleanup(history.close)
        handler = partial(ConsoleHandler, observer=SimpleNamespace(facts=self.facts, history=history))
        self.console = self.enterContext(serving(ThreadingHTTPServer(("127.0.0.1", 0), handler)))
        self.client = build_opener(ProxyHandler({}))

    def get(self, base, path, **params):
        with self.client.open(base + path + "?" + urlencode(params), timeout=3) as reply:
            return json.load(reply)

    def snapshot(self):
        db = self.journal.db
        with self.journal.lock:
            return tuple(tuple(tuple(row) for row in db.execute(sql)) for sql in (
                "SELECT * FROM tasks ORDER BY id", "SELECT * FROM events ORDER BY seq",
                "SELECT * FROM task_triggers ORDER BY task_id"))

    def test_unmounted_console_matches_authoritative_task(self):
        before = self.snapshot()
        task = self.get(self.agent, "/agent/tasks", id=TASK)
        context = self.get(self.console, "/api/task-context", task_id=TASK)
        self.assertTrue(context["task_available"])
        self.assertEqual(context, self.get(self.agent, "/agent/task-context", trace_id=TASK))
        self.assertEqual(context["scope"], {"task_id": TASK, "trace_id": TASK})
        self.assertEqual(context["task"]["steps"], [STEP])
        for field in ("id", "text", "state", "detail", "created", "updated", "steps", "trace_id"):
            self.assertEqual(context["task"][field], task[field], field)
        self.assertEqual((context["event_window"]["first_seq"], context["event_window"]["last_seq"]), (7, 21))
        index = self.get(self.console, "/api/review/tasks", state="succeeded", limit=1)
        self.assertEqual(index["tasks"][0]["id"], TASK)
        self.assertEqual(index["counts"]["succeeded"], 1)
        self.assertEqual(index, self.get(self.agent, "/agent/task-index", state="succeeded", limit=1))
        self.assertEqual(index, self.get(self.console, "/api/sessions", state="succeeded", limit=1))
        self.assertEqual(self.snapshot(), before)

    def test_public_context_resumes_an_archived_task_cursor(self):
        before = self.snapshot()
        params = {"task_id": TASK, "limit": 1}
        first = self.get(self.console, "/api/task-context", **params)
        self.assertEqual([row["seq"] for row in first["events"]], [7])
        self.assertTrue(first["events_has_more"])
        params.update(source_id=first["event_window"]["source_id"], after=first["next_after"], limit=2)
        resumed = self.get(self.console, "/api/task-context", **params)
        self.assertEqual(resumed, self.get(self.agent, "/agent/task-context", **params))
        self.assertEqual([row["seq"] for row in resumed["events"]], [13, 21])
        self.assertEqual(resumed["next_after"], 21)
        self.assertFalse(resumed["events_has_more"])
        self.assertEqual(self.snapshot(), before)

    def test_old_event_cursor_resumes_same_sequence_after_reconnect(self):
        from task_data import AgentFacts

        before = self.snapshot()
        first = self.facts.get("task-events", {"after": 0, "limit": 1})
        self.assertEqual([event["seq"] for event in first["events"]], [7])
        self.assertTrue(first["events_has_more"])
        self.assertEqual(first["next_after"], 7)
        second = AgentFacts(self.agent).get("task-events", {
            "source_id": first["source_id"], "after": first["next_after"], "limit": 2})
        self.assertFalse(second["events_has_more"])
        self.assertEqual(second["next_after"], 21)
        self.assertEqual([event["seq"] for event in second["events"]], [13, 21])
        task = self.get(self.agent, "/agent/tasks", id=TASK, after=7, limit=2)
        self.assertFalse(task["events_has_more"])
        for event, stored in zip(second["events"], task["events"], strict=True):
            for field in ("seq", "task_id", "step", "kind", "detail", "operation_id"):
                self.assertEqual(event[field], stored[field], field)
        with self.assertRaises(HTTPError) as stale:
            self.get(self.agent, "/agent/task-events", source_id="agent:old", after=13)
        self.assertEqual(stale.exception.code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_observe_only_console_uses_standalone_reader_and_denies_commands(self):
        from facts_server import make_server
        from main import Handler as ConsoleHandler
        from task_data import AgentFacts
        from history import History

        before = self.snapshot()
        directory = self.directory
        facts = self.enterContext(serving(make_server(("127.0.0.1", 0), directory)))
        self.enterContext(patch.dict(os.environ, {"EDGE_CONSOLE_OBSERVE_ONLY": "1", "EDGE_FACTS_URL": facts,
                                                  "EDGE_AGENT_URL": "http://127.0.0.1:1"}))
        store = History(directory.parent / "observe.db")
        self.addCleanup(store.close)
        handler = partial(ConsoleHandler, observer=SimpleNamespace(facts=AgentFacts(), history=store))
        ui = self.enterContext(serving(ThreadingHTTPServer(("127.0.0.1", 0), handler)))
        params = {"id": TASK, "after": 7, "limit": 1}
        owner = self.get(self.agent, "/agent/tasks", **params)
        self.assertEqual(self.get(facts, "/agent/tasks", **params), owner)
        self.assertEqual(self.get(ui, "/api/task-detail", **params), owner)
        self.assertEqual(owner["steps"], [STEP])
        self.assertEqual(owner["events"][0]["seq"], 13)
        self.assertTrue(owner["events_has_more"])
        self.assertEqual(self.get(ui, "/api/task-context", task_id=TASK)["task"]["state"], owner["state"])
        for endpoint, path, method, status in (
            (ui, "/agent/tasks?id=" + TASK, "GET", 403), (ui, "/agent/tasks", "POST", 403),
            (ui, "/agent/retry", "POST", 403), (facts, "/agent/tasks", "POST", 405),
            (facts, "/agent/retry", "POST", 405)):
            with self.subTest(path=path, method=method), self.assertRaises(HTTPError) as denied:
                self.client.open(Request(endpoint + path, method=method, data=b"{}" if method == "POST" else None,
                                         headers={"Content-Type": "application/json"}), timeout=3)
            self.assertEqual(denied.exception.code, status)
        self.assertEqual(self.snapshot(), before)


class StandaloneFactsSnapshotTest(unittest.TestCase):
    def test_reads_the_last_commit_after_clean_shutdown_or_with_pending_wal(self):
        from facts_server import make_server

        with tempfile.TemporaryDirectory() as temp:
            for pending_wal in (False, True):
                with self.subTest(pending_wal=pending_wal):
                    directory = Path(temp) / ("uncheckpointed" if pending_wal else "closed")
                    seed_readonly_snapshot(directory, pending_wal)
                    expected = {"tasks.db", "tasks.db-wal", "tasks.db-shm"} if pending_wal else {"tasks.db"}
                    self.assertEqual({path.name for path in directory.iterdir()}, expected)
                    # Inspect just the main file to prove the newest transaction
                    # can only be served by SQLite reading the WAL in this case.
                    uri = (directory / "tasks.db").as_uri() + "?immutable=1"
                    with closing(sqlite3.connect(uri, uri=True)) as main_only:
                        stored = main_only.execute("SELECT detail FROM tasks WHERE id=?", (TASK,)).fetchone()[0]
                    self.assertEqual(stored, "Position recorded" if pending_wal else FINAL_DETAIL)
                    client = build_opener(ProxyHandler({}))
                    with serving(make_server(("127.0.0.1", 0), directory)) as facts:
                        with client.open(facts + "/agent/tasks?id=" + TASK + "&after=21&limit=1", timeout=3) as reply:
                            task = json.load(reply)
                    self.assertEqual(task["detail"], FINAL_DETAIL)
                    self.assertEqual(task["steps"], [STEP])
                    self.assertEqual([row["seq"] for row in task["events"]], [34])
                    self.assertFalse(task["events_has_more"])


if __name__ == "__main__":
    unittest.main()
