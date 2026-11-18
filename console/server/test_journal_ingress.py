"""Exercise the archive with independent producer fixtures and a controllable observer clock."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

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
        self.assertEqual(self.rows(module="journal.watchdog"),[])
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

    def test_gaps_are_durable_intervals_and_do_not_block_other_plugins(self):
        camera = self.source("camera")
        for event_id in ("first", "second", "third"):
            self.publish(camera, "camera", event_id)
        with camera:
            camera.execute("UPDATE module_bus_events SET seq=seq+100")
            camera.execute("UPDATE module_bus_events SET seq=109 WHERE seq=103")
        self.publish(self.source("power"), "power", "battery")
        self.poll()
        source = "journal:camera:first"
        rows = self.rows(trace_id=source, code="module.journal_gap", source="edge_console")
        self.assertEqual([(r["data"]["missing_from"],r["data"]["missing_to"],
                           r["data"]["next_source_seq"],r["source_seq"]) for r in rows],
                         [(1,100,101,100),(103,108,109,108)])
        self.assertTrue(all(r["module"]=="journal.ingress" and r["source_id"]==source for r in rows))
        self.assertEqual(self.history.source_cursor(source),109)
        self.assertEqual(len(self.rows(event_id="third")),1)
        self.assertEqual(len(self.rows(event_id="battery")),1)
        self.reset_reader()
        self.poll()
        self.assertEqual(self.rows(kind="module.journal_gap"),rows)

    def test_archive_failure_after_gap_retries_actual_row_without_duplicate_diagnostic(self):
        db = self.source("camera")
        self.publish(db,"camera","reply")
        with db:
            db.execute("UPDATE module_bus_events SET seq=4")
        record = self.history.record
        def fail_on_actual_row(*args, **kwargs):
            if kwargs["kind"] == "device.notice":
                raise sqlite3.OperationalError("archive is unavailable")
            return record(*args, **kwargs)
        with patch.object(self.history,"record",side_effect=fail_on_actual_row):
            with self.assertRaises(sqlite3.OperationalError):
                self.poll()
        self.assertEqual(self.history.source_cursor("journal:camera:first"),3)
        self.assertFalse(self.rows(kind="module.journal_unavailable"))
        self.history.close()
        self.open_archive("archive")
        self.reset_reader()
        self.poll()
        self.assertEqual(len(self.rows(kind="module.journal_gap")),1)
        self.assertEqual(len(self.rows(event_id="reply")),1)

    def test_journal_disappearance_and_recovery_across_console_restarts(self):
        db = self.source("camera")
        self.publish(db,"camera","first")
        self.poll()
        db.close()
        folder = self.extra / "camera"
        parked = self.root / "parked"
        folder.rename(parked)
        self.reset_reader()
        self.poll()
        self.poll()
        self.history.close()
        self.open_archive("archive")
        self.reset_reader()
        self.poll()
        parked.rename(folder)
        self.poll()
        rows = self.rows(trace_id="journal.health:camera",module="journal.ingress",source="edge_console")
        self.assertEqual([r["data"]["status"] for r in rows],["available","absent","available"])
        self.assertEqual([r["parent_event_id"] for r in rows],[None,rows[0]["event_id"],rows[1]["event_id"]])
        self.assertEqual(rows[2]["cause_seq"],rows[1]["seq"])
        self.assertEqual(len(self.rows(event_id="first")),1)

    def test_unreadable_mount_does_not_report_every_plugin_absent(self):
        self.source("camera")
        power = self.source("power")
        self.poll()
        with patch.object(Path,"iterdir",side_effect=PermissionError("private path")):
            self.poll()
        self.publish(power,"power","still-works")
        self.poll()
        rows = self.rows(kind="module.journal_unavailable")
        self.assertEqual([(r["data"]["journal"],r["data"]["status"]) for r in rows],[("@sources","unreadable")])
        self.assertNotIn("private path",json.dumps(rows))
        self.assertEqual(len(self.rows(event_id="still-works")),1)


if __name__ == "__main__":
    unittest.main()
