import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from history import History
from main import Handler

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"client"))
from event_sink import Sink


class SinkTest(unittest.TestCase):
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
