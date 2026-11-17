"""SDD-007 producer commits, per-plugin observation and restart boundaries."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from module_journal import ModuleJournal
import runtime_plugins as runtime


def facts(db):
    return [json.loads(row[0]) for row in db.execute("SELECT event FROM module_bus_events ORDER BY seq")]


class JournalContract(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "owned" / "module-events.sqlite"
        self.journal = ModuleJournal(self.path)
        self.addCleanup(self.journal.close)
        self.event = {"module":"sensor", "trace_id":"trace-1", "kind":"reading", "code":"sensor.ready"}

    def test_atomic_commit_rollback_and_restart(self):
        self.journal.db.execute("CREATE TABLE state(value TEXT)")
        with self.assertRaises(RuntimeError), self.journal.db:
            self.journal.db.execute("INSERT INTO state VALUES('bad')")
            self.journal.emit(self.event, key="decision")
            raise RuntimeError("inject crash before commit")
        self.assertEqual(facts(self.journal.db), [])
        self.assertEqual(self.journal.db.execute("SELECT * FROM state").fetchall(), [])
        with self.journal.db:
            event_id = self.journal.emit(self.event, key="decision")
        with closing(ModuleJournal(self.path)) as reopened, reopened.db:
            self.assertEqual(reopened.source_id, self.journal.source_id)
            self.assertEqual(reopened.emit(self.event, key="decision"), event_id)
            self.assertEqual(len(facts(reopened.db)), 1)
            self.assertEqual(reopened.db.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_conflicting_intent_wire_redaction_and_explicit_id(self):
        event = {**self.event, "data":{"api_key":"dummy-private"}, "detail":"Bearer dummyprivate"}
        with self.journal.db:
            first = self.journal.emit(event,key="key")
            again = self.journal.emit(event,key="key")
            self.assertEqual(first, again)
            with self.assertRaises(ValueError):
                self.journal.emit({**event,"code":"sensor.broken"}, key="key")
            with self.assertRaises(ValueError):
                self.journal.emit({**event,"event_id":"another"}, key="key")
            envelope = {**self.event, "utc":"2020-01-01T10:00:00Z", "event_id":"from-source"}
            self.assertEqual(self.journal.emit(envelope), self.journal.emit(envelope))
            with self.assertRaises(ValueError):
                self.journal.emit({**envelope, "utc":"2020-01-02T10:00:00Z"})
        rows = facts(self.journal.db)
        self.assertEqual(len(rows), 2)
        self.assertNotIn("dummy", json.dumps(rows))
        with self.assertRaises(ValueError), self.journal.db:
            self.journal.emit({**event, "data":{"blob":"x"*65536}})

    def test_uncommitted_pulse_is_invisible_and_refresh_is_not_an_event(self):
        pulse = "SELECT pulse,ttl_s FROM module_bus_heartbeat"
        with closing(sqlite3.connect(self.path)) as reader:
            with self.journal.db:
                self.journal.heartbeat(2)
                self.assertIsNone(reader.execute(pulse).fetchone())
            first = reader.execute(pulse).fetchone()
            with self.journal.db:
                self.journal.heartbeat(3600)
            second = reader.execute(pulse).fetchone()
            self.assertNotEqual(first[0], second[0])
            self.assertEqual((first[1], second[1]), (2,3600))
            for invalid in (True, 1, 3601, 3.5):
                with self.assertRaises(ValueError):
                    self.journal.heartbeat(invalid)
            self.assertEqual(reader.execute(pulse).fetchall(), [second])
            self.assertEqual(facts(reader), [])


class RegistryContract(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = self.root / "state" / "module-events.sqlite"
        self.directory = self.root / "runtime"
        self.directory.mkdir()
        env = patch.dict(os.environ, {"EDGE_PLUGIN_DIR":str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.observer = self.open()

    def open(self):
        observer = runtime.RegistryObserver(self.store)
        self.addCleanup(lambda: observer.journal and observer.journal.close())
        return observer

    def install(self, **changes):
        path = self.directory / "camera.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema_version":1, "id":"camera", "version":"1",
                                   "provides":["verify.camera"],"requires":[],
                                   "endpoint":"http://private.invalid/verify", **changes}), encoding="utf-8")
        tmp.replace(path)
        return path

    def events(self):
        with closing(sqlite3.connect(self.store)) as reader:
            return facts(reader)

    def test_baseline_transitions_aba_and_parent_history_survive_restart(self):
        path = self.install()
        self.observer.observe()
        self.install(draining=True)
        self.observer.observe()
        self.install(version="2")
        self.observer.observe()
        path.write_text("invalid, private!", encoding="utf-8")
        self.observer.observe()
        self.install()
        restarted = self.open()
        restarted.observe()
        restarted.observe()
        path.unlink()
        restarted.observe()
        self.install()
        restarted.observe()
        events = self.events()
        self.assertEqual([e["data"]["after"] and e["data"]["after"]["status"] for e in events],
                         ["ready","draining","ready","invalid","ready",None,"ready"])
        self.assertEqual([e["data"]["baseline"] for e in events], [True]+[False]*6)
        self.assertEqual([e["parent_event_id"] for e in events], [None]+[e["event_id"] for e in events[:-1]])
        self.assertEqual([e["data"]["before"] for e in events], [None]+[e["data"]["after"] for e in events[:-1]])
        self.assertEqual(len({e["trace_id"] for e in events}), 1)
        self.assertEqual(len({e["event_id"] for e in events}), len(events))
        self.assertEqual(events[0]["data"]["after"], events[-1]["data"]["after"])
        self.assertNotIn("private", json.dumps(events))
        self.assertTrue(all(e["module"] == "runtime.registry" and "task_id" not in e for e in events))

    def test_unavailable_mount_and_directory_error_preserve_prior_state(self):
        self.install()
        self.observer.observe()
        self.directory.rename(self.root / "parked")
        for _ in range(2):
            with self.assertRaises(runtime.PluginUnavailable):
                self.observer.observe()
        with patch.object(runtime, "snapshot", side_effect=PermissionError("private/path")):
            with self.assertRaises(runtime.PluginUnavailable):
                self.observer.observe()
        (self.root / "parked").rename(self.directory)
        self.observer.observe()
        events = self.events()
        self.assertEqual([e["kind"] for e in events],
                         ["plugin.registry_observed","plugin.registry_observation_failed",
                          "plugin.registry_observation_failed","plugin.registry_observation_restored"])
        self.assertEqual(events[-1]["parent_event_id"],events[-2]["event_id"])
        self.assertNotIn("private",json.dumps(events))

    def test_failed_transition_transaction_can_be_retried(self):
        self.install()
        self.observer.observe()
        self.install(version="2")
        db = self.observer.journal.db
        db.execute("""CREATE TRIGGER deny BEFORE INSERT ON registry_projection
                      BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        with self.assertRaises(runtime.PluginUnavailable):
            self.observer.observe()
        self.assertEqual(len(self.events()), 1)
        db.execute("DROP TRIGGER deny")
        self.observer.observe()
        self.assertEqual(self.events()[-1]["data"]["before"]["version"],"1")
        self.assertEqual(self.events()[-1]["data"]["after"]["version"],"2")

    def test_background_works_without_requests_and_get_does_not_renew(self):
        stop = threading.Event()
        thread = threading.Thread(target=self.observer.run, args=(stop,))
        self.install()
        thread.start()
        try:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not self.observer.journal:
                stop.wait(.02)
            self.assertIsNotNone(self.observer.journal)
            self.observer.observe()  # lock waits for the background transaction to finish
            with closing(sqlite3.connect(self.store)) as reader:
                pulse = "SELECT pulse FROM module_bus_heartbeat"
                first = reader.execute(pulse).fetchone()
                self.assertIsNotNone(first)
                self.observer.observe()
                self.assertEqual(reader.execute(pulse).fetchone(), first)
                self.install(version="2")
                while time.monotonic() < deadline and len(facts(reader)) < 2:
                    stop.wait(.02)
                self.assertEqual(facts(reader)[-1]["data"]["after"]["version"],"2")
        finally:
            stop.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
