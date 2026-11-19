"""Black-box incremental map observation during capture; no ROS, cameras or writable API dependencies."""
import base64
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_recorder.service import Archive, Handler


class LiveExploration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.archive = Archive(temporary.name)
        self.addCleanup(self.archive.close)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.server.archive = self.archive
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval":.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/recordings"

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(self.url+path, data=data), timeout=5) as response:
            return response.read() if response.headers.get_content_type()=="text/html" else json.load(response)

    def start(self, rid):
        self.request("/start", dict(recording_id=rid, task_id=rid, sources=["map"]))

    def event(self, rid, i, cells, x=0, frame="odom", payload=None):
        grid = dict(frame=frame, width=2, height=1, resolution=1.0, origin=[x, 0, 0], cells=cells)
        content = json.dumps(grid).encode() if payload is None else payload
        obj = dict(task_id=rid, kind="map", event_id=f"grid-{i}", stamp_ns=i,
                   media=dict(name=f"grid/{i}.json", base64=base64.b64encode(content).decode()))
        return self.request(f"/{rid}/events",obj)["seq"]

    def test_task_isolation_progress_segments_and_sealed_replay(self):
        self.start("rover")
        self.start("second")
        self.assertIsNone(self.request("/rover/global-map")["grid"])
        self.assertIn("探索地图".encode(),self.request("/rover/map-view"))
        self.event("rover",1,[0,-1])
        first = self.request("/rover/global-map")
        self.assertEqual(first["grid"]["cells"], [0,-1])
        self.assertEqual(first["latest"]["new_cells"],1)
        self.event("second",2,[100,100])
        self.event("rover",3,[50,100],1)
        new = self.request("/rover/global-map")
        self.assertEqual(new["grid"]["cells"],[0,50,100])
        self.event("rover",4,[100,-1],0,"other")
        end=self.request("/rover/global-map")
        self.assertEqual(len(end["segments"]),2)
        self.assertEqual(end["grid"]["cells"],[100,-1])
        self.assertEqual(self.request("/rover/global-map?segment=1")["grid"],new["grid"])
        self.assertEqual(self.request("/second/global-map")["grid"]["cells"],[100,100])
        self.request("/rover/finish",dict(task_id="rover",status="COMPLETE"))
        self.archive.live_maps=None  # reconstruct from committed evidence
        replay=self.request("/rover/global-map?segment=1")
        self.assertEqual((replay["status"], replay["grid"]),("FINISHED",new["grid"]))
        for path in ("/absent/global-map","/rover/global-map?segment=0","/rover/global-map?segment=3",
                     "/rover/global-map?segment=1&segment=2","/rover/global-map?folder=second"):
            with self.subTest(path=path),self.assertRaises(HTTPError):
                self.request(path)

    def test_bad_attachment_does_not_hide_following_facts_or_finish_task(self):
        self.start("test")
        self.event("test",1,[0,0])
        bad=self.event("test",2,[],payload=b"not json")
        response=self.request("/test/global-map")
        self.assertEqual((response["grid"]["cells"],response["faults"],response["last_fault"]["seq"]),([0,0],1,bad))
        corrupted=self.event("test",3,[99,-1])
        with self.archive.lock,self.archive.db:
            self.archive.db.execute("UPDATE events SET blob=? WHERE seq=?", (b"{}",corrupted))
        last=self.event("test",4,[-1,100])
        response=self.request("/test/global-map")
        self.assertEqual((response["cursor"],response["accepted_seq"],response["faults"]),(last,last,2))
        self.assertEqual((response["last_fault"]["detail"],response["grid"]["cells"]),("map.attachment_digest",[0,100]))
        self.assertEqual(self.request("/test")["status"],"ACTIVE")

    def test_declared_rejection_preserves_geometry_and_next_measurement(self):
        self.start("sensor")
        self.event("sensor",1,[0,-1])
        before=self.request("/sensor/global-map")
        self.request("/sensor/events",dict(task_id="sensor",event_id="tilt",kind="map",stamp_ns=2,
                                         valid=False,error="map.invalid_geometry",detail="map plane tilted"))
        rejected=self.request("/sensor/global-map")
        self.assertEqual(rejected["last_fault"]["detail"],"map.invalid_geometry")
        self.assertEqual(rejected["grid"],before["grid"])
        self.event("sensor",3,[-1,100])
        self.assertEqual(self.request("/sensor/global-map")["grid"]["cells"],[0,100])

    def test_unchanged_map_uses_no_body_until_next_fact_or_closure(self):
        self.start("idle")
        self.event("idle",1,[100,0])
        addr=self.url+"/idle/global-map"
        with urlopen(addr,timeout=4) as page:
            etag=page.headers["ETag"]
        conditional=Request(addr,headers={"If-None-Match":etag})
        with self.assertRaises(HTTPError) as unchanged:
            urlopen(conditional,timeout=4)
        self.assertEqual((unchanged.exception.code,unchanged.exception.read()),(304,b""))
        self.request("/idle/finish",dict(task_id="idle",status="COMPLETE"))
        with urlopen(conditional,timeout=4) as closed:
            self.assertNotEqual(closed.headers["ETag"],etag)
            self.assertEqual(json.load(closed)["status"],"FINISHED")

    def test_large_backlog_has_an_explicit_cursor(self):
        self.start("busy")
        for i in range(1,12):
            self.event("busy",i,[i,-1])
        first=self.request("/busy/global-map")
        self.assertTrue(first["catching_up"])
        second=self.request("/busy/global-map")
        self.assertFalse(second["catching_up"])
        self.assertGreater(second["cursor"],first["cursor"])
        self.assertEqual(second["grid"]["cells"],[11,-1])


if __name__ == "__main__":
    unittest.main()
