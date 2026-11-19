"""Admission scope and causal capture contracts without any local ROS installation."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_recorder.ros_source import Sources, task_event


class SourceTest(unittest.TestCase):
    def test_only_this_tasks_bus_events(self):
        self.assertTrue(task_event({"operation_id":"a/step/1"}, "a"))
        self.assertTrue(task_event({"trace_id":"a"}, "a"))
        self.assertFalse(task_event({"operation_id":"aa/step/1"}, "a"))
        self.assertFalse(task_event({"detail":"a"}, "a"))

    def test_partial_marker_and_scope(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"source.jsonl"
            rows=[]
            source=Sources.__new__(Sources)
            source.args=SimpleNamespace(sim=True,task_id="a",markers=path)
            source.clock=2_000_000_000
            source.sink=SimpleNamespace(emit=lambda k,t,**kw:rows.append((k,t,kw)))
            source.mark_offset=0
            path.write_bytes(b'{"task_id":"b","phase":"unrelated"}\n{"task_id":"a","phase":"mapped",')
            source.markers()
            self.assertEqual(rows,[])
            with path.open("ab") as stream:
                stream.write(b'"source_stamp_ns":1000000000,"file":"map.yaml"}\n')
            source.markers()
            source.markers()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0][1],1000000000)
            self.assertEqual(rows[0][2]["artifact"],"map.yaml")
            self.assertNotIn("file",rows[0][2])


if __name__=="__main__":
    unittest.main()
