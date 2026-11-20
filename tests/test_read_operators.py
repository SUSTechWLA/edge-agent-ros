"""A real task owns decomposition, model candidate, blind checks and its frozen read."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent import planning
from agent.kernel import read_authority, worker
from agent.kernel.ledger import Journal
from edge_contracts.errors import Refused, Unavailable


class OperatorTask(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ops = self.root / "ops"
        self.ops.mkdir()
        self.site = self.root / "operators.json"
        self.reads = self.root / "reads.json"
        self.name = "bench_compute"
        self.binding = "sha256:" + "d"*64
        self.reads.write_text(json.dumps({"schema_version":1,"bindings":{self.name:self.binding}}))
        self.contract = {"description":"Difference between largest and smallest finite measurements",
                         "cases":[{"sample":[3,1,5],"expected":4},{"sample":[2,8],"expected":6}]}
        self.site.write_text(json.dumps({"schema_version":1, "runner":self.name,"contracts":{"spread":self.contract}}))
        self.env = patch.dict(os.environ, {"EDGE_READ_OPERATOR_POLICY":str(self.site),"EDGE_READ_OPERATOR_DIR":str(self.ops),
                             "EDGE_AUTONOMOUS_READ_POLICY":str(self.reads),"EDGE_CONTRACT_DIR":str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.journal = Journal(self.root / "tasks")
        self.addCleanup(self.journal.owner.close)
        self.addCleanup(self.journal.db.close)
        params = {"operator_digest":"string","columns":"integer"}
        self.cap = {"id":self.name,"description":"Apply a qualified calculation to observations",
                    "effect":"read","version":"1.0.0","parameters":params,
                    "_soft":{"name":self.name,"binding":self.binding},
                    "_parameter_schema":{"type":"object","properties":{k:{"type":v} for k,v in params.items()},
                        "required":list(params),"additionalProperties":False}}
        self.gap = {"disposition":"read_compute","steps":[],"reason":"a single measured scalar requires a reduction",
                    "computation":{"contract":"spread","input":{"columns":3}}}
        self.program = {"tokens":["max","min","sub"]}
        self.author_reply = {"instructions":self.program["tokens"]}
        self.events = []

    def run_plan(self, task, replies):
        profile = {"protocol":"http-planner","url":"http://fixture","version":"test","timeout_s":210}
        def plan(*args, **kwargs):
            return planning.plan(*args, **kwargs, model_profile=profile,model_request=replies)
        def freeze(task_id, candidate):
            provider, steps = deepcopy(candidate)
            for i,s in enumerate(steps, 1):
                s["soft_pin"]["reservation_id"] = f"{task_id}/step/{i}"
            return provider, steps
        def trace(kind,data):
            self.events.append((kind,data))
            self.journal.exchange_event(task,kind,data,None)
        return worker.task_plan(self.journal, task, trace, registry=lambda:{self.name:self.cap},planner=plan,
                                read_only_planner=Mock(),freeze=freeze)

    def test_new_program_is_verified_and_reused_by_another_task_without_author_call(self):
        original = self.journal.create("parent", "compare the extent of the observed levels")
        calls = Mock(side_effect=[self.gap, self.author_reply])
        _, (step,) = self.run_plan(original, calls)
        self.assertTrue(all(call.args[2] == 210 for call in calls.call_args_list))
        for call in calls.call_args_list:
            self.assertNotIn('"cases"', json.dumps(call.args[1]))
        self.assertEqual(step["soft_pin"]["reservation_id"],"parent/step/1")
        record = json.loads(next(self.ops.glob("*.json")).read_bytes())
        self.assertEqual(step["args"],{"columns":3,"operator_digest":record["operator_digest"]})
        self.assertEqual(record["program"],self.program)
        self.assertEqual(record["validation"]["case_count"],2)
        self.assertEqual(step["read_operator"], "spread")
        kinds = [kind for kind,_ in self.events]
        for first,second in zip(("planner.decomposed","operator.gap","operator.author_request","operator.verified",
                                 "operator.installed","planner.resumed"),("operator.gap","operator.author_request",
                                 "operator.verified","operator.installed","planner.resumed","planner.frozen")):
            self.assertLess(kinds.index(first), kinds.index(second))
        self.events.clear()
        follow = self.journal.create("second","another request for the difference in regional levels")
        calls = Mock(return_value=self.gap)
        _, (again,) = self.run_plan(follow, calls)
        self.assertEqual(calls.call_count,1)
        self.assertEqual(again["args"]["operator_digest"],record["operator_digest"])
        self.assertIn("operator.reused",[kind for kind,_ in self.events])
        self.assertNotIn("operator.author_request",[kind for kind,_ in self.events])
        with self.assertRaises(Refused):
            read_authority.authorize_dispatch([dict(step,read_operator="unapproved")])

    def test_lost_author_reply_resumes_parent_without_second_decomposition_and_budget_survives_restart(self):
        task = self.journal.create("lost","measure and calculate a comparison")
        with self.assertRaises(Unavailable):
            self.run_plan(task, Mock(side_effect=[self.gap,Unavailable("lost")]))
        calls = Mock(side_effect=[self.author_reply])
        _, (step,) = self.run_plan(task,calls)
        self.assertEqual(calls.call_count,1)
        self.assertEqual(step["soft_pin"]["reservation_id"],"lost/step/1")
        self.assertEqual([k for k,_ in self.events].count("planner.decomposed"),1)

    def test_bad_candidates_or_model_injected_digest_never_install_or_freeze(self):
        task = self.journal.create("bad","inspect a static series")
        calls = Mock(side_effect=[self.gap, {"instructions":["shell"]}, {"instructions":["min"]}])
        with self.assertRaises(Refused) as caught:
            self.run_plan(task, calls)
        self.assertEqual(caught.exception.code,"operator.validation_failed")
        with self.assertRaises(Refused) as caught:
            self.run_plan(task, Mock(side_effect=AssertionError("unexpected work")))
        self.assertEqual(caught.exception.code,"operator.attempts_exhausted")
        self.assertEqual(list(self.ops.iterdir()),[])
        task = self.journal.create("forge","inspect a static series")
        forged = {"disposition":"ready","reason":"simulated unauthorized direct runner","steps":[
                  {"tool":self.name,"args":{"columns":3,"operator_digest":self.binding}}]}
        with self.assertRaises(Refused):
            self.run_plan(task, Mock(return_value=forged))
        self.assertNotIn("planner.frozen",[k for k,_ in self.events])

    def test_author_gets_repair_feedback_without_seeing_the_holdout_cases(self):
        task = self.journal.create("repair","compute a comparison")
        bad = {"instructions":["min"]}
        calls = Mock(side_effect=[self.gap,bad,self.author_reply])
        self.run_plan(task,calls)
        revision = calls.call_args_list[-1].args[1]
        self.assertEqual(revision["validation_feedback"],{"code":"operator.independent_verification_mismatch",
                                                        "prior_program":bad})
        self.assertNotIn('"sample"',json.dumps(revision))
        self.assertNotIn('"expected"',json.dumps(revision))

    def test_revocation_during_authoring_stops_publication_and_dispatch(self):
        task = self.journal.create("revoked","calculate the requested range")
        def request(url, body, timeout):
            if "calculation" not in body:
                return self.gap
            self.reads.write_text(json.dumps({"schema_version":1,"bindings":{}}))
            return self.author_reply
        with self.assertRaises(Refused) as caught:
            self.run_plan(task,request)
        self.assertEqual(caught.exception.code,"operator.binding_denied")
        self.assertEqual(list(self.ops.iterdir()),[])
        self.assertNotIn("planner.frozen",[k for k,_ in self.events])


if __name__ == "__main__":
    unittest.main()
