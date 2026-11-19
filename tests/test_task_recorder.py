"""The archive contract is independent of ROS, Gazebo, codecs and the motion runtime."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_recorder.service import Archive,Rejected


class Recorder(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.archive=Archive(self.root)
        self.addCleanup(lambda:self.archive.close())
        self.start=dict(task_id="one",recording_id="take1",sources=["rgb","phase"])
        self.event=dict(task_id="one",kind="rgb",event_id="image1",stamp_ns=25,
                        media=dict(name="rgb/0001.png",base64=base64.b64encode(b"opaque source data").decode()))
        self.end=dict(task_id="one",status="SUCCEEDED",reason="received task terminal")

    def test_idempotent_and_scoped(self):
        a=self.archive
        s=a.start(self.start)
        self.assertEqual(s,a.start(self.start))
        a.start({**self.start,"task_id":"two","recording_id":"take2"})
        with self.assertRaises(Rejected): a.append("take2",self.event)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:a.append("take1",self.event),range(12)))
        self.assertEqual(sum(not r["duplicate"] for r in results),1)
        with self.assertRaises(Rejected): a.append("take1",{**self.event,"stamp_ns":26})
        with self.assertRaises(Rejected): a.append("take1",{**self.event,"event_id":"replacement"})
        with self.assertRaises(Rejected): a.append("take1",{**self.event,"kind":"lidar"})
        self.assertEqual(a.events("take1",0,1)[0]["event_id"],"image1")
        receipt=a.finish("take1",self.end)
        self.assertEqual(receipt["missing_sources"],["phase"])
        self.assertEqual(a.summary("take2")["status"],"ACTIVE")
        self.assertEqual(a.finish("take1",self.end),receipt)
        with self.assertRaises(Rejected): a.finish("take1",{**self.end,"status":"FAILED"})
        with self.assertRaises(Rejected): a.append("take1",{**self.event,"kind":"phase","event_id":"late"})
        media=self.root/"take1/media/mission/rgb/0001.png"
        self.assertEqual(media.read_bytes(),b"opaque source data")
        media.unlink()
        self.assertEqual(a.export("take1"),receipt)
        self.assertTrue(media.exists())

    def test_recorder_restart_and_quota(self):
        a=self.archive
        a.start(self.start)
        a.append("take1",self.event)
        a.close()
        self.archive=a=Archive(self.root,100)
        self.assertEqual(a.summary("take1")["status"],"INTERRUPTED")
        self.assertIsNone(a.export("take1")["task_status"])
        a.start({**self.start,"recording_id":"new"})
        with self.assertRaises(Rejected) as raised:
            a.append("new",self.event)
        self.assertEqual(raised.exception.status,507)
        self.assertEqual(a.export("new")["status"],"LIMIT_REACHED")
        self.assertEqual(a.events("new",0,5),[])

    def test_paths_and_source_clock(self):
        a=self.archive
        for rid in ("../other",".","recordings.sqlite"):
            with self.assertRaises(Rejected): a.start({**self.start,"recording_id":rid})
        a.start(self.start)
        with self.assertRaises(Rejected):
            a.append("take1",{**self.event,"media":{"name":"../x","base64":"YQ=="}})
        for i,t in enumerate((42,89,87)):
            a.append("take1",dict(task_id="one",kind="phase",event_id=str(i),stamp_ns=t))
        result=a.finish("take1",self.end)["stream_timing"]["phase"]
        self.assertEqual((result["max_source_gap_ns"],result["source_rewinds"]),(47,1))
        options=(self.root/"take1/media/mission/timeline.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(x)["stamp_ns"] for x in options],[42,89,87])

    def test_draining_allows_receipts_and_survives_reopen(self):
        a=self.archive
        a.start(self.start)
        a.lifecycle(True)
        self.assertEqual(a.lifecycle(),{"draining":True,"active_count":1})
        a.start(self.start)
        with self.assertRaises(Rejected) as error:
            a.start({**self.start,"recording_id":"new"})
        self.assertEqual(error.exception.status,409)
        a.append("take1",self.event)
        a.finish("take1",self.end)
        a.close()
        self.archive=a=Archive(self.root)
        self.assertEqual(a.lifecycle(),{"draining":True,"active_count":0})
        a.lifecycle(False)
        self.assertEqual(a.start({**self.start,"recording_id":"new"})["status"],"ACTIVE")


if __name__=="__main__":
    unittest.main()
