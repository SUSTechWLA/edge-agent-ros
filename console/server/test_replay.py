"""Exercise the public diagnostic boundaries without a live robot or model."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import zlib
from contextlib import closing, contextmanager
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

import rclpy

from observer import Observer
from main import Handler
from task_data import AgentFacts
from test_facts_fixture import agent_facts_server


class ReplayTest(unittest.TestCase):
    def test_import_continues_after_legacy_nonfinite_model_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / "tasks.db"
            db = sqlite3.connect(source)
            db.executescript("""
                CREATE TABLE audit_identity(singleton INTEGER,id TEXT);
                INSERT INTO audit_identity VALUES(1,'legacy-model');
                CREATE TABLE events(seq INTEGER PRIMARY KEY,utc TEXT,task_id TEXT,step INTEGER,
                                    kind TEXT,detail TEXT,operation_id TEXT,payload TEXT);
                INSERT INTO events VALUES
                    (1,'2025-11-01T12:00:00Z','diagnose',0,'model.response','','operation-1',
                    '{"trace":{"event_id":"old-reply","trace_id":"old-trace","artifact":{"rows":[NaN,-Infinity,1e999]}}}'),
                    (2,'2025-11-01T12:00:01Z','diagnose',0,'blocked','','operation-1',
                    '{"trace":{"event_id":"old-block","parent_event_id":"old-reply","trace_id":"old-trace"}}');
            """)
            db.close()
            env = {"EDGE_CONSOLE_DIR": str(path / "console"),
                   "EDGE_LEDGER_PATH": str(path / "absent.db"), "EDGE_ENVIRONMENT": "hardware"}
            rclpy.init()
            try:
                with agent_facts_server(source) as url, patch.dict(os.environ, {**env, "EDGE_AGENT_URL":url}):
                    for _ in range(2):
                        obs = Observer()
                        try:
                            obs.poll_facts()
                            obs.tick()
                            events = [json.loads(line) for line in obs.history.export({"trace_id":"old-trace"},True)]
                            self.assertEqual([e["event_id"] for e in events], ["old-reply","old-block"])
                            self.assertEqual([e["data"]["source_seq"] for e in events], [1,2])
                            archived = events[0]["data"]
                            self.assertEqual(archived["payload"]["trace"]["artifact"]["rows"], ["NaN","-Infinity","1e999"])
                            self.assertCountEqual(archived["legacy_nonfinite_literals"], ["NaN","-Infinity","1e999"])
                            self.assertNotIn("legacy_nonfinite_literals", events[1]["data"])
                            self.assertEqual(events[1]["cause_seq"], events[0]["seq"])
                        finally:
                            obs.history.close()
                            obs.destroy_node()
            finally:
                rclpy.shutdown()

    def test_authoritative_transitions_survive_disconnect(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            journal=sqlite3.connect(root/"tasks.db")
            journal.executescript("""
                CREATE TABLE audit_identity(singleton INTEGER,id TEXT);
                INSERT INTO audit_identity VALUES(1,'agent-v1');
                CREATE TABLE events(seq INTEGER PRIMARY KEY,utc TEXT,task_id TEXT,step INTEGER,
                                    kind TEXT,detail TEXT,operation_id TEXT,payload TEXT);
                INSERT INTO events VALUES(1,'2025-01-01T00:00:01Z','inspect',0,'accepted','inspect A',NULL,'{}'),
                  (2,'2025-01-01T08:00:02+08:00','inspect',0,'dispatch','submitted','inspect/step/1','{}');
            """)
            journal.close()
            ledger=sqlite3.connect(root/"core.db")
            ledger.executescript("""
                CREATE TABLE audit_identity(singleton INTEGER,id TEXT);
                INSERT INTO audit_identity VALUES(1,'core-v1');
                CREATE TABLE missions(mission_id TEXT,state TEXT,detail TEXT);
                CREATE TABLE steps(mission_id TEXT,step INTEGER,x REAL,y REAL,operation_id TEXT,
                                    action_result TEXT,evidence_x REAL,evidence_y REAL,odom_stamp_ns INTEGER);
                INSERT INTO missions VALUES('inspect/step/1','SUCCEEDED','verified');
                INSERT INTO steps VALUES('inspect/step/1',0,1,0,'nav','ok',1,0,5);
                CREATE TABLE mission_events(seq INTEGER PRIMARY KEY,utc TEXT,mission_id TEXT,operation_id TEXT,
                    kind TEXT,prior_state TEXT,state TEXT,detail TEXT,cause_seq INTEGER);
                INSERT INTO mission_events VALUES
                  (1,'2025-01-01T00:00:03Z','inspect/step/1',NULL,'mission.created',NULL,'RUNNING','accepted',NULL),
                  (2,'2025-01-01T00:00:04Z','inspect/step/1','nav','step.dispatch',NULL,NULL,'{"step":0}',1),
                  (3,'2025-01-01T00:00:05Z','inspect/step/1','nav','step.verified',NULL,NULL,
                      '{"step":0,"observed_x":1,"observed_y":0,"odom_stamp_ns":5}',2),
                  (4,'2025-01-01T00:00:06Z','inspect/step/1','nav','mission.transition','RUNNING','SUCCEEDED','verified',3);
            """)
            ledger.close()
            env={"EDGE_CONSOLE_DIR":str(root/"console"),"EDGE_LEDGER_PATH":str(root/"core.db"),
                 "EDGE_ENVIRONMENT":"hardware"}
            rclpy.init()
            try:
                with agent_facts_server(root/"tasks.db") as url, patch.dict(os.environ,{**env,"EDGE_AGENT_URL":url}):
                    for _ in range(2):
                        observer=Observer()
                        try:
                            observer.poll_facts()
                            observer.tick()
                            rows=[json.loads(line) for line in observer.history.export({"task_id":"inspect"},True)]
                            self.assertEqual(len(rows),6)
                            self.assertEqual(rows[2]["cause_seq"],rows[1]["seq"])
                            self.assertEqual(rows[-1]["cause_seq"],rows[-2]["seq"])
                            earlier=rows[2]["snapshot"]
                            self.assertEqual(earlier["missions"][0]["state"],"RUNNING")
                            self.assertIsNone(earlier["missions"][0]["steps"][0]["evidence_x"])
                            self.assertIsNone(earlier["telemetry_at"])
                            self.assertFalse(earlier["sim_world_aligned"])
                            self.assertEqual(rows[-1]["snapshot"]["missions"][0]["steps"][0]["evidence_x"],1)
                        finally:
                            observer.history.close()
                            observer.destroy_node()
            finally:
                rclpy.shutdown()


class LateDispatchTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.task_path = root / "tasks.db"
        self.task = self.enterContext(closing(sqlite3.connect(self.task_path)))
        self.task.executescript("""
            CREATE TABLE audit_identity(singleton INTEGER,id TEXT);
            INSERT INTO audit_identity VALUES(1,'owner-1');
            CREATE TABLE events(seq INTEGER PRIMARY KEY,utc TEXT,task_id TEXT,step INTEGER,
                                kind TEXT,detail TEXT,operation_id TEXT,payload TEXT);
        """)
        self.core = self.enterContext(closing(sqlite3.connect(root / "core.db")))
        self.core.executescript("""
            CREATE TABLE audit_identity(singleton INTEGER,id TEXT);
            INSERT INTO audit_identity VALUES(1,'core-1');
            CREATE TABLE missions(mission_id TEXT,state TEXT,detail TEXT);
            CREATE TABLE steps(mission_id TEXT,step INTEGER);
            CREATE TABLE mission_events(seq INTEGER PRIMARY KEY,utc TEXT,mission_id TEXT,operation_id TEXT,
                                        kind TEXT,prior_state TEXT,state TEXT,detail TEXT,cause_seq INTEGER);
        """)
        self.enterContext(patch.dict(os.environ, {"EDGE_CONSOLE_DIR": str(root / "console"),
            "EDGE_LEDGER_PATH": str(root / "core.db"), "EDGE_ENVIRONMENT": "hardware"}))
        rclpy.init()
        self.addCleanup(rclpy.shutdown)
        self.client = build_opener(ProxyHandler({}))

    @contextmanager
    def console(self):
        observer = Observer()
        try:
            with ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, observer=observer)) as server:
                self.base = f"http://127.0.0.1:{server.server_port}"
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
                thread.start()
                try:
                    yield observer
                finally:
                    server.shutdown()
                    thread.join()
        finally:
            observer.history.close()
            observer.destroy_node()

    def read(self, path, **params):
        with self.client.open(self.base + path + "?" + urlencode(params), timeout=3) as reply:
            return ([json.loads(line) for line in reply] if path == "/api/export" else json.load(reply))

    def poll_online(self, observer):
        with agent_facts_server(self.task_path) as url:
            observer.facts = AgentFacts(url)
            observer.poll_facts()
        # The server is now closed. A second poll on the existing client will see a real refusal.

    def core_event(self, mission, kind="mission.created", cause=None):
        with self.core:
            self.core.execute("""INSERT INTO mission_events VALUES(NULL,'2025-01-01T00:00:00Z',?,NULL,?,
                NULL,'RUNNING','core evidence',?)""", (mission, kind, cause))

    def dispatch(self, task, mission, event_id):
        trace = {"event_id":event_id, "trace_id":"trace-" + task,
                 "trigger":{"kind":"user","id":"trigger-" + task,"source":"operator"}}
        with self.task:
            self.task.execute("""INSERT INTO events VALUES(NULL,'2025-01-01T00:00:00Z',?,0,'dispatch',
                'delegated',?,?)""", (task, mission, json.dumps({"trace":trace})))

    def events(self, **params):
        return self.read("/api/events", after=0, **params)["events"]

    def test_pre_migration_operation_filters_keep_unlinked_rows_and_original_provenance(self):
        folder = Path(os.environ["EDGE_CONSOLE_DIR"])
        folder.mkdir()
        with closing(sqlite3.connect(folder / "history.db")) as archive:
            # Freeze the previous on-disk format. Its two genuine producer cursors are
            # populated, but the core event was ingested before the dispatch was visible.
            archive.executescript("""
                CREATE TABLE archive_identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    id TEXT NOT NULL,pruned_through INTEGER NOT NULL);
                INSERT INTO archive_identity VALUES(1,'pre-migration-archive',0);
                CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,utc TEXT NOT NULL,observed_at REAL NOT NULL,
                    kind TEXT NOT NULL,level TEXT NOT NULL,source TEXT NOT NULL,session_id TEXT NOT NULL,
                    mission_id TEXT,operation_id TEXT,detail TEXT NOT NULL,data TEXT NOT NULL,cause_seq INTEGER,
                    source_id TEXT,source_seq INTEGER,snapshot BLOB NOT NULL,event_id TEXT,trace_id TEXT,
                    parent_event_id TEXT,task_id TEXT,module TEXT,layer TEXT,code TEXT,call_id TEXT,
                    trigger_kind TEXT,trigger_id TEXT,trigger_source TEXT,ingress_hash TEXT,UNIQUE(source_id,source_seq));
            """)
            snapshot = zlib.compress(b'{"session_id":"old","ledger_id":"core-1","missions":[]}')
            archive.executemany("""INSERT INTO events(seq,utc,observed_at,kind,level,source,session_id,
                mission_id,operation_id,detail,data,source_id,source_seq,snapshot,event_id,trace_id,task_id)
                VALUES(?,'2025-01-01T00:00:00Z',1,?,'info',?,'old',?,?,'evidence',?,?,?,?,?,?,?)""", [
                (10,"mission.created","edge_core/ledger","opaque",None,'{"source_seq":1,"source_cause_seq":null}',
                 "core-1",1,snapshot,"old-creation","opaque",None),
                (12,"agent.dispatch","mission_agent/tasks","task","opaque","{}","agent:owner-1",2,
                 snapshot,"old-dispatch","trace-task","task")])
            archive.commit()
            columns = {r[1] for r in archive.execute("PRAGMA table_info(events)")}
            self.assertNotIn("peer_source", columns)
            for field, value in (("mission_id","task"), ("trace_id","trace-task")):
                old = archive.execute(f"""SELECT seq FROM events WHERE source='edge_core/ledger' AND mission_id IN
                    (SELECT operation_id FROM events WHERE source='mission_agent/tasks' AND {field}=?)""", (value,))
                self.assertEqual(old.fetchall(), [(10,)])
        # Reingest a later retry for the same operation so the migration also exercises the linker.
        with self.task:
            self.task.execute("""INSERT INTO events VALUES(3,'2025-01-01T00:00:01Z','task',0,
                'dispatch','retry','opaque','{"trace":{"event_id":"old-retry","trace_id":"trace-task"}}')""")
        for _ in range(2):
            with self.console() as observer:
                self.poll_online(observer)
                self.assertEqual(observer.history.source_cursor("core-1"), 1)
                self.assertEqual(observer.history.source_cursor("agent:owner-1"), 3)
                for key, value in (("task_id","task"), ("trace_id","trace-task")):
                    query = {"source":"edge_core/ledger",key:value}
                    page = self.read("/api/events", after=0, limit=1, **query)
                    self.assertEqual(page["archive_id"], "pre-migration-archive")
                    self.assertEqual([r["seq"] for r in page["events"]], [10])
                    self.assertEqual(self.read("/api/event-stats", **query)["count"], 1)
                    row = page["events"][0]
                    self.assertEqual((row["event_id"],row["trace_id"]), ("old-creation","opaque"))
                    self.assertEqual((row["task_id"],row["parent_event_id"],row["cause_seq"]), (None,None,None))
                    self.assertEqual(row["data"], {"source_seq":1,"source_cause_seq":None})
                    self.assertEqual(self.read("/api/replay", at=10)["event"], row)
                    exported = self.read("/api/export", snapshot=1, **query)
                    self.assertEqual([r["seq"] for r in exported], [10])
                    self.assertEqual(exported[0]["snapshot"]["event"], {**row,"schema_version":1})

    def test_late_dispatch_repairs_replay_and_preserves_core_causes_and_cursors(self):
        with self.console() as observer:
            self.poll_online(observer)  # Witness the identity even though this Agent has no events yet.
            self.core_event("opaque")
            self.core_event("opaque", "mission.transition", 1)
            self.core_event("opaque-sibling", cause=1)
            self.core_event("opaque-unclaimed")
            self.core_event("core-parent-missing", cause=73)
            observer.poll_facts()
            before = self.events(source="edge_core/ledger")
            self.assertEqual([e["source_seq"] for e in before], [1,2,3,4,5])
            self.assertTrue(all(e["task_id"] is None for e in before))
            snapshot = self.read("/api/replay", at=before[0]["seq"])
            offline_facts = observer.facts

        with self.console() as observer:
            observer.facts = offline_facts
            self.core_event("opaque", "mission.transition", 2)
            observer.poll_facts()
            self.assertEqual(observer.history.source_cursor("core-1"), 6)
            self.dispatch("task", "opaque", "original-dispatch")
            self.dispatch("task", "opaque-sibling", "sibling-dispatch")
            self.dispatch("task", "opaque", "repeated-dispatch")
            self.dispatch("separate", "core-parent-missing", "another-dispatch")
            self.poll_online(observer)
            first = self.events(event_id="original-dispatch")[0]
            rows = self.events(source="edge_core/ledger")
            for old, new in zip(before, rows):
                for field in ("seq","event_id","utc","source_id","source_seq","observed_at","detail"):
                    self.assertEqual(old[field], new[field])
            for index in (0,1,2,5):
                for field in ("task_id","trace_id","trigger_kind","trigger_id","trigger_source"):
                    self.assertEqual(rows[index][field], first[field])
            self.assertIsNone(rows[3]["task_id"])
            self.assertEqual(rows[4]["task_id"], "separate")
            self.assertEqual(rows[4]["data"]["source_cause_seq"], 73)
            self.assertIsNone(rows[4]["cause_seq"])
            self.assertIsNone(rows[4]["parent_event_id"])
            self.assertEqual((rows[0]["parent_event_id"], rows[0]["cause_seq"]), ("original-dispatch", first["seq"]))
            self.assertEqual((rows[0]["data"]["parent_task_id"], rows[0]["data"]["agent_dispatch_seq"]),
                             ("task", first["seq"]))
            for child, parent in ((1,0),(2,0),(5,1)):
                self.assertEqual((rows[child]["cause_seq"],rows[child]["parent_event_id"]),
                                 (rows[parent]["seq"], rows[parent]["event_id"]))
            replay = self.read("/api/replay", at=before[0]["seq"])
            self.assertEqual(replay["event"], rows[0])
            self.assertEqual({k:v for k,v in snapshot.items() if k!="event"}, {k:v for k,v in replay.items() if k!="event"})
            first_page = self.read("/api/events", source="edge_core/ledger", trace_id="trace-task", after=0, limit=1)
            self.assertTrue(first_page["has_more"])
        with self.console() as observer:
            self.poll_online(observer)
            self.assertEqual(observer.history.source_cursor("core-1"), 6)
            next_page = self.read("/api/events", source="edge_core/ledger", trace_id="trace-task",
                                  after=first_page["events"][0]["seq"], limit=10)
            self.assertEqual(next_page["archive_id"], first_page["archive_id"])
            self.assertEqual(next_page["events"], [rows[i] for i in (1,2,5)])
            exported = self.read("/api/export", source="edge_core/ledger", task_id="task", snapshot=1)
            self.assertEqual([r["seq"] for r in exported], [rows[i]["seq"] for i in (0,1,2,5)])
            self.assertEqual(exported[0]["snapshot"]["event"]["parent_event_id"], "original-dispatch")
            self.assertEqual(exported[0]["snapshot"]["event"]["data"], rows[0]["data"])

    def test_reused_operation_cannot_cross_either_producer_identity(self):
        with self.console() as observer:
            self.core_event("never-witnessed")
            # Start with a known closed endpoint and no previously observed Agent identity.
            with agent_facts_server(self.task_path) as url:
                observer.facts = AgentFacts(url)
            observer.poll_facts()
            self.poll_online(observer)
            self.core_event("reused")
            observer.poll_facts()
            with self.task:
                self.task.execute("UPDATE audit_identity SET id='owner-2'")
            self.dispatch("other", "reused", "new-owner-dispatch")
            self.dispatch("other", "never-witnessed", "cold-dispatch")
            self.poll_online(observer)
            self.core_event("reused", "mission.transition", 2)
            observer.poll_facts()
            with self.core:
                self.core.execute("UPDATE audit_identity SET id='core-2'")
                self.core.execute("DELETE FROM mission_events")
            self.core_event("reused")
            self.poll_online(observer)
            rows = self.events(source="edge_core/ledger", kind="mission.created")
            self.assertEqual([row["source_id"] for row in rows], ["core-1","core-1","core-2"])
            self.assertTrue(all(row["task_id"] is None for row in rows))
            for field,value in (("trace_id","trace-other"), ("task_id","other")):
                self.assertEqual(self.events(source="edge_core/ledger", **{field:value}), [])

    def test_offline_dispatch_backlog_spanning_core_replacement_is_ambiguous(self):
        with self.console() as observer:
            self.poll_online(observer)
            self.core_event("reused")
            observer.poll_facts()
            with self.task:
                self.task.executemany("""INSERT INTO events VALUES(NULL,'2025-01-01T00:00:00Z','delayed',0,
                    'progress','queued',NULL,'{}')""", [()] * 256)
            self.dispatch("delayed", "reused", "old-ledger-dispatch")
            with self.core:
                self.core.execute("UPDATE audit_identity SET id='core-2'")
                self.core.execute("DELETE FROM mission_events")
            self.core_event("reused")
            observer.poll_facts()  # See the new core while the Agent is still offline.
            self.poll_online(observer)  # The dispatch is beyond the first Agent page.
            self.assertEqual(self.events(event_id="old-ledger-dispatch"), [])
        with self.console() as observer:
            self.poll_online(observer)
            rows = self.events(source="edge_core/ledger", kind="mission.created")
            self.assertEqual([row["source_id"] for row in rows], ["core-1","core-2"])
            self.assertTrue(all(row["parent_event_id"] is None and row["task_id"] is None for row in rows))
            self.assertEqual(self.events(source="edge_core/ledger", trace_id="trace-delayed"), [])
            self.dispatch("fresh", "new-mission", "new-ledger-dispatch")
            self.core_event("new-mission")
            self.poll_online(observer)
            fresh = self.events(mission_id="new-mission")[0]
            self.assertEqual((fresh["source_id"],fresh["parent_event_id"]), ("core-2","new-ledger-dispatch"))


if __name__=="__main__":
    unittest.main()
