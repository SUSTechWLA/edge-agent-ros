"""Exercise the independent auditor against real provider code and corrupted offline receipts."""
from copy import deepcopy
import json
import unittest

import test_review055_providers as providers
from scripts import verify_review_comparison as audit


class OfflineVerifier(unittest.TestCase):
    def setUp(self):
        self.fixture = providers.Review()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_cross_checks_partial_coverage_and_tampered_pixels(self):
        inputs = self.fixture
        regions = audit.measure(inputs.dir, inputs.meta, 3, 1.)
        self.assertEqual([r["valid_fraction"] for r in regions], [1., .6, .3])
        self.assertEqual([r["state"] for r in regions], ["near_surface", "near_surface", "unknown"])
        for name in ("user", "review_builtin", "review_composed"):
            kwargs = {"graph": providers.graph()} if name == "review_composed" else {}
            facts = inputs.run_tool(name, **kwargs)["evidence"]
            audit.assert_pixels(facts, inputs.meta, inputs.ref, regions, 1.)
            facts["review"]["regions"][0]["depth_m"] = .95
            with self.assertRaisesRegex(ValueError, "incorrect classification"):
                audit.assert_pixels(facts, inputs.meta, inputs.ref, regions, 1.)
        depth_path = inputs.dir / (inputs.meta["depth_ref"][7:] + ".png")
        depth_path.write_bytes(depth_path.read_bytes()[:-4] + b"oops")
        with self.assertRaisesRegex(ValueError, "PNG hash"):
            audit.measure(inputs.dir, inputs.meta, 3, 1.)

    def test_graph_must_match_model_response_and_each_service_receipt(self):
        graph = providers.graph()
        args = {"columns": 3, "distance_m": 1., "graph": graph}
        facts = self.fixture.run_tool("review_composed", graph=graph)["evidence"]
        model = "ollama/test-model"
        text = "Rank the low-depth columns."
        context = {"request":text,"arguments":{"columns":3,"distance_m":1.},
                   "root":"frame","required_output_type":"review","operations":providers.adapters.OPS}
        reply = {"call_id":"abc", "provider":model, "protocol":"ollama.chat",
                 "artifact":{"content":{"message":{"content":json.dumps(graph)}}}}
        request = {"call_id":"abc","provider":model,"protocol":"ollama.chat","prompt":{"id":"planner.compose_read_graph"},
                   "artifact":{"content":{"model":"test-model","messages":[{"role":"user","content":json.dumps(context)}]}}}
        events = {
            "model.request":[({"seq":1},request)],
            "model.response":[({"seq":2,"event_id":"reply"},reply)],
            "planner.graph_composed":[({"seq":3,"event_id":"graph"},{"graph":graph,"provider":model})],
        }
        self.assertEqual(audit.check_graph(events,args,facts,text)["services"],
                         ["review.measure","review.classify","review.rank"])
        wrong = deepcopy(facts)
        wrong["graph_receipts"][1]["output_sha256"] = wrong["graph_receipts"][2]["output_sha256"]
        with self.assertRaisesRegex(ValueError, "receipt mismatch"):
            audit.check_graph(events,args,wrong,text)
        reply["artifact"]["content"]["message"]["content"] = json.dumps(providers.graph("rank_image"))
        with self.assertRaisesRegex(ValueError, "persisted Ollama response"):
            audit.check_graph(events,args,facts,text)
