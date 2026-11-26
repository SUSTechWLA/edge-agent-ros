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
from planning import Refused, feedback_plan, ollama_hybrid_thinking_model, plan, planner_profile
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

    def test_planner_and_author_share_bounded_duration_and_token_limit(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {"EDGE_CONTRACT_DIR":temp}):
            path = Path(temp) / "planner-profile.json"
            profile = {"schema_version":1,"version":"v1","protocol":"ollama","url":"http://local","model":"qwen"}
            path.write_text(json.dumps(dict(profile, timeout_s=210, options={"num_predict":240})))
            self.assertEqual(planner_profile()["timeout_s"], 210)
            self.assertEqual(planner_profile()["options"]["num_predict"], 240)
            for field, bad in (("timeout_s", True),("timeout_s",241),("timeout_s",4),("num_predict",0),("num_predict",1025)):
                options = {field:bad} if field == "timeout_s" else {"options":{field:bad}}
                path.write_text(json.dumps(dict(profile, **options)))
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

    def test_qwen3_hybrid_disables_thinking_by_default_and_honors_keep_alive(self):
        for tag in ("qwen3", "qwen3:8b", "qwen3:4b", "qwen3:8b-q4_K_M", "library/qwen3:8b"):
            self.assertTrue(ollama_hybrid_thinking_model(tag), tag)
        for non_hybrid in ("qwen3:4b-instruct", "qwen3:4b-instruct-2507", "qwen3:8b-thinking",
                           "qwen3-coder:30b", "qwen2.5:1.5b", "deepseek-flash"):
            self.assertFalse(ollama_hybrid_thinking_model(non_hybrid), non_hybrid)

        with TemporaryDirectory() as temp:
            directory = Path(temp)
            with patch.dict(os.environ, {
                "EDGE_CONTRACT_DIR": temp,
                "EDGE_PLANNER_URL": "",
                "EDGE_OLLAMA_URL": "http://127.0.0.1:11434",
                "EDGE_OLLAMA_MODEL": "qwen3:8b",
                "EDGE_OLLAMA_KEEP_ALIVE": "30m",
                "EDGE_OLLAMA_NUM_CTX": "4096",
                "EDGE_OLLAMA_NUM_PREDICT": "256",
            }):
                env_prof = planner_profile()
                self.assertIs(env_prof["think"], False)
                self.assertEqual(env_prof["keep_alive"], "30m")
                self.assertEqual(env_prof["options"]["num_predict"], 256)

                captured = []
                def fake_ollama(url, body, timeout):
                    captured.append((url, body, timeout))
                    return {"message": {"content": json.dumps({
                        "goal": "sensor_calibration",
                        "tool": "sensor_calibration_status",
                        "args": {},
                        "reason": "先核实标定状态",
                    })}}

                station_root = ROOT / "calibration/station"
                caps = {t["id"]: t for p in (station_root / "tools").glob("*.json")
                        if (t := json.loads(p.read_bytes()))}
                task = {"id": "cal-qwen3-8b", "text": "请帮我标定当前传感器", "steps": []}
                provider, steps = feedback_plan(task, caps, station_root / "feedback.json", None,
                                                model_request=fake_ollama)
                self.assertEqual(provider, "ollama/qwen3:8b")
                self.assertEqual(steps[0]["tool"], "sensor_calibration_status")
                _, body, _ = captured[0]
                self.assertIs(body["think"], False)
                self.assertEqual(body["keep_alive"], "30m")
                self.assertEqual(body["options"]["num_predict"], 256)
                self.assertEqual(body["format"]["oneOf"][0]["properties"]["reason"]["maxLength"], 160)

            change(directory, model="qwen3:8b", url="http://127.0.0.1:11434",
                   think=False, keep_alive="45m", num_ctx=4096, num_predict=320, num_thread=6)
            with patch.dict(os.environ, {"EDGE_CONTRACT_DIR": temp}):
                loaded = planner_profile()
                self.assertIs(loaded["think"], False)
                self.assertEqual(loaded["keep_alive"], "45m")
                self.assertEqual(loaded["options"]["num_predict"], 320)
                self.assertEqual(loaded["options"]["num_thread"], 6)

            for bad_env in (
                {"EDGE_OLLAMA_KEEP_ALIVE": "forever"},
                {"EDGE_OLLAMA_THINK": "maybe"},
                {"EDGE_OLLAMA_NUM_CTX": "64"},
                {"EDGE_OLLAMA_NUM_PREDICT": "9999"},
            ):
                (directory / "planner-profile.json").unlink(missing_ok=True)
                with patch.dict(os.environ, {"EDGE_CONTRACT_DIR": temp, "EDGE_PLANNER_URL": "", **bad_env}):
                    with self.assertRaises(Refused):
                        planner_profile()


if __name__ == "__main__":
    unittest.main()
