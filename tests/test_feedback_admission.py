"""One physical reservation per station feedback round, including transport restarts."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from agent.kernel import feedback_admission as admission
from agent.kernel.ledger import Journal
from edge_contracts.errors import OutcomeUnknown
from edge_contracts.soft_transport import SoftError


class StationReservationTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.policy = self.root/"policy.json"
        self.policy.write_text(json.dumps({"commissioned_motion_tools":["reposition"]}))
        self.db = Journal(self.root)
        self.addCleanup(lambda:self.db.db.close())
        self.addCleanup(lambda:self.db.owner.close())
        self.db.create("job","移动底盘采样")
        self.task = self.db.get("job")
        self.task["steps"] = [dict(tool="observe",status="SUCCEEDED",evidence={"pose":1})]
        self.task["cursor"] = 1
        self.db.save(self.task, "PLANNING", "first observation")
        self.pin = dict(name="reposition",version="2",binding="sha256:"+"a"*64,resource_id="chassis",
                        plugin_id="motion.local",provider_id="motion.command",verifier_id="motion.odometry",
                        content_digest="sha256:"+"d"*64,**{"class":"physical"})
        self.cap = dict(id=self.pin["name"],version="2",effect="actuation",parameters={"route":"string"})
        self.caps = {self.pin["name"]:{**self.cap, "_physical":self.pin}}
        self.registry = Mock(side_effect=lambda:self.caps)
        self.step = dict(tool="reposition",version="2",binding=self.pin["binding"],status="PENDING",
                         args={"route":"north"},feedback={"goal":{"tool":"fit"},"required_motion_tools":["reposition"]},
                         verification={"required":[]},
                         physical_cap=self.cap,physical_pin=self.pin)
        self.planner = Mock(return_value=("edge-model", [*self.task["steps"], self.step]))
        self.reservations = {}
        def owner(action, task, index, provider, step):
            self.assertEqual(action, "freeze_step")
            self.assertIsNotNone(self.db.soft_intent(task))
            op = f"{task}/step/{index}"
            frozen = deepcopy(step)
            frozen["physical_pin"]["reservation_id"] = op
            value = self.reservations.setdefault(op, [provider, frozen])
            self.assertEqual(value, [provider, frozen])
            return value
        self.owner = Mock(side_effect=owner)

    def plan(self):
        return admission.plan_round(self.db, self.task, None, self.policy,
                                    registry=self.registry, planner=self.planner, physical_request=self.owner)

    def test_lost_reply_restarts_with_same_round_and_exact_proposal(self):
        freeze = self.owner.side_effect
        def drop(*args, **kwargs):
            freeze(*args, **kwargs)
            raise SoftError("plugin.transport","lost reply")
        self.owner.side_effect = drop
        with self.assertRaises(SoftError):
            self.plan()
        self.db.owner.close()
        self.db.db.close()
        self.db = Journal(self.root)
        self.task = self.db.get("job")
        self.owner.side_effect = freeze
        self.task["provider"], self.task["steps"] = self.plan()
        self.db.save(self.task, "RUNNING", "admitted", kind="planned")
        self.assertEqual(self.planner.call_count,1)
        self.assertEqual(self.owner.call_args_list[0],self.owner.call_args_list[1])
        self.assertEqual(list(self.reservations),["job/step/2"])
        self.assertIsNone(self.db.soft_intent("job"))
        step = self.task["steps"][-1]
        admission.authorize_dispatch(step,"job/step/2",self.caps["reposition"],self.policy,self.registry)
        with self.assertRaises(OutcomeUnknown):
            admission.authorize_dispatch(step,"other/step/2",self.caps["reposition"],self.policy,self.registry)
        self.policy.write_text("{}")
        with self.assertRaises(OutcomeUnknown):
            admission.authorize_dispatch(step,"job/step/2",self.caps["reposition"],self.policy,self.registry)

    def test_goal_grant_requires_owner_receipt_and_is_revocable(self):
        grant = {"version":1,"goals":{"fit":{"request_pattern":"标定传感器","tools":["reposition"],
                 "requires_evidence":{"tool":"observe","field":"front_placement_confirmed"}}}}
        path = self.root/"grant.json"
        path.write_text(json.dumps(grant),encoding="utf-8")
        self.task["text"] = "标定传感器"
        observer = self.task["steps"][0]
        with patch.dict(os.environ,EDGE_FEEDBACK_GOAL_MOTION_GRANTS=str(path)):
            ident = admission.authorization.station_motion_grant(self.task["text"],"fit")["digest"]
            observer.update(operation_id="job/step/1",feedback={"station_grant":ident},evidence={"front_placement_confirmed":True})
            self.step["feedback"]["station_grant"] = ident
            _, rows = self.plan()
            admitted = rows[-1]
            admission.authorize_dispatch(admitted,"job/step/2",self.caps["reposition"],self.policy,self.registry)
            path.write_text('{"version":1,"goals":{}}',encoding="utf-8")
            with self.assertRaises(OutcomeUnknown):
                admission.authorize_dispatch(admitted,"job/step/2",self.caps["reposition"],self.policy,self.registry)

    def test_refuses_replacement_stale_policy_and_foreign_intent(self):
        self.owner.side_effect = SoftError("plugin.transport","unavailable")
        with self.assertRaises(SoftError):
            self.plan()
        self.caps["reposition"] = {**self.caps["reposition"], "_physical":{**self.pin,"binding":"b"*64}}
        with self.assertRaises(OutcomeUnknown):
            self.plan()
        self.caps["reposition"]["_physical"] = self.pin
        self.policy.write_text("{}")
        with self.assertRaises(OutcomeUnknown):
            self.plan()
        self.assertEqual(self.owner.call_count,1)
        self.assertEqual(self.planner.call_count,1)
        self.assertEqual(self.db.soft_intent("job")["index"],2)

    def test_malformed_reply_or_history_cannot_add_an_action(self):
        for reply in (None, ["other", {}], ["edge-model", dict(self.step,args={"route":"south"})]):
            self.owner.side_effect = None
            self.owner.return_value = reply
            with self.assertRaises(OutcomeUnknown):
                self.plan()
        self.task["steps"][0]["evidence"] = {"pose":2}
        with self.assertRaises(OutcomeUnknown):
            self.plan()
        self.assertEqual(self.owner.call_count,3)
        self.assertEqual(self.planner.call_count,1)

    def test_legacy_intent_and_http_or_soft_impostor_never_reach_owner(self):
        self.caps["reposition"] = {**self.cap,"endpoint":"http://legacy"}
        with self.assertRaises(OutcomeUnknown):
            self.plan()
        self.caps["reposition"] = {**self.cap,"_soft":self.pin}
        with self.assertRaises(OutcomeUnknown):
            self.plan()
        self.db.soft_intent("job", ["older", [self.step]])
        with self.assertRaises(OutcomeUnknown):
            self.plan()
        self.owner.assert_not_called()
        for forged in (self.step, dict(self.step,feedback={}), dict(self.step,physical_pin=None)):
            with self.assertRaises(OutcomeUnknown):
                admission.authorize_dispatch(forged, "job/step/2", self.cap, self.policy, self.registry)

    def test_default_station_read_step_needs_no_reservation(self):
        self.policy.write_text("{}")
        self.caps["observe"] = dict(id="observe",effect="read",endpoint="http://observer")
        observation = dict(tool="observe",args={},feedback={"goal":{"tool":"fit"}},status="PENDING")
        self.planner.return_value = ("local", [*self.task["steps"],observation])
        self.assertEqual(self.plan(),self.planner.return_value)
        admission.authorize_dispatch(observation, "job/step/2", self.caps["observe"], self.policy, self.registry)
        self.assertIsNone(self.db.soft_intent("job"))
        self.owner.assert_not_called()

    def test_station_feedback_round_uses_deployed_controller_contract(self):
        from test_navigation_physical import fixture_profile, fixture_deployment, NavigationController, NavigationLedger
        from agent import planning

        profile = fixture_profile()
        deployment = fixture_deployment(profile)
        ledger = NavigationLedger(self.root, "fixture-boot")
        self.addCleanup(ledger.close)
        ledger.install(deployment)
        controller = NavigationController(ledger, deployment)
        for role in ("executor","verifier"):
            controller.command(role, dict(action="next", binding=deployment["pin"]["binding"], instance_id=role))
        def catalog():
            return {cap["id"]:cap for cap in controller.catalog()["tools"]}
        goal = dict(description="到达指定地点",tool="nav_execute",field="route_ref",
                    equals=profile["routes"][0]["route_ref"],tools=["nav_execute"])
        self.policy.write_text(json.dumps(dict(goals={"navigation":goal},max_steps=6,
                                               commissioned_motion_tools=["nav_execute"])))
        prior=self.task["steps"][0]
        prior.update(feedback={"goal":{"tool":goal["tool"]}, "required_motion_tools":["nav_execute"]})
        def planner(task,caps,path,trace):
            return planning.feedback_plan(task,caps,path,trace,
                   model_profile={"version":"test","protocol":"http-planner","url":"http://test.invalid"},
                   model_request=lambda *args:dict(goal="navigation",tool="nav_execute",
                                     args={"route_ref":goal["equals"]},reason="根据上一轮观测选择已登记路线"))
        provider, steps = admission.plan_round(self.db, self.task, None, self.policy,
                            registry=catalog, planner=planner,
                            physical_request=lambda action, **fields:controller.command("agent",dict(action=action,**fields)))
        self.assertEqual(provider,"http-planner")
        step = steps[-1]
        op = "job/step/2"
        admission.authorize_dispatch(step,op,catalog()["nav_execute"],self.policy,catalog)
        reply = controller.command("agent", dict(action="call",pin=step["physical_pin"],
                                                 wire=dict(version=step["version"],arguments=step["args"],operation_id=op)))
        self.assertEqual(reply,{"operation_id":op,"state":"RUNNING"})
        work=controller.command("executor",dict(action="next",binding=deployment["pin"]["binding"],instance_id="executor"))
        self.assertEqual(work["operation_id"],op)
        self.assertEqual(work["route"]["route_ref"],goal["equals"])
        controller.heartbeat.clear()
        lookup = lambda pin,action:controller.command("agent",dict(action=action,pin=pin))
        self.assertNotIn("lease",lookup(step["physical_pin"],"operation_status"))
        self.assertFalse(catalog())
        self.assertIsNone(admission.poll_or_issue(dict(step,operation_id=op),op,step["physical_cap"],self.policy,catalog,
                                                  owner_status=lookup,issue=self.fail))

    def test_worker_polls_issued_owner_through_catalog_loss_then_accepts_independent_proof(self):
        from agent.kernel.worker import worker
        from agent import runtime_plugins
        from edge_contracts.errors import Refused
        op="job/step/2"
        self.task["provider"], self.task["steps"] = self.plan()
        self.db.save(self.task,"RUNNING","admitted",kind="planned")
        original=self.task["steps"][-1]
        row=dict(operation_id=op,pin=original["physical_pin"],args=original["args"],state="RESERVED",settled=False)
        def issue():
            self.assertEqual(row["state"],"RESERVED")
            row["state"]="CLAIMED"
        writer=Mock(side_effect=issue)
        lookup=Mock(side_effect=lambda pin,action:deepcopy(row))
        robot=Mock()
        robot.run.side_effect=lambda step,op,cap,trace,ctx:admission.poll_or_issue(
            step,op,cap,self.policy,self.registry,owner_status=lookup,issue=writer)
        def tick():
            self.db.db.execute("UPDATE tasks SET due=0 WHERE id='job'")
            worker(self.db,robot,False,keep_running=Mock(side_effect=[True,False]),
                   plan_task=self.fail,registry=self.fail,services=runtime_plugins)
            return self.db.get("job")
        self.assertEqual(tick()["state"],"WAITING")
        self.policy.write_text("{}")
        self.caps.clear()
        self.assertEqual(tick()["state"],"WAITING")
        row.update(state="SUCCEEDED",proof={"operation_id":op},candidate={"destination":"north"})
        task=tick()
        self.assertEqual(task["state"],"PLANNING")
        self.assertEqual(task["steps"][-1]["evidence"]["verifier_id"],original["physical_pin"]["verifier_id"])
        self.assertEqual(writer.call_count,1)
        self.assertEqual(lookup.call_count,3)
        self.assertEqual(self.db.db.execute("SELECT operation FROM soft_settlements").fetchall()[0][0],op)
        row["state"]="FAILED"
        with self.assertRaises(Refused):
            robot.run(task["steps"][-1],op,self.cap,None,None)
        self.assertEqual(writer.call_count,1)

    def test_after_admission_error_retry_can_only_reconcile_original_receipt(self):
        from edge_contracts.errors import OperationCanceled
        op="job/step/2"
        self.task["provider"],self.task["steps"]=self.plan()
        step=self.task["steps"][-1]
        step.update(status="RUNNING", operation_id=op)
        self.db.save(self.task,"RECOVERY_REQUIRED","waiting for original owner",kind="error",op=op,
                     diagnostic={"code":"planner.feedback_admission_unconfirmed"})
        resumed=self.db.retry("job")["steps"][-1]
        self.assertTrue(resumed["reconcile_only"])
        row=dict(pin=step["physical_pin"],args=step["args"],operation_id=op,state="RESERVED",settled=False)
        def poll():
            return admission.poll_or_issue(resumed,op,self.cap,self.policy,self.registry,
                                          owner_status=lambda *args:row,issue=self.fail)
        with self.assertRaises(OutcomeUnknown):
            poll()
        row["state"]="CANCELED"
        with self.assertRaises(OperationCanceled):
            poll()
        for bad in (dict(state="SUCCEEDED",candidate={},proof={"operation_id":"another"}),dict(pin={}),dict(args={})):
            saved=deepcopy(row)
            row.update(bad)
            with self.assertRaises(OutcomeUnknown):
                poll()
            row=saved
