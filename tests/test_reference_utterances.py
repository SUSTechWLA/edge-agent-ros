"""Two-reference read tools in the local planner, including third-party v1 registrations."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import planning


class ReferenceUtterances(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        environment = patch.dict(os.environ, {"EDGE_PLUGIN_DIR": self.temp.name, "EDGE_EXTRA_PLUGIN_DIR": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.cap = {"id": "estimate", "version": "1", "description": "试算路线", "effect": "read",
                    "parameters": {"destination_ref": "string", "actor_ref": "string"},
                    "utterances": ["为 {actor_ref} 试算到 {destination_ref} 的路线",
                                   "观测[{destination_ref}] + [{actor_ref}]"],
                    "endpoint": "http://fixture/read"}

    def install(self, cap):
        (self.dir / (cap["id"] + ".json")).write_text(json.dumps(cap), encoding="utf-8")
        return planning.registry()

    def test_generic_read_plugin_reaches_normal_planning_entry(self):
        caps = self.install(self.cap)
        for text in ("为 rover.1 试算到 office/4-a 的路线", "观测[office/4-a] + [rover.1]！"):
            with self.subTest(text=text):
                provider, steps = planning.plan(text, caps)
                self.assertEqual(provider, "local")
                self.assertEqual([{"destination_ref": "office/4-a", "actor_ref": "rover.1"}],
                                 [s["args"] for s in steps])
                self.assertEqual(steps[0]["binding"], planning.binding(caps["estimate"]))
        self.assertIsNone(planning.local_plan("观测Xoffice/4-aX + [rover.1]", caps))
        for actor in ("", "r" * 129, "robot 2", "a}"):
            self.assertIsNone(planning.local_plan(f"为 {actor} 试算到 office 的路线", caps))

    def test_reject_non_read_ambiguous_or_malformed_two_slot_patterns(self):
        for cap in [
            {**self.cap, "effect": "idempotent"},
            {**self.cap, "verbatim_argument": True},
            {**self.cap, "parameters": {"actor_ref": "number", "destination_ref": "string"}},
            *[{**self.cap, "utterances": [pattern]} for pattern in
              ("{actor_ref}{destination_ref}", "{actor_ref}_{destination_ref}", "{actor_ref}和{actor_ref}",
               "{actor_ref}和{unknown_ref}", "{actor_ref}和{destination_ref}}")],
        ]:
            with self.subTest(cap=cap), patch("builtins.print"):
                self.assertNotIn(cap["id"], self.install(cap))
        # Projected v2 metadata does not pass through the v1 directory parser.
        denied = {**self.cap, "effect": "idempotent"}
        self.assertIsNone(planning.local_plan("为 rover 试算到 office 的路线", {"estimate": denied}))
        caps = self.install(self.cap)
        caps["another_read"] = {**self.cap, "id": "another_read"}
        with self.assertRaises(planning.Refused):
            planning.local_plan("为 rover 试算到 office 的路线", caps)


if __name__ == "__main__":
    unittest.main()
