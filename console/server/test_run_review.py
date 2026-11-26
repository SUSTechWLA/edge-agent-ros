"""Read-only operator review contract over the actual HTTP handler and SQLite stores."""
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from history import History
from main import Handler
from task_data import AgentFacts
from test_facts_fixture import agent_facts_server


class RunReviewTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tasks = self.root/"tasks.db"
        self.history = History(self.root/"history.db")
        self.addCleanup(self.history.close)
        agent_url = self.enterContext(agent_facts_server(self.tasks))
        handler = partial(Handler,observer=SimpleNamespace(facts=AgentFacts(agent_url),history=self.history))
        server = ThreadingHTTPServer(("127.0.0.1",0),handler)
        service = threading.Thread(target=server.serve_forever,daemon=True)
        service.start()
        self.addCleanup(service.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"

    def fetch(self,path,**params):
        with urlopen(self.url+path+"?"+urlencode(params),timeout=2) as response:
            if path=="/api/export":
                return [json.loads(line) for line in response]
            return json.load(response)

    def test_catalog_counts_filters_cursor_and_legacy_trace(self):
        path = "/api/review/tasks"
        with self.assertRaises(HTTPError) as error:
            self.fetch(path)
        self.assertEqual(error.exception.code,503)
        self.assertFalse(self.tasks.exists())
        with closing(sqlite3.connect(self.tasks)) as writer:
            writer.executescript("""CREATE TABLE tasks(id TEXT PRIMARY KEY,text,state,detail,created,updated);
                INSERT INTO tasks VALUES('a','走到门口','SUCCEEDED','ok','t','t');
                INSERT INTO tasks VALUES('b','Read speed','RECOVERY_REQUIRED','no odom','t','t');
                INSERT INTO tasks VALUES('c','Check power','BLOCKED','no sensor','t','t');
                INSERT INTO tasks VALUES('d','Look around','RUNNING','','t','t');
                INSERT INTO tasks VALUES('e','Cancel me','CANCELED','','t','t');
                INSERT INTO tasks VALUES('f','Queued','WAITING','','t','t');""")
            first = self.fetch(path,limit=2)
            counts = {"all":6,"succeeded":1,"issues":2,"active":2}
            self.assertEqual(first["counts"],counts)
            self.assertEqual([t["id"] for t in first["tasks"]],["f","e"])
            self.assertEqual(first["tasks"][0]["trace_id"],"f")
            self.assertEqual(self.fetch(path,state="issues")["counts"],counts)
            self.assertEqual([t["id"] for t in self.fetch(path,state="issues")["tasks"]],["c","b"])
            writer.executescript("""CREATE TABLE task_triggers(task_id TEXT PRIMARY KEY,trace_id);
                INSERT INTO task_triggers VALUES('d','same-mission');
                UPDATE tasks SET updated='newer' WHERE id='d';
                INSERT INTO tasks VALUES('g','Later','PLANNING','','t','t');""")
            second = self.fetch(path,limit=2,before=first["next_before"])
            self.assertEqual(second["tasks"][0]["trace_id"],"same-mission")
            self.assertEqual([t["id"] for t in second["tasks"]],["d","c"])
            third = self.fetch(path,limit=2,before=second["next_before"])
            self.assertEqual([t["id"] for t in third["tasks"]],["b","a"])
            self.assertIsNone(third["next_before"])
            for query in ({"state":"garbage"},{"limit":0},{"limit":101},{"before":"xyz!"},
                          {"before":first["next_before"],"state":"issues"}):
                with self.subTest(query=query),self.assertRaises(HTTPError) as error:
                    self.fetch(path,**query)
                self.assertEqual(error.exception.code,400)
            writer.executescript("""CREATE TABLE audit_identity(singleton INTEGER PRIMARY KEY,id);
                INSERT INTO audit_identity VALUES(1,'replacement');""")
            with self.assertRaises(HTTPError) as error:
                self.fetch(path,before=first["next_before"])
            self.assertEqual(error.exception.code,400)

    def test_filter_before_limit_export_and_replay(self):
        def record(kind,source="edge_console",**kw):
            return self.history.record({"session_id":"run"},kind=kind,source=source,detail="fixture",**kw)
        active=record("agent.planned",source="mission_agent/tasks",trace={"task_id":"inspection"})
        blocked=record("agent.blocked",source="mission_agent/tasks",trace={"task_id":"inspection"})
        diagnostic=record("telemetry.sample",level="warn")
        other=record("telemetry.sample",source="custom.module")
        for _ in range(65):
            sample=record("telemetry.sample")
        def ids(path,**q):
            data=self.fetch(path,**q)
            return [e["seq"] for e in (data if path=="/api/export" else data["events"])]
        self.assertEqual(ids("/api/events",view="changes",limit=2),[other,diagnostic])
        self.assertEqual(ids("/api/events",view="changes",limit=2,before=diagnostic),[blocked,active])
        self.assertEqual(ids("/api/events",view="issues",after=0),[blocked,diagnostic])
        self.assertEqual(ids("/api/events",view="issues",task_id="inspection"),[blocked])
        self.assertEqual(ids("/api/events",limit=1),[sample])
        self.assertEqual(self.fetch("/api/replay",at=sample)["event"]["kind"],"telemetry.sample")
        self.assertEqual(ids("/api/export",view="changes"),[active,blocked,diagnostic,other])
        self.assertEqual(ids("/api/export"),list(range(1,sample+1)))
        for path in ("/api/events","/api/export"):
            with self.assertRaises(HTTPError) as error:
                self.fetch(path,view="unknown")
            self.assertEqual(error.exception.code,400)

    def test_explicit_sample_links_are_visible_without_changing_archive_retention(self):
        def sample(trace=None):
            return self.history.record({"session_id":"run"},kind="telemetry.sample",
                source="edge_console",detail="fixture",trace=trace)
        plain=sample()
        linked=[sample(trace) for trace in (
            {"task_id":"task-known"}, {"trace_id":"trace-known"},
            {"parent_event_id":"not-yet-seen"}, {"call_id":"call-known"},
            {"trigger":{"kind":"watchdog"}}, {"trigger":{"id":"scan7"}},
            {"trigger":{"source":"test"}},
        )]
        before=self.fetch("/api/export")
        retention=self.history.retention()
        # The existing retention policy still counts these as sampling rows.
        self.assertEqual(retention["samples"],1+len(linked))
        self.assertEqual(retention["events"],1+len(linked))
        changes=self.fetch("/api/events",view="changes")["events"]
        self.assertEqual([event["seq"] for event in changes],list(reversed(linked)))
        self.assertTrue(all(event["mission_id"] is None for event in changes))
        self.assertEqual([event["seq"] for event in self.fetch("/api/export",view="changes")],linked)
        self.assertEqual(self.fetch("/api/export"),before)
        self.assertEqual(self.history.retention(),retention)
        # Automatically assigned event_id/module/code do not make a periodic
        # observation a change, but its original snapshot remains replayable.
        event=self.fetch("/api/replay",at=plain)["event"]
        self.assertTrue(event["event_id"])
        self.assertEqual(event["kind"],"telemetry.sample")


if __name__=="__main__":
    unittest.main()
