"""The Agent retries invalid proposals within a bounded catalog; invalid graphs are never dispatched."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.read_graph_composition import compose, validate_graph
from edge_contracts.errors import Refused
from perception.review055.definitions import GRAPH_LANGUAGE


def review_graph():
    return {"nodes": [
        {"id": "sample", "op": "measure_p10", "input": "frame"},
        {"id": "near", "op": "classify_near", "input": "sample"},
        {"id": "ordered", "op": "rank_nearest", "input": "near"},
    ], "output": "ordered"}


class ReadGraphComposition(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.prompts = []
        self.args = {"columns": 4, "distance_m": 1.1}
        self.text = "将静止图像划成四列，对1.1米范围做近距复核，按距离递增返回。"

    def propose(self, replies):
        model = {"protocol": "ollama", "model": "fixture", "version": "test", "url": "http://example.invalid",
                 "options": {}}
        choices = iter(replies)

        def request(url, payload, timeout):
            self.prompts.append(json.loads(payload["messages"][-1]["content"]))
            self.assertTrue(url.endswith("/api/chat"))
            return {"message": {"content": json.dumps(next(choices))}}

        def record(kind, payload):
            self.events.append((kind, deepcopy(payload)))

        return compose(self.text, {"graph_language": GRAPH_LANGUAGE}, self.args, {}, record, model, request)

    def test_schema_and_typed_edges_reject_out_of_bounds(self):
        edits = {
            "duplicate node": lambda g: g["nodes"][2].update(id="sample"),
            "rebind root": lambda g: g["nodes"][0].update(id="frame"),
            "forward reference": lambda g: g["nodes"][0].update(input="near"),
            "wrong port type": lambda g: g["nodes"][1].update(input="frame"),
            "unregistered operation": lambda g: g["nodes"][0].update(op="launch_driver"),
            "escape node id": lambda g: g["nodes"][0].update(id="../opaque"),
            "outcome missing": lambda g: g.update(output="sample"),
            "result unknown": lambda g: g.update(output="unseen"),
            "hidden extra parameter": lambda g: g["nodes"][1].update(endpoint="other"),
            "unused operation": lambda g: g["nodes"].append({"id":"unused","input":"frame","op":"measure_median"}),
            "too many nodes": lambda g: g["nodes"].extend([dict(g["nodes"][0]) for _ in range(6)]),
        }
        for name, edit in edits.items():
            with self.subTest(name=name):
                graph = review_graph()
                edit(graph)
                with self.assertRaises(ValueError):
                    validate_graph(graph, GRAPH_LANGUAGE)

    def test_invalid_then_correct_retry_uses_machine_feedback(self):
        wrong = review_graph()
        wrong["nodes"][1]["input"] = "frame"
        corrected = review_graph()
        result = self.propose([wrong, corrected])
        self.assertEqual(result, {**self.args, "graph": corrected})
        self.assertEqual(len(self.prompts), 2)
        self.assertNotIn("previous_validation_error", self.prompts[0])
        self.assertIn("incompatible type", self.prompts[1]["previous_validation_error"])
        self.assertEqual(self.prompts[0]["operations"], self.prompts[1]["operations"])
        self.assertEqual(self.prompts[1]["request"], self.text)
        self.assertEqual(self.prompts[1]["arguments"], self.args)
        calls = [(kind, value["attempt"]) for kind, value in self.events if kind.startswith("planner.graph_")]
        self.assertEqual(calls, [("planner.graph_rejected", 1), ("planner.graph_composed", 2)])

    def test_second_failure_refuses_without_fabricating_a_plan(self):
        bad = review_graph()
        bad["nodes"][1]["op"] = "publish_motion"
        with self.assertRaises(Refused) as caught:
            self.propose([bad, {"nodes": [], "output": "frame"}, review_graph()])
        self.assertEqual(caught.exception.code, "planner.invalid_proposal")
        self.assertEqual(len(self.prompts), 2)
        phases = [kind for kind, _ in self.events if kind.startswith("planner.graph_")]
        self.assertEqual(phases, ["planner.graph_rejected", "planner.graph_rejected"])
        self.assertFalse(any(kind in ("dispatch", "tool.request") for kind, _ in self.events))
