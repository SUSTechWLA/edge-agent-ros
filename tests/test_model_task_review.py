"""A model replay must independently bind model exchanges, reservations and original sensor pixels."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

try:
    from PIL import Image
    from task_recorder.model_task_review import Journal, QueryFrame, model_decision
except ModuleNotFoundError as exc:
    if exc.name != "PIL":
        raise
    Image = None


@unittest.skipUnless(Image, "Pillow is required for video evidence")
class ModelTaskReviewTest(unittest.TestCase):
    def setUp(self):
        self.task = dict(text="compare the room", steps=[])
        cap = dict(id="measure", description="read pixels", version="1", effect="read", binding="A", parameters={})
        self.step = dict(tool="measure", args={})
        self.task["steps"] = [dict(**self.step, version="1", binding="A", operation_id="t/step/1",
                           soft_cap=cap, soft_pin=dict(name="measure", binding="A", reservation_id="t/step/1"))]
        self.answer = dict(disposition="ready", steps=[self.step], reason="result")
        self.context = dict(request=self.task["text"], capabilities=[cap], authority="deployment.read_only")
        shared = dict(protocol="ollama.chat", provider="ollama/test", call_id="C")
        def artifact(value):
            return dict(content=value, sha256="x")
        self.journal = Journal.__new__(Journal)
        self.journal.events = []
        for i, (kind, data) in enumerate([
            ("planner.context", {"trace":dict(authority="deployment.read_only", artifact=artifact([cap]))}),
            ("model.request", {"trace":dict(**shared, artifact=artifact(dict(model="m",
                     messages=[dict(role="user", content=json.dumps(self.context))])))}),
            ("model.response", {"trace":dict(**shared, duration_ms=10, artifact=artifact(dict(model="m",
                     message={"content":json.dumps(self.answer)})))}),
            ("planner.selected", {"trace":dict(artifact=artifact(self.answer))}),
            ("planner.frozen", {"trace":dict(steps=[dict(tool="measure", binding="A", operation_id="t/step/1")])}),
            ("planned", {"steps":self.task["steps"]})
        ]):
            self.journal.events.append((dict(phase=kind, source_seq=i, stamp_ns=i), data))

    def test_retains_public_model_choices_and_original_catalog(self):
        choice = model_decision(self.journal, self.task)
        self.assertEqual(choice["decisions"], [dict(tool="measure", args={})])
        self.assertEqual(choice["offered"][0]["effect"], "read")
        self.assertNotIn("reason", choice)

    def test_rejects_unrelated_response_and_different_reservation(self):
        self.journal.events[2][1]["trace"]["call_id"] = "different"
        with self.assertRaisesRegex(ValueError, "unique response"):
            model_decision(self.journal, self.task)
        self.journal.events[2][1]["trace"]["call_id"] = "C"
        self.journal.events[4][1]["trace"]["steps"][0]["operation_id"] = "other"
        with self.assertRaisesRegex(ValueError, "reservation"):
            model_decision(self.journal, self.task)

    def test_rejects_changed_request_even_if_the_model_choice_is_plausible(self):
        self.task["text"] = "something else"
        with self.assertRaisesRegex(ValueError, "different user request"):
            model_decision(self.journal, self.task)

    def test_motion_pin_is_refused_even_when_the_model_catalog_says_read(self):
        self.task["steps"][0]["physical_pin"] = {"class":"physical"}
        with self.assertRaisesRegex(ValueError, "read-only DSH dispatch"):
            model_decision(self.journal, self.task)

    def test_sensor_report_must_reference_the_same_original_files_and_regions(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            refs = {}
            for name in ("rgb", "depth", "calibration"):
                file = root / ("new.json" if name == "calibration" else "new.png")
                if name == "calibration":
                    file.write_text(json.dumps(dict(frame_id="cam", width=2, height=2)))
                else:
                    Image.new("RGB" if name == "rgb" else "I;16", (2, 2)).save(file)
                digest = hashlib.sha256(file.read_bytes()).hexdigest()
                refs[name] = "sha256:" + digest
                file.rename(root / (digest + file.suffix))
            samples = [dict(source_id=name, kind="artifact", stamp_ns=9, clock_id="unix", actor_ref="r",
                            frame_id="cam", artifact_ref=ref, calibration_ref=refs["calibration"])
                       for name, ref in refs.items()]
            program = "depth | coverage"
            row = dict(bounds_xyxy=[0,0,2,2], pixels=4, valid_pixels=0, value=0.)
            a = dict(width=2, height=2, program=program, grid=[1,1], channel="depth", statistic="coverage",
                     depth_scale_m=.001, depth_scale_ref="scale", minimum_indices=[0], maximum_indices=[0], unit="%",
                     frame_id="cam", actor_ref="r", clock_id="unix", regions=[row], **{k + "_ref":v for k,v in refs.items()},
                     **{k + "_source_id":k for k in refs}, **{k + "_stamp_ns":9 for k in refs})
            receipt = SimpleNamespace(batch=dict(analysis=a, observations=dict(actor_ref="r", basis="published",
                          clock_id="unix", as_of_ns=10, samples=samples)), events=[{},{"stamp_ns":9},{"stamp_ns":11}])
            self.assertEqual(QueryFrame(receipt, root, {"program":program}).rgb.size, (2, 2))
            row["pixels"] = 8
            with self.assertRaisesRegex(ValueError, "region geometry"):
                QueryFrame(receipt, root, {"program":program})
            row["pixels"] = 4
            row["value"] = 2.
            with self.assertRaisesRegex(ValueError, "archived pixels"):
                QueryFrame(receipt, root, {"program":program})
            row["value"] = 0.
            samples[-1]["frame_id"] = "unrelated"
            with self.assertRaisesRegex(ValueError, "analysis input"):
                QueryFrame(receipt, root, {"program":program})
