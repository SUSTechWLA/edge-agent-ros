"""The gap resolver may only expose a single preauthorized software service."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from client.soft_projection_evolution import selection


class GapTest(unittest.TestCase):
    def setUp(self):
        self.dep = {"type":"service","name":"test.sensing","schema_ref":"test.sensing/call@1"}
        self.base = {"test.sensor":{"provides":[self.dep],"content_digest":"original"}}
        self.projection = {"id":"test.project","content_digest":"candidate","effects":"read","kind":"tool",
            "class":"soft","runtime":"inproc","entrypoint":"agent.read_service_tool:apply","mounts":[],
            "requires":[self.dep],"provenance":{"code_digest":"code"},"network":{"ingress":[],"egress":[]},
            "config_schema":{"properties":{"profile":{"const":{"service":"test.sensing",
                "legacy":{"id":"report","effect":"read","parameters":{"target":"string"},"utterances":["找{target}"]}}}}}}
        self.task = {"state":"BLOCKED","steps":[],"text":"找纸箱","events":[{"kind":"blocked","event_id":"gap",
                    "payload":json.dumps({"diagnostic":{"code":"planner.capability_missing"}})}]}

    def propose(self, **overrides):
        item = {**copy.deepcopy(self.projection), **overrides}
        return selection(self.task, self.base, {**self.base,"test.project":item})

    def test_derives_query_and_tracks_gap(self):
        p = self.propose()
        self.assertEqual({"target":"纸箱"},p["arguments"])
        self.assertEqual(("test.sensing","gap","candidate"),(p["service"],p["gap_event"],p["candidate_digest"]))

    def test_authority_and_dependency_boundaries(self):
        for kw in ({"effects":"actuation"},{"class":"physical"},{"runtime":"container"},
                   {"entrypoint":"custom:apply"},{"mounts":["/dev"]},{"network":{"egress":["other"],"ingress":[]}},
                   {"requires":[]},{"requires":[dict(self.dep,schema_ref="different")]}):
            with self.subTest(change=kw), self.assertRaises(ValueError):
                self.propose(**kw)

    def test_dispatched_or_unrelated_gap_and_wrong_text(self):
        for change in ({"state":"FAILED"},{"steps":[{"tool":"move"}]},{"text":"移动机器人"},
                       {"events":[{"kind":"blocked","payload":json.dumps({"diagnostic":{"code":"io.timeout"}})}]}):
            orig = self.task
            self.task = {**self.task,**change}
            with self.assertRaises(ValueError):
                self.propose()
            self.task = orig


if __name__ == "__main__":
    unittest.main()
