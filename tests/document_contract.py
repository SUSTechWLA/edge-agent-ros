"""Verify referenced repository files exist in the checkout, including Git-tracked evidence."""
import re
import json
import subprocess
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class DocumentContract(unittest.TestCase):
    def test_run_outputs_and_published_verification_files(self):
        def tracked(directory):
            raw = subprocess.check_output(["git","ls-files","-z","--",directory],cwd=ROOT)
            return [path for path in raw.decode().split("\0") if path]

        def ignored(path):
            result = subprocess.run(["git","check-ignore","--no-index","-q","--",path],cwd=ROOT)
            self.assertIn(result.returncode,(0,1))
            return result.returncode == 0

        root = "artifacts/verification/"
        self.assertEqual(tracked("docs/evidence/verification"),[])
        self.assertEqual(tracked(".ci-results"),[])
        published = tracked("artifacts")
        self.assertIn(root + "README.md",published)
        for path in published:
            self.assertTrue(path.startswith(root) and not ignored(path),path)
        for path in (root+"new-run/result.json",root+"new-run/trace.ndjson",
                     root+"20261117-031532-ae63c8/private.log","artifacts/images/export.tar",
                     ".ci-results/system/runs/new-run/summary.json"):
            self.assertTrue(ignored(path),path)

    def test_curated_evidence_has_a_real_result(self):
        evidence = ROOT/"docs/evidence"
        records = {path.name:json.loads(path.read_text(encoding="utf-8")) for path in evidence.glob("*.json")}
        scene = records["semantic-sample.json"]
        self.assertTrue(scene["passed"])
        self.assertEqual(len(scene["tasks"]),7)
        for task in scene["tasks"]:
            if "world_error_m" in task:
                self.assertGreaterEqual(task["world_error_m"],0)
                self.assertLess(task["world_error_m"],scene["tolerances"]["goal_error_m"])
        self.assertEqual(scene["outage_recovery"]["archive_rows_after_reconnect"],1)
        model = records["trace-sample.json"]
        self.assertTrue(model["passed"])
        self.assertEqual(model["root_task"],model["duplicate_task"])
        self.assertEqual(model["exported_event_count"],len(model["indexed_events"]))
        for record in records.values():
            self.assertIsInstance(record,dict)
        plugins = records["runtime-plugins-sample.json"]
        self.assertRegex(plugins["verified_revision"],r"^[a-f0-9]{40}$")
        self.assertEqual(plugins["suite"]["status"],"passed")
        self.assertEqual(len(plugins["suite"]["stages"]),18)
        measured = plugins["blackbox"]
        self.assertTrue(measured["passed"])
        self.assertEqual(len(measured["cases"]),measured["state_snapshots"])
        self.assertEqual(len({row["task_id"] for row in measured["cases"].values()}),measured["task_count"])
        self.assertEqual(len(measured["calls_by_operation"]),measured["task_count"])
        self.assertEqual(set(measured["calls_by_operation"].values()),{1})
        self.assertEqual(measured["cases"]["baseline-uri"]["state"],"BLOCKED")
        self.assertEqual(measured["cases"]["crash-window"]["state"],"SUCCEEDED")
        self.assertEqual(measured["crash_window_verification_calls"],{"image-check":1,"review":2})
        self.assertEqual(measured["service_resolution"]["drain"]["new_capture_count_before_reopen"],0)
        for board in plugins["board_fixtures"].values():
            self.assertTrue(board["passed"])
            self.assertEqual(board["revision"],plugins["verified_revision"])
        run = records["sdd-003-system.json"]
        self.assertRegex(run["verified_revision"], r"^[a-f0-9]{40}$")
        self.assertEqual(run["suite_status"], "passed")
        suite = run["suite"]
        self.assertEqual(len(suite["passed_stage_duration_s"]), suite["test_stage_count"])
        self.assertEqual(suite["passed_stage_count"], suite["test_stage_count"])
        for arch in ("amd64", "arm64"):
            self.assertTrue(run[arch]["passed"])
            self.assertEqual(run[arch]["revision"], run["verified_revision"])
        matrix = run["resource_matrix"]
        self.assertEqual(matrix["measured_cases"], len(matrix["runs"]))
        self.assertEqual(matrix["passed_count"], sum(row["passed"] for row in matrix["runs"]))
        self.assertEqual(matrix["oom_count"], sum(row["oom_kill_events"] > 0 for row in matrix["runs"]))

        for sample in records["lifecycle-sample.json"]["runs"]:
            self.assertTrue(sample["passed"])
            self.assertEqual(sum(sample["tool_counts"].values()), sample["unique_tasks"])
            self.assertEqual(sample["terminal_verified_events"], sample["unique_tasks"])
            self.assertEqual(sample["faults"]["persistent_outage"]["second_retry_http"], 409)

    def test_local_links_can_survive_a_clean_checkout(self):
        raw = subprocess.check_output(["git","ls-files","--cached","--others","--exclude-standard","-z"],cwd=ROOT)
        files = {name for name in raw.decode().split("\0") if name}
        missing = []
        for name in sorted(f for f in files if f.endswith(".md")):
            source = ROOT / name
            if not source.is_file():
                continue
            prose = re.sub(r"^```.*?^```[^\n]*", "", source.read_text(encoding="utf-8-sig"), flags=re.M|re.S)
            for link in re.findall(r"\]\(([^\s)]+)\)", prose):
                target = urlsplit(link)
                if target.scheme or not target.path or target.netloc:
                    continue
                path = (source.parent / unquote(target.path)).resolve()
                if not path.is_relative_to(ROOT) or not (path.is_file() and path.relative_to(ROOT).as_posix() in files
                        or path.is_dir() and any(f.startswith(path.relative_to(ROOT).as_posix()+"/") for f in files)):
                    missing.append(f"{name} -> {link}")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
