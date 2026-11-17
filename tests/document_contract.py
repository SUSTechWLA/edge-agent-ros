"""Verify referenced repository files exist in the checkout, including Git-tracked evidence."""
import re
import json
import subprocess
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class DocumentContract(unittest.TestCase):
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
        if "sdd-003-system.json" in records:
            run=records["sdd-003-system.json"]
            self.assertRegex(run["verified_revision"],r"^[a-f0-9]{40}$")
            self.assertEqual(run["suite_status"],"passed")
            self.assertTrue(run["arm64"]["passed"])

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
