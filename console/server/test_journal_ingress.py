"""Exercise the archive with independent producer fixtures and a controllable observer clock."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from history import History
from journal_ingress import JournalIngress


class JournalIngressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.extra = self.root / "sources"
        self.extra.mkdir()
        self.t = 100.0
        self.open_archive("archive")
        self.reset_reader()

    def open_archive(self, name):
        self.history = History(self.root / (name + ".db"))
        self.addCleanup(self.history.close)

    def reset_reader(self):
        self.reader = JournalIngress({}, self.extra, clock=lambda:self.t)

    def source(self, name, epoch="first"):
        path = self.extra / name / "module-events.sqlite"
        path.parent.mkdir()
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.executescript("""CREATE TABLE module_bus_meta(key,value);
            CREATE TABLE module_bus_events(seq INTEGER PRIMARY KEY,event TEXT);
            CREATE TABLE module_bus_heartbeat(singleton INTEGER PRIMARY KEY,pulse TEXT,ttl_s INTEGER);""")
        with db:
            db.execute("INSERT INTO module_bus_meta VALUES('source_id',?)",(epoch,))
        return db

    def publish(self, db, module, event_id, **fields):
        event = {"schema_version":1, "kind":"device.notice", "code":"temperature.changed",
                 "event_id":event_id, "trace_id":"mission-a", "module":module,
                 "utc":"2026-11-16T00:00:00Z", **fields}
        with db:
            db.execute("INSERT INTO module_bus_events(event) VALUES(?)",(json.dumps(event),))

    def poll(self):
        self.reader.poll(self.history,lambda at:{"session_id":"unit","source_time":at})

    def rows(self, **filters):
        return self.history.page(0,None,200,filters)["events"]

    def test_poison_does_not_stall_other_rows_or_other_sources(self):
        a, b = self.source("camera"), self.source("power")
        self.publish(a,"camera","child",parent_event_id="parent",data={"api_key":"private"})
        with a:
            a.execute("INSERT INTO module_bus_events(event) VALUES(?)",(b"\xff",))
        self.publish(a,"camera","child",data={"changed":True})
        self.publish(a,"power","spoof")
        self.publish(a,"camera","after")
        self.publish(b,"power","parent")
        self.poll()
        before = self.rows()
        self.poll()
        self.assertEqual(before,self.rows())
        self.assertEqual(len(self.rows(kind="module.event_invalid")),2)
        self.assertEqual(len(self.rows(kind="module.event_conflict")),1)
        child, = self.rows(event_id="child")
        self.assertEqual(child["cause_seq"],self.rows(event_id="parent")[0]["seq"])
        self.assertEqual(child["source"],"journal:camera")
        self.assertNotIn("private",json.dumps(before))
        self.assertEqual(self.rows(event_id="after")[0]["source_seq"],5)
        self.assertFalse(self.rows(event_id="spoof"))
        self.open_archive("rebuilt")
        self.poll()
        self.assertEqual(self.rows(event_id="child")[0]["event_id"],child["event_id"])

    def test_exact_duplicate_across_transports_retains_original_provenance(self):
        db = self.source("power")
        self.publish(db,"power","original")
        raw, = db.execute("SELECT event FROM module_bus_events").fetchone()
        from module_events import parse_event
        event, digest = parse_event(raw)
        self.history.record({"session_id":"s"},kind=event["kind"],source="ros:/edge/events",
                            detail="",trace=event,ingress_hash=digest)
        self.poll()
        self.assertEqual(len(self.rows(event_id="original")),1)
        self.assertEqual(self.rows(kind="module.event_conflict")[0]["source_seq"],1)

    def test_changed_epoch_resets_source_position(self):
        db = self.source("power")
        self.publish(db,"power","first")
        self.poll()
        with db:
            db.execute("DELETE FROM module_bus_events")
            db.execute("UPDATE module_bus_meta SET value='new'")
        self.publish(db,"power","second")
        self.poll()
        old, new = self.rows(source="journal:power")
        self.assertEqual((old["source_seq"],new["source_seq"]),(1,1))
        self.assertNotEqual(old["source_id"],new["source_id"])

    def test_optional_heartbeat_silence_after_restart_and_real_recovery(self):
        db = self.source("camera")
        self.poll()
        self.t += 100
        self.poll()
        self.assertEqual(self.rows(),[])
        with db:
            db.execute("INSERT INTO module_bus_heartbeat VALUES(1,'old',4)")
        self.poll()
        self.t += 4
        self.poll()
        self.reset_reader()
        self.poll()
        self.t += 20
        self.poll()
        overdue, = self.rows(module="journal.watchdog")
        self.assertEqual(overdue["kind"],"module.heartbeat_overdue")
        with db:
            db.execute("UPDATE module_bus_heartbeat SET pulse='new'")
        self.poll()
        recovery, = self.rows(kind="module.heartbeat_restored")
        self.assertEqual(recovery["parent_event_id"],overdue["event_id"])
        self.reset_reader()
        self.poll()
        self.t += 4
        self.poll()
        self.assertEqual(len(self.rows(kind="module.heartbeat_overdue")),2)


if __name__ == "__main__":
    unittest.main()
