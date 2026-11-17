import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from history import History
from main import Handler

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"client"))
from event_sink import Sink


class SinkTest(unittest.TestCase):
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
