"""Readable tool milestones are a projection. A running poll can never act as terminal evidence."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from task_recorder.trace_view import milestone_key, title, tool_history


class TraceViewTest(unittest.TestCase):
    def test_repeated_progress_and_distinct_operations(self):
        def event(phase, op=1, **extra):
            return dict(kind="agent_trace",phase=phase,operation_id=f"task/step/{op}",tool="sample",**extra)
        request=event("tool.request")
        running=event("tool.response",outcome="RUNNING")
        receipt=event("tool.response",outcome="CANDIDATE")
        verified=event("verified")
        second=event("tool.request",op=2)
        self.assertEqual(tool_history([request,running,request,running,receipt,verified,second]),[request,receipt,verified,second])
        self.assertIsNone(milestone_key(running))
        self.assertIn("CANDIDATE",title(receipt)[0])
        self.assertNotEqual(milestone_key(receipt),milestone_key(verified))

    def test_failure_retains_source_reference(self):
        row=dict(kind="agent_trace",phase="tool.error",operation_id="a/step/1",module="camera",
                 source_utc="2026-11-19T00:00:00.000Z",source_seq=73,code="camera.raw_encoding")
        heading,description=title(row)
        self.assertIn("失败",heading)
        self.assertIn("camera",heading)
        for evidence in ("camera.raw_encoding","a/step/1","73",row["source_utc"]):
            self.assertIn(evidence,description)


if __name__ == "__main__":
    unittest.main()
