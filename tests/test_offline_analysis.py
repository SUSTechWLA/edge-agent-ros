"""Read public task exports on a workstation with only the analysis package."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis import analyze

SAMPLES = ROOT / "analysis/examples"
FILES = {"task": "task.json", "console": "events.ndjson", "evidence": "evidence.json", "recording": "recording.json"}


class OfflineAnalysisTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)

    def cli(self, *args):
        env = {**os.environ, "PYTHONPATH": "", "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, "-B", "-S", "-m", "analysis", *args], cwd=self.work,
                              env=env, capture_output=True, text=True, encoding="utf8", timeout=10)

    def install_package(self):
        module = self.work / "analysis"
        module.mkdir()
        for name in ("__init__.py", "__main__.py"):
            shutil.copyfile(ROOT / "analysis" / name, module / name)

    def test_detached_cli_reproduces_outputs_without_changing_inputs(self):
        self.install_package()
        args, inputs = [], {}
        for name, filename in FILES.items():
            path = self.work / filename
            shutil.copyfile(SAMPLES / filename, path)
            inputs[name] = path.read_bytes()
            args.extend(["--" + name, filename])
        result = self.cli(*args, "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report, json.loads((SAMPLES / "report.json").read_bytes()))
        self.assertEqual(report["task"]["state"], "SUCCEEDED")
        self.assertTrue(report["task"]["events_has_more"])
        self.assertEqual(report["console"]["by_level"], {"info": 1, "warn": 1})
        self.assertEqual(report["console"]["explicit_owner_matches"], 2)
        self.assertEqual(report["evidence"]["loaded_events"], 2)
        self.assertEqual(report["evidence"]["returned_rows"], 1)
        self.assertEqual(report["recording"]["reported_task_status"], "STOPPED")
        self.assertEqual(report["recording"]["missing_sources"], ["rgb"])
        summary = self.cli(*args)
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assertEqual(summary.stdout, (SAMPLES / "summary.txt").read_text("utf8"))
        for name, filename in FILES.items():
            self.assertEqual(report[name]["input_sha256"], sha256(inputs[name]).hexdigest())
            self.assertEqual((self.work / filename).read_bytes(), inputs[name])

    def test_missing_owner_empty_export_and_failed_observation(self):
        console = self.work / "empty.ndjson"
        console.touch()
        query = self.work / "blocked.json"
        query.write_text(json.dumps({"operation_id": "q", "state": "BLOCKED",
                                     "error": {"code": "evidence.source_unavailable"}}), "utf8")
        report = analyze(console=console, evidence=query, recording=SAMPLES / FILES["recording"])
        self.assertIsNone(report["task"])
        self.assertEqual(report["console"]["events"], 0)
        self.assertIsNone(report["console"]["first_seq"])
        self.assertIsNone(report["console"]["explicit_owner_matches"])
        self.assertEqual(report["evidence"]["error_code"], "evidence.source_unavailable")
        self.assertEqual(report["recording"]["reported_task_status"], "STOPPED")
        self.assertIsNone(analyze(evidence=SAMPLES / "evidence.json")["console"])
        with self.assertRaisesRegex(ValueError, "at least one"):
            analyze()

    def test_legacy_unattributed_and_other_task_events_are_counted_separately(self):
        console = self.work / "events.ndjson"
        events = [json.loads(line) for line in (SAMPLES / FILES["console"]).read_bytes().splitlines()]
        events[0].pop("task_id")
        events[1].update(seq=7, task_id="another-task")
        console.write_bytes(b"\r\n".join(json.dumps(e).encode() for e in events))
        result = analyze(task=SAMPLES / "task.json", console=console)["console"]
        self.assertEqual((result["events"], result["first_seq"], result["last_seq"]), (2, 1, 7))
        self.assertEqual((result["explicit_owner_matches"], result["without_task_id"]), (0, 1))
        self.assertEqual(result["input_sha256"], sha256(console.read_bytes()).hexdigest())

    def test_truncated_evidence_preserves_limits_and_rejects_mixed_task_ids(self):
        data = json.loads((SAMPLES / "evidence.json").read_bytes())
        data["evidence"]["truncated"] = True
        data["evidence"]["coverage"].update(has_more=True, retention_gap_possible=True)
        evidence = self.work / "evidence.json"
        evidence.write_text(json.dumps(data), encoding="utf8")
        report = analyze(evidence=evidence)["evidence"]
        self.assertTrue(report["has_more"] and report["truncated"] and report["retention_gap_possible"])
        different = json.loads((SAMPLES / "task.json").read_bytes())
        different["id"] = "other"
        task = self.work / "other-task.json"
        task.write_text(json.dumps(different), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "different tasks"):
            analyze(task=task, evidence=evidence)

    def test_bad_input_identifies_file_and_ndjson_line_without_partial_output(self):
        self.install_package()
        path = self.work / "bad.ndjson"
        first = (SAMPLES / FILES["console"]).read_bytes().splitlines()[0]
        for bad in (b"not json", b"[]", b"\xff", first):
            with self.subTest(bad=bad[:10]):
                original = first + b"\n" + bad
                path.write_bytes(original)
                result = self.cli("--console", path.name, "--format", "json")
                self.assertEqual(result.returncode, 2)
                self.assertIn(path.name, result.stderr)
                self.assertIn("line 2", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(path.read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "recording.*does-not-exist"):
            analyze(recording=self.work / "does-not-exist")
        with self.assertRaisesRegex(ValueError, "evidence.*state"):
            analyze(evidence=SAMPLES / "recording.json")


if __name__ == "__main__":
    unittest.main()
