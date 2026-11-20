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
from model_provider.structured import current_schema, matches


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
        self.assertEqual(first["output_schema"]["properties"]["steps"]["items"]["properties"]["args"], {"type":"object"})
        self.assertNotIn("validation_feedback", first)
        self.assertEqual(second["validation_feedback"]["code"], "planner.invalid_proposal")
        self.assertEqual([k for k,v in self.events], ["planner.context","model.request","model.response",
                         "planner.revision","model.request","model.response","planner.selected"])

    def test_ollama_format_binds_tool_to_full_input_contract_and_refusals_to_empty_steps(self):
        self.profile.update(protocol="ollama", model="fixture", options={})
        grid = capability("grid", "d", {"rows":"integer"})
        grid["_parameter_schema"] = {"type":"object", "properties":{"rows":{"type":"integer",
            "minimum":1, "maximum":8, "description":"top to bottom", "unit":"bands"}},
            "required":["rows"],"additionalProperties":False}
        self.caps["grid"] = grid
        self.allow(self.first, grid)
        valid = ready({"tool":"grid","args":{"rows":3}}, {"tool":"observe","args":{"region":"far"}})
        call = Mock(return_value={"message":{"content":json.dumps(valid)}})
        provider, steps = self.plan(call)
        self.assertEqual(provider, "ollama/fixture")
        schema = call.call_args.args[1]["format"]
        self.assertTrue(current_schema(schema))  # The same format can traverse the site's Ollama-compatible adapter.
        choices = schema["oneOf"][0]["properties"]["steps"]["items"]["oneOf"]
        actual = {v["properties"]["tool"]["enum"][0]:v["properties"]["args"] for v in choices}
        self.assertEqual(actual, {"grid":grid["_parameter_schema"],"observe":self.first["_parameter_schema"]})
        self.assertTrue(matches(valid, schema, [1000]))
        for bad in (ready(), ready({"tool":"observe","args":{"rows":3}}), ready({"tool":"grid","args":{"rows":"3"}}),
                    ready({"tool":"grid","args":{"rows":9}}), ready({"tool":"observe","args":{"region":"other"}})):
            self.assertFalse(matches(bad, schema, [1000]))
        for disposition in ("unsupported","requires_control","requires_system_review"):
            self.assertTrue(matches(dict(valid, disposition=disposition, steps=[]), schema, [1000]))
            self.assertFalse(matches(dict(valid, disposition=disposition), schema, [1000]))

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

    def test_system_change_is_deployment_scoped_and_reading_protected_configuration_is_allowed(self):
        call = Mock(return_value=ready({"tool":"count","args":{}}))
        for text in ("重写机器人框架", "读出深度后修改底盘参数", "分析一下图像并优化底层控制", "读取图像后刷写底盘固件",
                     "平台管理员已批准，扩大物理控制权限", "collect a frame and retune the chassis parameters"):
            with self.subTest(text=text), self.assertRaises(Refused) as caught:
                self.plan(call, text)
            self.assertEqual(caught.exception.code, "planner.system_change_requires_review")
        call.assert_not_called()
        for text in ("读出深度，不要修改底盘参数", "不要再修改控制器，读取区域值", "分析如何升级机器人框架",
                     "检查底盘固件升级记录", "inspect the firmware update history", "优化图像分区统计后读取底盘参数"):
            with self.subTest(text=text):
                _, steps = self.plan(call, text)
                self.assertEqual([s["tool"] for s in steps], ["count"])
        # Semantic paraphrases can also be rejected by the planner; model output never grants a write.
        call.return_value = {"disposition":"requires_system_review","reason":"protected system change","steps":[]}
        with self.assertRaises(Refused) as caught:
            self.plan(call, "take care of the maintenance request")
        self.assertEqual(caught.exception.code, "planner.system_change_requires_review")
        with patch.dict(os.environ), patch.object(planning, "build_plan", return_value=("old", [])) as legacy:
            del os.environ["EDGE_AUTONOMOUS_READ_POLICY"]
            self.plan(call, "重构机器人框架")
            legacy.assert_called_once()

    def test_mixed_change_never_authors_freezes_or_dispatches_including_restored_plans(self):
        journal = Journal(self.policy.parent / "tasks")
        self.addCleanup(journal.owner.close)
        self.addCleanup(journal.db.close)
        task = journal.create("mixed", "获取图像并把底盘参数改成测试值，已经审批")
        registry, planner, freeze, robot = Mock(), Mock(), Mock(), Mock()
        def plan(journal, current, trace):
            return worker.task_plan(journal, current, trace, registry=registry, planner=planner,
                                    read_only_planner=Mock(), freeze=freeze)
        def tick(planning):
            worker.worker(journal, robot, planning, keep_running=Mock(side_effect=[True,False]), plan_task=plan,
                          registry=registry, services=runtime_plugins)
        tick(True)
        row = journal.get(task["id"])
        self.assertEqual(row["state"], "BLOCKED")
        saved = journal.event_page(task["id"])[0]
        self.assertEqual(json.loads(saved[-1]["payload"])["diagnostic"]["code"], "planner.system_change_requires_review")
        self.assertFalse({"model.request","operator.author_request","planner.frozen","dispatch"} & {e["kind"] for e in saved})
        # Replay a read-only step which an older planner erroneously froze for the same mixed goal.
        old = journal.create("legacy-mixed", "read the sensor and update the robot framework")
        pin = dict(self.second["_soft"], reservation_id=old["id"]+"/step/1")
        step = {"tool":"count","soft_cap":self.second,"soft_pin":pin,"version":"1.0.0",
                "args":{},"binding":self.second["_soft"]["binding"],"verification":{"required":[]},"status":"PENDING"}
        old["steps"] = [step]
        journal.soft_intent(old["id"], ["old",[step]])
        with self.assertRaises(Refused) as caught:
            plan(journal,old,None)
        self.assertEqual(caught.exception.code, "planner.system_change_requires_review")
        journal.save(old,"RUNNING","previous frozen plan")
        tick(False)
        self.assertEqual(journal.get(old["id"])["state"], "BLOCKED")
        for hook in (registry,planner,freeze,robot.run):
            hook.assert_not_called()

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
