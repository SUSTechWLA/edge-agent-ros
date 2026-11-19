"""Fault-inject the frozen-mission scorer without needing a running Gateway."""
from pathlib import Path
import json
import math
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "system"))
import plugin_tracks_metrics as metrics


class FrozenMissionMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.first = "first/step/1/waypoint/1"
        self.second = "second/step/1/waypoint/1"
        self.restart = {"operation_id": self.first, "gateway_starts":[self.line(self.first)],
                        "truth_at_kill":{"x":1, "y":0, "v":0.3, "w":0.0}}
        self.resilience = {"passed":True, "cases":{"restart":self.restart},
                           "gateway_start_witness":{"scope":"private_stack_lifetime",
                                                   "lines":[self.line(self.first), self.line(self.second)]}}
        self.places = {"passed":True, "run_id":"sample", "tasks":[]}
        for key, spec in metrics.read(metrics.CORPUS)["missions"].items():
            xy = spec.get("goal", [0, 0])
            pose = {"xy":xy, "stamp_s":5, "v":0, "w":0}
            self.places["tasks"].append({"id":"sample-"+key, "text":spec["text"], "state":spec["state"],
                "truth":[pose, dict(pose, stamp_s=6)], "steps":[{"tool":spec["tool"],
                "evidence":{"x":xy[0], "y":xy[1]}}]})
        self.faults = {"passed":True, "cases":{"replacement_core_ledger":{"state":"RECOVERY_REQUIRED"}}}

    @staticmethod
    def line(operation):
        return f"gateway-1 | [edge_gateway]: Tracking {operation} to (2.600, 0.000)"

    def measure(self, **kwargs):
        for name, data in (("places",self.places), ("resilience",self.resilience), ("agent-faults",self.faults)):
            (self.directory / (name + ".json")).write_text(json.dumps(data), encoding="utf-8")
        return metrics.measure(self.directory, **kwargs)

    def test_full_witness_covers_both_operations(self):
        result = self.measure()
        self.assertTrue(result["passed"])
        self.assertEqual(result["all_gateway_operation_ids"], [self.first, self.second])
        self.assertEqual(result["matching_cases"], 7)

    def test_unexpected_replacement_duplicate_or_missing_start_fails(self):
        lines = self.resilience["gateway_start_witness"]["lines"]
        for operations in ([self.first, "fresh-op", self.second], [self.first, self.second, self.second],
                           [self.first], [self.second, self.first]):
            with self.subTest(operations=operations):
                lines[:] = [self.line(key) for key in operations]
                result = self.measure()
                self.assertFalse(result["passed"])
                self.assertEqual(result["unexpected_operation_ids"], ["fresh-op"] if "fresh-op" in operations else [])
                self.assertEqual(result["duplicate_motion_starts"], len(operations) - len(set(operations)))

    def test_legacy_witness_requires_explicit_contract(self):
        del self.resilience["gateway_start_witness"]
        self.assertFalse(self.measure()["passed"])
        historical = self.measure(allow_legacy=True)
        self.assertTrue(historical["passed"])
        self.assertEqual(historical["comparison_strength"], "original_operation_only")
        self.assertEqual(historical["motion_operations"], 1)

    def test_bad_pose_cannot_supply_stationarity_or_success(self):
        for task in ("unknown", "garden"):
            row = next(item for item in self.places["tasks"] if item["id"] == "sample-" + task)
            previous = row["truth"][1]
            for invalid in (math.nan, math.inf, True, "0", None):
                with self.subTest(task=task, invalid=invalid):
                    row["truth"][1] = dict(previous, xy=[invalid, 0])
                    result = self.measure()
                    self.assertFalse(result["passed"])
                    self.assertEqual(result["invalid_world_samples"], 1)
            row["truth"][1] = previous

    def test_independent_source_failure_and_kill_witness_required(self):
        self.faults["passed"] = False
        self.assertFalse(self.measure()["passed"])
        self.faults["passed"] = True
        self.restart["truth_at_kill"]["v"] = math.nan
        self.assertFalse(self.measure()["passed"])


if __name__ == "__main__":
    unittest.main()
