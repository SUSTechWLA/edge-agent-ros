"""Distinguish an Ollama cold start from deployment or model identity failures."""
from pathlib import Path
from subprocess import CompletedProcess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "system"))
import model_readiness


class ModelReadiness(unittest.TestCase):
    args = (Path("compose.json"), "test-project", {}, "qwen2.5:1.5b")

    def test_refusal_then_matching_cache(self):
        refused = CompletedProcess([], 1, "", "Error: could not connect to ollama server")
        ready = CompletedProcess([], 0, "NAME ID\nqwen2.5:1.5b abcd", "")
        with patch.object(model_readiness.subprocess, "run", side_effect=[refused, ready]) as call, \
             patch.object(model_readiness.time, "sleep"):
            model_readiness.wait_for_cached_model(*self.args)
        self.assertEqual(call.call_count, 2)

    def test_unrelated_exec_failure_is_immediate(self):
        denied = CompletedProcess([], 126, "", "exec: permission denied")
        with patch.object(model_readiness.subprocess, "run", return_value=denied) as call:
            with self.assertRaisesRegex(RuntimeError, "126.*permission denied"):
                model_readiness.wait_for_cached_model(*self.args)
        self.assertEqual(call.call_count, 1)

    def test_prefix_does_not_substitute_for_exact_model(self):
        wrong = CompletedProcess([], 0, "NAME ID\nqwen2.5:1.5b-other abcd", "")
        with patch.object(model_readiness.subprocess, "run", return_value=wrong), \
             patch.object(model_readiness.time, "monotonic", side_effect=[10, 10, 11, 11]):
            with self.assertRaisesRegex(TimeoutError, "cached models.*1.5b-other"):
                model_readiness.wait_for_cached_model(*self.args, seconds=1)


if __name__ == "__main__":
    unittest.main()
