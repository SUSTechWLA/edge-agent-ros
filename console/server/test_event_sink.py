import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from history import History
from journal_ingress import JournalIngress
from main import Handler

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"client"))
from event_sink import Sink, SinkError
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"agent"))
from module_journal import ModuleJournal


def event_page(rows, cursor, *, archive="fixture", pruned=0, more=False):
    return {"archive_id":archive,"events":rows,"cursor":cursor,"pruned_through_seq":pruned,"has_more":more}


def source_row(seq, **extra):
    return {"seq":seq,"source":"journal:input","kind":"test.sample","event_id":f"E{seq}",**extra}


class IntegrityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base=Path(tmp.name)

    def open(self,filters=None):
        sink=Sink(self.base/"sink.db","http://unused",filters or {})
        self.addCleanup(sink.close)
        return sink

    def poll(self,sink,page):
        discovery=event_page([],0,archive=page.get("archive_id","fixture"))
        with patch.object(sink,"page",side_effect=lambda after,limit: discovery if limit==1 else page):
            return sink.copy()

    def test_malformed_page_never_advances_even_if_first_row_is_valid(self):
        sink=self.open({"kind":"test.sample"})
        self.poll(sink,event_page([source_row(1)],1))
        for page in (event_page([source_row(2),source_row(1)],2),event_page([source_row(2),source_row(2)],2),
                     event_page([source_row(2),source_row(3,kind="other")],3),
                     event_page([source_row(2),source_row(4)],3),event_page([],2,more=True),
                     event_page([],2**63),event_page([],True),event_page([],2,pruned=3),
                     event_page([],0),event_page([],2,archive="private?access_token=x"),
                     event_page([source_row(2),{"seq":3}],3),event_page([],2,more=0)):
            with self.subTest(page=page),self.assertRaises(SinkError):
                self.poll(sink,page)
            self.assertEqual(sink.db.execute("SELECT seq FROM events").fetchall(),[(1,)])
            self.assertEqual(sink.db.execute("SELECT cursor FROM checkpoints").fetchall(),[(1,)])
        self.assertNotIn("access_token",str(sink.db.execute("SELECT data FROM subscription_notices").fetchall()))

    def test_conflicting_position_and_deferred_parent_resolution(self):
        child = source_row(3,parent_event_id="E5",cause_seq=None,detail="private-input")
        all_rows=self.open()
        self.poll(all_rows,event_page([child],3))
        selected=self.open({"kind":"test.sample"})
        with self.assertRaisesRegex(SinkError,"event_conflict"):
            self.poll(selected,event_page([source_row(1),dict(child,event_id="different")],3))
        self.assertFalse(selected.db.execute("SELECT 1 FROM events WHERE seq=1").fetchone())
        self.assertFalse(selected.db.execute("SELECT 1 FROM checkpoints WHERE selector=?",(selected.key,)).fetchone())
        with_cause=dict(child,cause_seq=5)
        self.poll(selected,event_page([with_cause],5))
        copy=json.loads(all_rows.db.execute("SELECT event FROM events WHERE seq=3").fetchone()[0])
        self.assertEqual(copy,with_cause)
        self.assertEqual(all_rows.db.execute("SELECT count(*) FROM events").fetchone()[0],1)
        another=self.open({"source":"journal:input"})
        self.poll(another,event_page([with_cause],5))  # exact replay in a third selector
        again=self.open({"source":"journal:input","kind":"test.sample"})
        with self.assertRaisesRegex(SinkError,"event_conflict"):
            self.poll(again,event_page([dict(with_cause,cause_seq=6)],6))

    def test_parent_must_be_a_distinct_position_within_archive_watermark(self):
        sink = self.open()
        self.poll(sink,event_page([source_row(1)],1))
        for cause in (3,5):
            page = event_page([source_row(2),source_row(3,parent_event_id="E4",cause_seq=cause)],4)
            with self.subTest(cause=cause),self.assertRaisesRegex(SinkError,"invalid_row"):
                self.poll(sink,page)
            self.assertEqual(sink.db.execute("SELECT seq FROM events").fetchall(),[(1,)])
            self.assertEqual(sink.db.execute("SELECT cursor FROM checkpoints").fetchone()[0],1)
        # The parent may arrive later than the child in a distributed system.
        self.poll(sink,event_page([source_row(3,parent_event_id="E4",cause_seq=4),source_row(4)],4))
        self.assertEqual(sink.db.execute("SELECT seq FROM events ORDER BY seq").fetchall(),[(1,),(3,),(4,)])

    def test_durable_block_recovery_retention_and_publish_ack_replay(self):
        sink=self.open()
        journal=ModuleJournal(self.base/"module-events.sqlite")
        self.addCleanup(journal.close)
        self.poll(sink,event_page([],0))
        for _ in range(2):
            with self.assertRaisesRegex(SinkError,"invalid_page"):
                self.poll(sink,event_page([],3,more=True))
        self.assertEqual(sink.db.execute("SELECT count(*) FROM subscription_notices").fetchone()[0],1)
        sink.db.execute("""CREATE TRIGGER fail_ack BEFORE UPDATE OF delivered ON subscription_notices
                   BEGIN SELECT RAISE(ABORT,'simulated restart after publish'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            sink.bind_journal(journal,"test.consumer")
        self.assertEqual(journal.db.execute("SELECT count(*) FROM module_bus_events").fetchone()[0],1)
        sink.db.execute("DROP TRIGGER fail_ack")
        sink.close()
        sink=self.open()
        sink.bind_journal(journal,"test.consumer")
        self.poll(sink,event_page([source_row(3)],3,pruned=2))
        self.poll(sink,event_page([],3,pruned=2))
        events=[json.loads(raw) for (raw,) in journal.db.execute("SELECT event FROM module_bus_events ORDER BY seq")]
        self.assertEqual([e["kind"] for e in events],["subscription.blocked","subscription.retention","subscription.restored"])
        self.assertEqual(events[-1]["parent_event_id"],events[0]["event_id"])
        self.assertEqual(events[-1]["trace_id"],events[0]["trace_id"])
        self.assertEqual(sink.db.execute("SELECT count(*) FROM subscription_notices WHERE delivered=0").fetchone()[0],0)
        self.assertTrue(self.poll(sink,event_page([],0,archive="new")))
        self.assertFalse(self.poll(sink,event_page([],0,archive="new")))
        self.assertEqual(sink.db.execute("SELECT count(*) FROM subscription_notices WHERE kind='subscription.archive_changed'").fetchone()[0],1)

    def test_legacy_input_database_keeps_checkpoint_and_checks_prior_journal_ids(self):
        path=self.base/"sink.db"
        with closing(sqlite3.connect(path)) as old, old:
            old.executescript("""CREATE TABLE events(archive_id TEXT,seq INTEGER,event TEXT NOT NULL,PRIMARY KEY(archive_id,seq));
                CREATE TABLE checkpoints(archive_id TEXT,selector TEXT,cursor INTEGER NOT NULL,PRIMARY KEY(archive_id,selector));""")
            old.execute("INSERT INTO events VALUES('fixture',1,?)",(json.dumps(source_row(1)),))
            old.execute("INSERT INTO checkpoints VALUES('fixture','{}',1)")
        sink=self.open()
        with self.assertRaisesRegex(SinkError,"event_conflict"):
            self.poll(sink,event_page([source_row(2,event_id="E1")],2))
        self.assertEqual(sink.db.execute("SELECT cursor FROM checkpoints").fetchone(),(1,))
        self.poll(sink,event_page([source_row(2)],2))
        self.assertEqual(sink.db.execute("SELECT cursor FROM checkpoints").fetchone(),(2,))

    def test_prune_final_telemetry_during_source_restart_preserves_watermark(self):
        path=self.base/"archive.db"
        source=History(path)
        source.record({"session_id":"s"},source="edge_console",kind="telemetry.sample",detail="")
        with source.db:
            source.db.execute("UPDATE events SET observed_at=1")
        source.close()
        with patch.dict(os.environ,{"EDGE_TELEMETRY_RETENTION_DAYS":"1"}):
            source=History(path)
        self.addCleanup(source.close)
        page=source.page(0,None,1,{})
        self.assertEqual((page["events"],page["cursor"],page["pruned_through_seq"]),([],1,1))


class SinkTest(unittest.TestCase):
    def test_corrupt_http_page_is_reported_to_the_public_archive_then_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            extensions=base/"sources"
            with closing(History(base/"source.db")) as history, closing(ModuleJournal(
                    extensions/"test.consumer"/"module-events.sqlite")) as journal:
                frame=lambda _:{"session_id":"test"}
                history.record(frame(None),kind="test.sample",source="fixture",detail="trusted sample")
                invalid=True
                class Source(Handler):
                    def do_GET(self):
                        if invalid and "kind=test.sample" in self.path:
                            raw=b'{"archive_id":"bogus","events":"private-invalid-input"'
                            self.api_headers(200,"application/json",len(raw))
                            self.wfile.write(raw)
                            return
                        super().do_GET()
                server=ThreadingHTTPServer(("127.0.0.1",0),partial(Source,observer=SimpleNamespace(history=history)))
                thread=threading.Thread(target=server.serve_forever,daemon=True)
                thread.start()
                try:
                    url=f"http://127.0.0.1:{server.server_port}"
                    selectors={"kind":"test.sample"}
                    def consume():
                        with closing(Sink(base/"mirror.db",url,selectors)) as sink:
                            sink.bind_journal(journal,"test.consumer")
                            return sink.copy()
                    for _ in range(2):
                        with self.assertRaisesRegex(SinkError,"subscription.invalid_json"):
                            consume()
                    ingress=JournalIngress({},extensions)
                    ingress.poll(history,frame)
                    def public():
                        with urlopen(url+"/api/events?after=0&source=journal%3Atest.consumer",timeout=2) as reply:
                            return json.load(reply)["events"]
                    blocked,=public()
                    self.assertEqual(blocked["kind"],"subscription.blocked")
                    self.assertEqual(blocked["data"]["reason"],"subscription.invalid_json")
                    self.assertNotIn("private-invalid-input",json.dumps(blocked))
                    self.assertNotIn("127.0.0.1",json.dumps(blocked))
                    invalid=False
                    self.assertFalse(consume())
                    ingress.poll(history,frame)
                    blocked,restored=public()
                    self.assertEqual((restored["kind"],restored["parent_event_id"],restored["cause_seq"]),
                                     ("subscription.restored",blocked["event_id"],blocked["seq"]))
                    with closing(sqlite3.connect(base/"mirror.db")) as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0],1)
                        self.assertEqual(db.execute("SELECT count(*) FROM checkpoints").fetchone()[0],1)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()

    def test_cut_http_body_after_first_page_recovers_without_skipping_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = History(base / "source.db")
            self.addCleanup(store.close)
            for _ in range(40):
                store.record({"session_id":"run"}, source="sensor", kind="sample", detail="")
            attempts = []
            class CutPage(Handler):
                def do_GET(self):
                    cursor = parse_qs(urlsplit(self.path).query).get("after", ["0"])[0]
                    if cursor == "32" and cursor not in attempts:
                        attempts.append(cursor)
                        self.api_headers(200, "application/json", 64)
                        self.wfile.write(b'{"events":')
                        self.close_connection = True
                        return
                    attempts.append(cursor)
                    super().do_GET()

            server = ThreadingHTTPServer(("127.0.0.1",0), partial(CutPage, observer=SimpleNamespace(history=store)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            dest = base / "sink.db"
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[2]/"client/event_sink.py"),
                                        "--url", f"http://127.0.0.1:{server.server_port}", "--db",str(dest)],
                                       stderr=subprocess.PIPE, text=True)
            try:
                until = time.monotonic() + 10
                while time.monotonic() < until and process.poll() is None:
                    if "32" in attempts and dest.exists():
                        with sqlite3.connect(dest) as db:
                            count = db.execute("SELECT count(*) FROM events").fetchone()[0]
                            if count == 40:
                                self.assertEqual(db.execute("SELECT cursor FROM checkpoints").fetchone()[0], 40)
                                break
                    time.sleep(.05)
                else:
                    self.fail(f"sink stopped or timed out after request cursors {attempts}")
                self.assertGreaterEqual(attempts.count("32"), 2)
            finally:
                process.terminate()
                process.communicate(timeout=5)
                server.shutdown()
                server.server_close()
                thread.join()

    def test_http_restart_filter_epoch_and_atomic_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{"EDGE_TELEMETRY_MAX_SAMPLES":"1"}):
            root=Path(tmp)
            observer=SimpleNamespace(history=History(root/"first.db"))
            http=ThreadingHTTPServer(("127.0.0.1",0),partial(Handler,observer=observer))
            thread=threading.Thread(target=http.serve_forever,daemon=True)
            thread.start()
            url=f"http://127.0.0.1:{http.server_port}"
            sink=Sink(root/"sink.db",url,{"module":"power.monitor"})
            first=observer.history
            def record(module):
                return observer.history.record({"session_id":"session"},source="driver",detail="temperature",
                                               kind="sensor.update",trace={"module":module})
            try:
                for _ in range(35):
                    record("power.monitor")
                record("another.module")
                for _ in range(3):
                    first.record({"session_id":"s"},source="edge_console",kind="telemetry.sample",detail="")
                self.assertTrue(sink.copy())
                self.assertEqual(sink.db.execute("SELECT cursor FROM checkpoints").fetchone()[0],32)
                sink.close()
                sink=Sink(root/"sink.db",url,{"module":"power.monitor"})
                sink.db.execute("""CREATE TRIGGER injected_fail BEFORE UPDATE ON checkpoints
                    BEGIN SELECT RAISE(ABORT,'forced sink failure'); END""")
                with self.assertRaises(sqlite3.IntegrityError):
                    sink.copy()
                self.assertEqual(sink.db.execute("SELECT count(*) FROM events").fetchone()[0],32)
                self.assertEqual(len(first.page(0,None,500,{"module":"power.monitor"})["events"]),35)
                sink.db.execute("DROP TRIGGER injected_fail")
                self.assertFalse(sink.copy())
                self.assertFalse(sink.copy())
                self.assertEqual(sink.db.execute("SELECT count(*) FROM events").fetchone()[0],35)
                self.assertGreater(sink.db.execute("SELECT count(*) FROM retention_notices").fetchone()[0],0)
                observer.history=History(root/"second.db")
                record("power.monitor")
                self.assertTrue(sink.copy())
                self.assertFalse(sink.copy())
                archives=sink.db.execute("SELECT archive_id,count(*) FROM events GROUP BY archive_id").fetchall()
                self.assertEqual(sorted(n for _,n in archives),[1,35])
                print("read-only HTTP sink: restart, rollback, 2 archive IDs, 36 unique events")
            finally:
                sink.close()
                http.shutdown()
                http.server_close()
                thread.join()
                if observer.history is not first:
                    observer.history.close()
                first.close()
