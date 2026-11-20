"""Rendered read receipts must use the original operation, camera bytes and capture window."""
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from PIL import Image
    from task_recorder.sensor_review import Review
except ImportError:
    Image = None


@unittest.skipUnless(Image, "install the offline render dependencies to exercise sensor review")
class SensorReviewTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.record = self.root / "recording"
        self.timeline = self.record / "media/mission/timeline.jsonl"
        self.timeline.parent.mkdir(parents=True)
        self.task = self.root / "task.json"
        self.window = self.root / "window.jsonl"
        time = 1780000000000000000
        self.time = time
        def sample(name, mode, value):
            path = self.root / (name + ".png")
            Image.new(mode, (2, 2), value).save(path)
            key = hashlib.sha256(path.read_bytes()).hexdigest()
            path.rename(self.artifacts / (key + ".png"))
            return dict(actor_ref="rover", kind="artifact", media_type="image/png", artifact_ref="sha256:" + key,
                        source_id=name, frame_id="optical", clock_id="unix", stamp_ns=str(time - 1))
        samples = [sample("depth", "I;16", 640), sample("color", "RGB", "blue")]
        evidence = {"actor_ref": "rover", "samples": samples, "basis": "published", "as_of_ns": str(time+2), "clock_id": "unix"}
        step = dict(tool="read", operation_id="test/step/1", args={}, status="SUCCEEDED", evidence=evidence)
        self.task.write_text(json.dumps(dict(id="test", text="read", state="SUCCEEDED", steps=[step])))
        self.header = self.record / "recording.json"
        self.header.write_text(json.dumps(dict(task_id="test", task_status="SUCCEEDED", status="FINISHED", missing_sources=[])))
        traces = [dict(kind="agent_request", phase="user_request", text="read"),
                  dict(kind="agent_trace", phase="tool.request", operation_id="test/step/1", tool="read", call_id="A"),
                  dict(kind="agent_trace", phase="tool.response", operation_id="test/step/1", tool="read", call_id="A", outcome="SUCCEEDED"),
                  dict(kind="agent_trace", phase="verified", operation_id="test/step/1", tool="read")]
        rows = [dict(t, seq=i, event_id=str(i), received_ns=time+i, stamp_ns=time+i, task_id="test") for i, t in enumerate(traces)]
        self.timeline.write_text("\n".join(map(json.dumps, rows)), encoding="utf8")
        self.window.write_text("\n".join(json.dumps({"received_ns": str(time+i), "samples": samples}) for i in (0, 10)))

    def review(self):
        return Review(self.task, self.record, self.artifacts, self.window)

    def test_16bit_depth_cannot_be_substituted_for_color(self):
        view = self.review()
        self.assertEqual(view.depth["source_id"], "depth")
        self.assertEqual(view.rgb["source_id"], "color")
        self.assertEqual(view.image(view.depth["artifact_ref"]).getpixel((0, 0)), 640)
        self.assertEqual(len(view.window), 2)

    def test_artifact_tamper_fails_before_render(self):
        path = next(self.artifacts.glob("*.png"))
        path.write_bytes(b"invalid")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.review()

    def test_unrelated_window_or_operation_is_rejected(self):
        text = self.window.read_text()
        self.window.write_text(text.replace(str(self.time+10), str(self.time+1)))
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.review()
        self.window.write_text(text)
        self.timeline.write_text(self.timeline.read_text().replace('"call_id": "A", "outcome"', '"call_id": "B", "outcome"'))
        with self.assertRaisesRegex(ValueError, "did not match"):
            self.review()


if __name__ == "__main__":
    unittest.main()
