"""A direct-task recording binds the submitted text, authorization and measured range."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import hashlib
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from PIL import Image
    from task_recorder.direct_target_demo import client_exchange, source_stages, target_image, verified_range
except ModuleNotFoundError as error:
    if error.name != "PIL":
        raise
    source_stages = None


@unittest.skipUnless(source_stages, "Pillow is required for recorder review")
class DirectTaskDemoTest(unittest.TestCase):
    def setUp(self):
        self.submission = {
            "task_id": "sample",
            "request_text": "找到右侧绿植",
            "sent_ns": "100",
            "acknowledged_ns": "140",
            "http_status": 202,
        }
        self.view = SimpleNamespace(
            task={"id": "sample", "trigger": {"kind": "user"}, "text": self.submission["request_text"]},
            events=[
                {"kind": "agent_request", "stamp_ns": 120},
                {"stamp_ns": 180},
                {"stamp_ns": 250},
                {"stamp_ns": 270},
            ],
            tool="measure",
            op="sample/step/1",
            obs={"as_of_ns": 200},
            record=SimpleNamespace(
                by={
                    "agent_trace": [
                        {"phase": "planned", "stamp_ns": 150},
                        {
                            "phase": "dispatch",
                            "tool": "measure",
                            "stamp_ns": 175,
                            "operation_id": "sample/step/1",
                        },
                    ]
                }
            ),
        )

    def test_follows_original_direct_request_on_one_edge_clock(self):
        self.assertEqual(source_stages(self.view, self.submission), [100, 180, 200, 250, 270])

    def test_client_exchange_is_bound_to_the_original_http_bytes(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            task = self.view.task
            header = dict(self.submission, method="POST", path="/agent/tasks")
            body = {"id": task["id"], "text": task["text"]}
            for name, key, value in (
                ("submit-body.json", "request_body_sha256", body),
                ("submit-response.json", "response_body_sha256", {**body, "state": "PLANNING"}),
            ):
                raw = json.dumps(value).encode()
                (folder / name).write_bytes(raw)
                header[key] = hashlib.sha256(raw).hexdigest()
            (folder / "submit.json").write_text(json.dumps(header), encoding="utf8")
            self.assertEqual(client_exchange(folder, task)["http_status"], 202)
            path = folder / "submit-body.json"
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "HTTP exchange"):
                client_exchange(folder, task)

    def test_rejects_edited_text_late_client_issue_and_continuations(self):
        for change in (
            {"request_text": "不同的目标"},
            {"task_id": "other"},
            {"sent_ns": 121},
            {"http_status": 400},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                source_stages(self.view, {**self.submission, **change})
        self.view.task["trigger"] = {"kind": "agent"}
        with self.assertRaisesRegex(ValueError, "directly submitted"):
            source_stages(self.view, self.submission)

    def test_distinguishes_surface_range_from_axial_depth(self):
        self.view.xyz = [0.3, 0, 0.4]
        self.view.g = {"range_m": 0.5, "axial_depth_m": 0.4}
        self.assertAlmostEqual(verified_range(self.view), 0.5)
        self.view.g["range_m"] = 0.4
        with self.assertRaisesRegex(ValueError, "distance mismatch"):
            verified_range(self.view)

    def test_instance_box_must_belong_to_the_measured_pixels(self):
        self.view.rgb = Image.new("RGB", (32, 24))
        self.view.pt = (14, 12)
        self.view.bbox = (12, 10, 17, 15)
        self.view.rows = {12: [(14, 15)]}
        self.view.mask = {"stride": 1}
        identity = {"rgb_ref": "frame", "bbox_xyxy": [4, 3, 28, 20]}
        self.view.g = {"rgb_ref": "frame", "detection": identity}
        self.assertEqual(target_image(self.view).size, (32, 24))
        for change in ({"rgb_ref": "another frame"}, {"bbox_xyxy": [1, 1, 4, 4]}):
            self.view.g["detection"] = {**identity, **change}
            with self.assertRaises(ValueError):
                target_image(self.view)


if __name__ == "__main__":
    unittest.main()
