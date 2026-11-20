"""Archive only a completed operation's own fresh, scoped, hash-matching observations."""
from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_recorder import spatial_capture as capture


class SpatialCaptureArchiveTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "camera" / "artifacts"
        self.source.mkdir(parents=True)
        self.actor, self.sid, self.tid = "rover", "rgb", "capture-test"
        self.now = 1_795_000_000_000_000_179
        self.original = self.publish(b"original sensor observation")
        self.batch = dict(schema_version=1, basis="published", actor_ref=self.actor, clock_id="unix",
                          as_of_ns=str(self.now), samples=[self.original])
        self.task = dict(id=self.tid, state="SUCCEEDED", steps=[
            dict(tool="read_spatial_observations", args={"actor_ref": self.actor}, status="SUCCEEDED",
                 operation_id=self.tid + "/step/1", evidence=self.batch)])

    def publish(self, data):
        digest = hashlib.sha256(data).hexdigest()
        (self.source / (digest + ".png")).write_bytes(data)
        return dict(kind="artifact", artifact_ref="sha256:" + digest, source_id=self.sid, frame_id="camera",
                    media_type="image/png", actor_ref=self.actor, clock_id="unix", calibration_ref="cal.ref",
                    stamp_ns=str(self.now - 5))

    def samples(self, task, tool="read_spatial_observations"):
        return capture.receipt_samples(task, self.tid, self.actor, {self.sid}, tool)

    def test_accepts_original_and_nested_perception_receipts(self):
        self.assertEqual(self.samples(self.task), [self.original])
        for tool, args in (("perceive_stationary", {"actor_ref": self.actor}), ("mark_target", {"target": "right-side plane"})):
            with self.subTest(tool=tool):
                task = deepcopy(self.task)
                task["steps"][0].update(tool=tool, args=args,
                    evidence={"actor_ref": "decoy", "samples": [], "observations": self.batch})
                self.assertEqual(self.samples(task, tool), [self.original])
                # A valid outer object cannot rescue a substituted nested batch.
                task["steps"][0]["evidence"] = {**self.batch, "observations": {**self.batch, "samples": []}}
                with self.assertRaises(ValueError):
                    self.samples(task, tool)

    def test_rejects_false_operations_and_mismatched_scope(self):
        changes = [
            lambda t: t.update(id="different-task"),
            lambda t: t.update(state="BLOCKED"),
            lambda t: t["steps"].append(deepcopy(t["steps"][0])),
            lambda t: t["steps"][0].update(status="FAILED"),
            lambda t: t["steps"][0].update(operation_id="different-task/step/1"),
            lambda t: t["steps"][0].update(tool="mark_target"),
            lambda t: t["steps"][0]["args"].update(actor_ref="other"),
            lambda t: t["steps"][0]["evidence"].update(actor_ref="other"),
            lambda t: t["steps"][0]["evidence"].update(basis="replay"),
            lambda t: t["steps"][0]["evidence"]["samples"][0].update(actor_ref="other"),
            lambda t: t["steps"][0]["evidence"]["samples"][0].update(source_id="somewhere-else")]
        for change in changes:
            task = deepcopy(self.task)
            change(task)
            with self.subTest(task=task), self.assertRaises(ValueError):
                self.samples(task)

    def test_int64_time_budget_is_inclusive_and_rejects_future(self):
        # The interval bounds apply identically to dynamically chosen tools.
        self.original["stamp_ns"] = str(self.now - 1_000_000_000)
        self.assertEqual(self.samples(self.task), [self.original])
        for stamp in (self.now - 1_000_000_001, self.now + 1):
            self.original["stamp_ns"] = str(stamp)
            with self.subTest(stamp=stamp), self.assertRaisesRegex(ValueError, "one-second"):
                self.samples(self.task)

    def test_model_selected_steps_require_frozen_read_bindings(self):
        step = self.task["steps"][0]
        pin = "sha256:"+"a"*64
        step.update(binding=pin, soft_cap={"effect":"read"}, soft_pin={"name":step["tool"],"binding":pin})
        self.task["steps"].append({**step, "operation_id":self.tid+"/step/2"})
        self.assertEqual(self.samples(self.task, None), [self.original, self.original])
        self.task["steps"][1]["binding"] = "sha256:"+"b"*64
        with self.assertRaisesRegex(ValueError, "frozen"):
            self.samples(self.task, None)

    def test_gateway_response_is_size_limited_and_bound_to_requested_actor(self):
        def request(wire):
            with patch.object(capture, "urlopen", return_value=BytesIO(wire)) as http:
                result = capture.fetch("http://localhost/observations", self.actor)
                self.assertEqual(json.loads(http.call_args.args[0].data), {"actor_ref": self.actor})
                return result

        batch = {**self.batch, "as_of_ns": self.now, "samples": [{**self.original, "stamp_ns": self.now - 5}]}
        self.assertEqual(request(json.dumps(batch).encode()), batch)
        other_robot = {**batch, "actor_ref": "dog", "samples": [{**batch["samples"][0], "actor_ref": "dog"}]}
        for invalid in (other_robot, {**batch, "basis": "replay"}):
            with self.subTest(batch=invalid), self.assertRaisesRegex(ValueError, "binding"):
                request(json.dumps(invalid).encode())
        with self.assertRaisesRegex(ValueError, "too large"):
            request(b" " * 65537)

    def test_rejects_unknown_source_digest_and_quota_without_partial_artifact(self):
        archive = capture.Archive(self.root / "copy", {self.sid: self.source.parent})
        for change in (dict(source_id="other"), dict(artifact_ref="../path"), dict(media_type="text/html")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                archive.copy({**self.original, **change})
        with patch.object(capture, "FILE_LIMIT", 4), self.assertRaises(ValueError):
            archive.copy(self.original)
        with patch.object(capture, "MAX_BYTES", 4), self.assertRaises(ValueError):
            archive.copy(self.original)
        filename = self.original["artifact_ref"][7:] + ".png"
        (self.source / filename).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "digest"):
            archive.copy(self.original)
        self.assertEqual(list(archive.root.iterdir()), [])
        self.assertEqual((archive.total, archive.saved), (0, set()))

    def test_archive_pins_task_pixels_even_if_live_feed_has_advanced(self):
        current = self.publish(b"newer observation from same live source")
        out = self.root / "result"
        out.mkdir()
        (out / "task.json").write_text(json.dumps(self.task), encoding="utf8")
        options = SimpleNamespace(out=out, source=[(self.sid, self.source.parent)], required=[self.sid],
                    actor=self.actor, task=self.tid, tool="read_spatial_observations", lead=.1,
                    tail=.2, timeout=1., url="http://test.invalid/observations")
        # A deterministic clock allows one newer gateway batch after the original task receipt.
        with patch.object(capture.time, "monotonic", side_effect=[0., 0., 0., .05, .22, .22]), \
             patch.object(capture.time, "sleep"), \
             patch.object(capture, "fetch", return_value={**self.batch, "samples": [current]}):
            result = capture.record(options)
        self.assertEqual((result["batches"], result["artifact_files"]), (1, 2))
        for sample, data in ((self.original, b"original sensor observation"), (current, b"newer observation from same live source")):
            self.assertEqual((out / "artifacts" / (sample["artifact_ref"][7:] + ".png")).read_bytes(), data)
        self.assertEqual(json.loads((out / "capture.json").read_bytes()), result)
        self.assertEqual(json.loads((out / "sensor-timeline.jsonl").read_text())["samples"], [current])


if __name__ == "__main__":
    unittest.main()
