"""SDD-015 supervisor detection, independent recovery, durable replay and source evidence."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"client"))
from watchdog import ProbeError, Reader, Watchdog


class Sources:
    def __init__(self):
        self.bad = {}
        self.calls = []
        self.rows = []
        self.archive = "a"
        self.cursor = self.pruned = 0
        self.page_error = None

    def __call__(self, target, reconcile=False, operation=None):
        self.calls.append((target,reconcile,operation))
        if (target,reconcile) in self.bad:
            raise ProbeError(self.bad[target,reconcile])
        if target=="agent":
            return {"ready":1} if reconcile else {"capability_count":2}
        return {"archive_id":self.archive,"cursor":self.cursor,"pruned_through_seq":self.pruned,"count":len(self.rows)}

    def page(self, after, cutoff, operation):
        matches = [v for v in self.rows if after < v["seq"] <= cutoff]
        if self.page_error:
            raise ProbeError(self.page_error)
        return {"archive_id":self.archive,"cursor":self.cursor,"pruned_through_seq":self.pruned,
                "events":matches[:16],"has_more":len(matches)>16}

    def add(self, count):
        for _ in range(count):
            self.cursor += 1
            self.rows.append({"seq":self.cursor,"source":"mission_agent/tasks","kind":"agent.blocked",
                              "event_id":"err"+str(self.cursor),"trace_id":"t","task_id":"t","code":"planner.missing"})


class TestWatchdog(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/"watchdog"/"module-events.sqlite"
        self.now = 0
        self.sources = Sources()
        self.open()
        self.addCleanup(lambda: self.w.close())

    def open(self):
        self.w = Watchdog(self.path,self.sources,threshold=2,grace=5,attempts=2,recheck=3,
                          clock=lambda:self.now)

    def events(self,kind):
        return [json.loads(r[0]) for r in self.w.db.execute("SELECT event FROM module_bus_events ORDER BY seq")
                if json.loads(r[0])["kind"]=="watchdog."+kind]

    def test_grace_debounce_both_targets_duplicate_suppression(self):
        self.sources.bad = {(k,False):"connection_unavailable" for k in ("agent","console")}
        self.w.tick()
        self.now=6
        self.w.tick()
        self.sources.bad.clear()
        self.w.tick()
        self.assertEqual(self.events("detected"),[])
        self.sources.bad = {(k,False):"timeout" for k in ("agent","console")}
        for _ in range(12):
            self.w.tick()
        self.assertEqual({r["data"]["target"] for r in self.events("detected")},{"agent","console"})
        self.assertEqual(len(self.events("intervention_required")),2)
        self.assertEqual(len(self.events("action_attempt")),4)
        self.assertTrue(all(r["data"]["reason"] and r["data"]["steps"] for r in self.events("intervention_required")))
        self.sources.bad.pop(("agent",False))
        self.w.tick()
        self.assertEqual([r["data"]["target"] for r in self.events("recovered")],["agent"])
        self.assertEqual(self.events("recovered")[0]["parent_event_id"],self.events("action_confirmed")[0]["event_id"])
        with self.assertRaises(sqlite3.OperationalError):
            Watchdog(self.path,self.sources)

    def test_reconcile_replays_window_in_pages_across_restart(self):
        self.w.tick()
        self.now=6
        self.sources.bad["console",False]="timeout"
        self.w.tick()
        self.w.tick()
        self.w.tick()
        self.sources.add(24)
        self.sources.bad.clear()
        self.w.tick()
        self.assertEqual(len(self.events("recovered")),0)
        self.assertEqual(self.w.db.execute("SELECT seq FROM watchdog_cursor").fetchone(),(16,))
        self.w.close()
        self.open()
        self.w.tick()
        row=self.events("action_confirmed")[0]
        self.assertEqual(row["data"]["repair"]["rows_copied"],24)
        self.assertEqual(row["data"]["repair"]["through_seq"],24)
        self.assertEqual(row["data"]["repair"]["from_seq"],0)
        self.assertEqual(row["operation_id"],self.events("action_requested")[0]["operation_id"])
        self.w.tick()
        self.assertEqual(self.w.db.execute("SELECT count(*),count(DISTINCT event_id) FROM watchdog_events").fetchone(),(24,24))
        self.assertEqual(len(self.events("detected")),1)

    def test_reboot_after_intent_repeats_same_read_only_operation(self):
        self.w.tick()
        self.now=6
        self.sources.bad["agent",False]="timeout"
        self.w.tick()
        class Crash(BaseException):
            pass
        original=self.w.reader
        def removed(target,reconcile=False,operation=None):
            if reconcile and target=="agent":
                raise Crash
            return original(target,reconcile,operation)
        removed.page=original.page
        self.w.reader=removed
        with self.assertRaises(Crash):
            self.w.tick()
        op=self.events("action_attempt")[0]["operation_id"]
        self.w.close()
        self.open()
        self.sources.bad.clear()
        self.w.tick()
        self.assertEqual(self.events("action_confirmed")[0]["operation_id"],op)
        self.assertEqual(len(self.events("action_attempt")),1)

    def test_retention_archive_change_and_bad_pages(self):
        self.w.tick()
        self.now=6
        self.sources.add(3)
        self.sources.pruned=1
        for _ in range(3):
            self.w.tick()
        self.assertTrue(self.events("action_confirmed")[0]["data"]["repair"]["retention_crossed"])
        self.assertEqual(self.events("intervention_required")[0]["data"]["reason"],"retention_crossed")
        self.sources.archive="b"
        self.sources.pruned=0
        self.sources.rows=[]
        self.sources.cursor=1
        for _ in range(3):
            self.w.tick()
        self.assertTrue(self.events("action_confirmed")[-1]["data"]["repair"]["archive_changed"])
        self.sources.cursor=0
        for _ in range(3):
            self.w.tick()
        self.assertEqual(self.events("intervention_required")[-1]["data"]["reason"],"cursor_regressed")
        self.sources.cursor=2
        self.sources.rows=[{"seq":2,"source":"untrusted"}]
        self.w.tick()
        self.assertEqual(self.w.db.execute("SELECT seq FROM watchdog_cursor").fetchone(),(1,))
        self.assertEqual(self.events("action_unavailable")[-1]["data"]["reason"],"mirror_invalid_page")

    def test_http_body_limits_and_explicit_no_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            mode="redirect"
            def log_message(self,*_):
                pass
            def do_GET(self):
                if self.mode=="redirect":
                    self.send_response(302)
                    self.send_header("Location","http://127.0.0.1:1")
                    self.end_headers()
                    return
                body={"oversize":b"x"*262150,"malformed":b"not json","schema":b'{"capabilities":{}}',
                      "overflow":b'{"archive_id":"a","cursor":9223372036854775808,"count":1,"pruned_through_seq":0}',
                      "partial":b"abc","slow":b"x"}[self.mode]
                self.send_response(200)
                self.send_header("Content-Length",str(len(body)+(3 if self.mode=="partial" else 0)))
                self.end_headers()
                if self.mode=="slow":
                    time.sleep(.35)
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
        with ThreadingHTTPServer(("127.0.0.1",0),Handler) as server:
            thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.05})
            thread.start()
            try:
                url="http://127.0.0.1:"+str(server.server_port)
                reader=Reader({"agent":url,"console":url},.15)
                for mode,reason in (("redirect","http_302"),("oversize","reply_too_large"),("malformed","invalid_reply"),
                                     ("schema","invalid_reply"),("overflow","invalid_reply"),("partial","incomplete_reply"),
                                     ("slow","timeout|incomplete_reply|connection_unavailable")):
                    with self.subTest(mode=mode),self.assertRaisesRegex(ProbeError,reason):
                        Handler.mode=mode
                        reader("console" if mode=="overflow" else "agent",False)
            finally:
                server.shutdown()
                thread.join()

    def test_restoring_old_archive_reuses_exact_records_and_rejects_changed_evidence(self):
        self.w.tick()
        self.now=6
        self.sources.add(1)
        self.w.tick()
        for identity in ("b","a"):
            self.sources.archive=identity
            for _ in range(3):
                self.w.tick()
        repair=self.events("action_confirmed")[-1]["data"]["repair"]
        self.assertEqual((repair["archive_id"],repair["rows_copied"],repair["rows_reused"]),("a",0,1))
        self.assertEqual(self.w.db.execute("SELECT count(*) FROM watchdog_events").fetchone(),(2,))
        self.sources.archive="b"
        self.sources.rows[0]["code"]="altered"
        for _ in range(4):
            self.w.tick()
        self.assertEqual(self.events("action_unavailable")[-1]["data"]["reason"],"mirror_event_conflict")
        self.assertEqual(self.w.db.execute("SELECT archive_id,seq FROM watchdog_cursor").fetchone(),("a",1))


if __name__=="__main__":
    unittest.main()
