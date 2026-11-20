"""Deployment authority must hold across model selection, freeze and journal replay."""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]
from agent import planning, runtime_plugins
from agent.kernel import read_authority, worker
from agent.kernel.ledger import Journal
from edge_contracts.errors import OutcomeUnknown, Refused


def capability(name, char, parameters=None):
    return {"id":name, "description":name, "parameters":parameters or {}, "version":"1.0.0", "effect":"read",
            "_soft":{"name":name, "plugin_id":"test." + name, "version":"1.0.0", "binding":"sha256:" + char * 64}}


def ready(*steps):
    return {"disposition":"ready", "reason":"所选工具可完成全部观测", "steps":list(steps)}


class AutonomousRead(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.policy = Path(tmp.name) / "read.json"
        env = patch.dict(os.environ, {"EDGE_AUTONOMOUS_READ_POLICY":str(self.policy), "EDGE_CONTRACT_DIR":tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.first = capability("observe", "a", {"region":"string"})
        self.first["_parameter_schema"] = {"type":"object", "properties":{
            "region":{"type":"string", "enum":["near","far"]}}, "required":["region"], "additionalProperties":False}
        self.second = capability("count", "b")
        self.allow(self.first, self.second)
        self.caps = {s["id"]:s for s in (self.first, self.second)}
        self.caps.update(planning.BASE)
        self.caps["self_reported"] = {**capability("self_reported", "c"), "endpoint":"http://untrusted.invalid"}
        self.profile = {"protocol":"http-planner", "version":"test", "url":"http://model.invalid"}
        self.events = []

    def allow(self, *caps):
        self.policy.write_text(json.dumps({"schema_version":1,
            "bindings":{s["id"]:s["_soft"]["binding"] for s in caps}}))

    def plan(self, request, text="请比较两种观测"):
        return planning.plan(text, self.caps, lambda k,v:self.events.append((k,v)),
                             model_profile=self.profile, model_request=request)

    def test_dynamic_composition_and_parameter_repair_before_any_dispatch(self):
        request = Mock(side_effect=[ready({"tool":"observe","args":{"region":"wrong"}}),
                                   ready({"tool":"count","args":{}}, {"tool":"observe","args":{"region":"near"}})])
        provider, steps = self.plan(request)
        self.assertEqual(provider, "http-planner")
        self.assertEqual([s["tool"] for s in steps], ["count","observe"])
        self.assertEqual([s["status"] for s in steps], ["PENDING","PENDING"])
        first, second = [c.args[1] for c in request.call_args_list]
        self.assertEqual([s["id"] for s in first["capabilities"]], ["observe","count"])
        self.assertEqual(first["capabilities"][0]["input_schema"], self.first["_parameter_schema"])
        self.assertNotIn("validation_feedback", first)
        self.assertEqual(second["validation_feedback"]["code"], "planner.invalid_proposal")
        self.assertEqual([k for k,v in self.events], ["planner.context","model.request","model.response",
                         "planner.revision","model.request","model.response","planner.selected"])

    def test_forces_model_even_for_exact_utterance_and_builtin_local_shortcut(self):
        request = Mock(return_value=ready({"tool":"count", "args":{}}))
        self.plan(request, "count")
        self.assertEqual(request.call_count, 1)
        request.return_value = ready({"tool":"inspect","args":{"points":[[1,2]]}})
        with self.assertRaises(Refused):
            self.plan(request, "巡检 (1,2)")
        self.assertEqual(request.call_count, 3)

    def test_partial_read_cannot_complete_explicit_motion_and_negative_is_observation(self):
        call = Mock(return_value=ready({"tool":"count","args":{}}))
        for text in ("先看下当前情况，然后向前移动一米", "读一下深度并向前移动一米", "read the depth and drive forward"):
            with self.subTest(text=text), self.assertRaises(Refused) as caught:
                self.plan(call, text)
            self.assertEqual(caught.exception.code, "planner.control_requires_approval")
        for text in ("不要移动，查看静止时的前景", "前进方向的区域深度是多少"):
            self.plan(call, text)
        call.return_value = {"disposition":"requires_control","steps":[], "reason":"任务需要控制"}
        with self.assertRaises(Refused) as caught:
            self.plan(call, "perform the entire multistage chore")
        self.assertEqual(caught.exception.code, "planner.control_requires_approval")

    def test_no_local_fallback_for_unapproved_bindings_missing_policy_or_missing_model(self):
        self.allow(self.caps["self_reported"])
        request = Mock()
        with self.assertRaises(Refused) as caught:
            self.plan(request, "status")
        self.assertEqual(caught.exception.code, "planner.read_catalog_empty")
        self.policy.unlink()
        with self.assertRaises(Refused) as caught:
            self.plan(request, "status")
        self.assertEqual(caught.exception.code, "planner.read_policy_invalid")
        request.assert_not_called()
        self.allow(self.first)
        self.profile["url"] = None
        with self.assertRaises(Refused) as caught:
            self.plan(request, "observe")
        self.assertEqual(caught.exception.code, "planner.not_configured")

    def test_kernel_rechecks_whole_frozen_plan_and_recovers_previously_issued_work(self):
        safe = {"tool":"count","soft_cap":self.second, "soft_pin":self.second["_soft"],
                "binding":self.second["_soft"]["binding"], "verification":{"required":[]}, "status":"PENDING"}
        bad = dict(safe, physical_pin={"class":"physical"})
        for bad_step in (bad, dict(safe, soft_pin={**safe["soft_pin"], "class":"physical"}),
                         dict(safe, verification={"required":["verify.external"]})):
            with self.assertRaises(Refused):
                read_authority.authorize_dispatch([safe, bad_step])
        read_authority.authorize_dispatch([safe])
        self.allow(self.first)
        with self.assertRaises(Refused):
            read_authority.authorize_dispatch([safe])
        with self.assertRaises(OutcomeUnknown):
            read_authority.authorize_dispatch([dict(safe,status="RUNNING",operation_id="t/step/1")])

    def test_replayed_freeze_cannot_substitute_previous_arguments_or_operation(self):
        safe = {"tool":"observe","args":{"region":"near"},"version":"1.0.0","soft_cap":self.first,
                "soft_pin":self.first["_soft"],"binding":self.first["_soft"]["binding"],
                "verification":{"required":[]},"status":"PENDING"}
        journal = Mock()
        journal.soft_intent.return_value = ("model",[safe])
        frozen = dict(safe, soft_pin={**safe["soft_pin"],"reservation_id":"task/step/1"})
        args = {"registry":Mock(),"planner":Mock(),"read_only_planner":Mock()}
        task = {"id":"task","text":"inspect the near region"}
        for bad in (dict(frozen,args={"region":"far"}), dict(frozen,soft_pin=dict(frozen["soft_pin"],reservation_id="other/step/1"))):
            with self.assertRaises(OutcomeUnknown):
                worker.task_plan(journal, task, None, freeze=Mock(return_value=("model",[bad])), **args)
        self.assertEqual(worker.task_plan(journal, task, None, freeze=Mock(return_value=("model",[frozen])),
                                         **args), ("model",[frozen]))
        args["planner"].assert_not_called()

    def test_deployment_actor_is_supplied_to_model_checked_and_rechecked_on_replay(self):
        cap = capability("observe_actor", "d", {"actor_ref":"string"})
        self.caps[cap["id"]] = cap
        self.allow(cap)
        bad = {"tool":cap["id"],"args":{"actor_ref":"other"}}
        good = {"tool":cap["id"],"args":{"actor_ref":"rover.1"}}
        with patch.dict(os.environ, {"EDGE_ACTOR_REF":"rover.1"}):
            request = Mock(side_effect=[ready(bad), ready(good)])
            provider, (step,) = self.plan(request)
            self.assertEqual(request.call_args.args[1]["actor_ref"], "rover.1")
            self.assertEqual(step["args"], good["args"])
            for args in ({}, bad["args"]):
                with self.assertRaises(Refused):
                    read_authority.authorize_dispatch([dict(step, soft_cap=cap, soft_pin=cap["_soft"],
                                                            args=args, verification={"required":[]})])
        with patch.dict(os.environ, {"EDGE_ACTOR_REF":"bad actor!"}):
            with self.assertRaises(Refused) as caught:
                self.plan(Mock())
            self.assertEqual(caught.exception.code, "planner.read_actor_invalid")

    def test_recovery_never_calls_freeze_or_robot_for_old_physical_task(self):
        journal = Journal(self.policy.parent / "journal")
        self.addCleanup(journal.owner.close)
        self.addCleanup(journal.db.close)
        task = journal.create("old", "先看环境，再开到目标点", None)
        old = {"tool":"move","status":"PENDING","physical_pin":{"class":"physical","reservation_id":"old/step/1"}}
        journal.soft_intent("old", ["local",[old]])
        freeze = Mock()
        with self.assertRaises(Refused):
            worker.task_plan(journal, task, None, registry=Mock(), planner=Mock(), read_only_planner=Mock(), freeze=freeze)
        freeze.assert_not_called()
        task["provider"], task["steps"] = "local", [old]
        journal.save(task,"RUNNING","legacy frozen plan",kind="planned")
        robot = Mock()
        worker.worker(journal,robot,False,keep_running=Mock(side_effect=[True,False]),plan_task=Mock(),
                      registry=Mock(),services=runtime_plugins)
        self.assertEqual(journal.get("old")["state"], "BLOCKED")
        robot.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
