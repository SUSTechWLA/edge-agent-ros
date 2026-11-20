"""Verify online model selection using the same reader as the production Agent."""
import os
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "model_provider"))
sys.path.insert(0, str(ROOT / "agent"))
from planning import Refused, planner_profile
from select_planner import change


class ModelSelectionTest(unittest.TestCase):
    def test_ollama_thread_budget_is_optional_and_bounded(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {"EDGE_CONTRACT_DIR":temp}):
            path = Path(temp) / "planner-profile.json"
            profile = {"schema_version":1,"version":"v1","protocol":"ollama","url":"http://local","model":"qwen"}
            path.write_text(json.dumps(profile))
            self.assertNotIn("num_thread", planner_profile()["options"])
            for value in (1,3,64,0,65,True,2.5,"3"):
                path.write_text(json.dumps(dict(profile, options={"num_thread":value})))
                if type(value) is int and 1 <= value <= 64:
                    self.assertEqual(planner_profile()["options"]["num_thread"], value)
                else:
                    with self.assertRaises(Refused):
                        planner_profile()

    def test_hot_select_replay_and_restore_with_optimistic_concurrency(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {"EDGE_CONTRACT_DIR": temp,
                "EDGE_PLANNER_URL": "", "EDGE_OLLAMA_URL": "http://original:11434"}):
            directory = Path(temp)
            local = change(directory, model="qwen2.5:1.5b", expect="environment")
            replay = change(directory, model="qwen2.5:1.5b", expect=local["active"])
            self.assertFalse(replay["changed"])
            cloud = change(directory, model="deepseek-flash", expect=local["active"])
            self.assertEqual(planner_profile()["model"], "deepseek-flash")
            with self.assertRaisesRegex(ValueError, "expected revision"):
                change(directory, restore="environment", expect=local["active"])
            change(directory, restore=cloud["previous"], expect=cloud["active"])
            self.assertEqual(planner_profile()["model"], "qwen2.5:1.5b")
            change(directory, restore="environment", expect=local["active"])
            self.assertEqual(planner_profile()["url"], "http://original:11434")

    def test_invalid_credentials_url_or_modified_history_cannot_change_active_profile(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            local = change(directory, model="qwen2.5:1.5b")
            change(directory, model="deepseek-flash")
            active = (directory / "planner-profile.json").read_bytes()
            (directory / ".planner-history" / (local["active"] + ".json")).write_text("{}")
            with self.assertRaisesRegex(ValueError, "saved revision"):
                change(directory, restore=local["active"])
            with self.assertRaisesRegex(ValueError, "adapter URL"):
                change(directory, model="qwen2.5:1.5b", url="https://user:password@example.com")
            self.assertEqual((directory / "planner-profile.json").read_bytes(), active)


if __name__ == "__main__":
    unittest.main()
