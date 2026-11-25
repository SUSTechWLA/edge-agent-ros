"""Check feedback choices and durable resume independently of model quality."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent import planning
from agent.kernel.ledger import Journal
from agent.kernel.verification import finish_step
from edge_contracts.service_directory import ServiceSnapshot


ROOT = Path(__file__).resolve().parents[1] / "calibration/station"


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        self.caps = {tool["id"]:tool for path in (ROOT/"tools").glob("*.json")
                     if (tool := json.loads(path.read_bytes()))}
        self.policy = ROOT/"feedback.json"
        self.task = {"id":"cal-1", "text":"请帮我标定当前传感器","steps":[]}
        self.profile = {"version":"test","protocol":"http-planner","url":"http://test.invalid"}

    def decide(self,action,goal="sensor_calibration",args=None):
        def model(url,body,timeout):
            self.assertEqual(len(body["observations"]),len(self.task["steps"][-5:]))
            return {"tool":action,"goal":goal,"args":args or {},"reason":"已读取现场观测"}
        return planning.feedback_plan(self.task,self.caps,self.policy,None,
                                      model_profile=self.profile,model_request=model)[1]

    def test_next_action_uses_observation_and_preserves_prior_operation(self):
        self.task["steps"]=self.decide("observe_target",args={"target":"checkerboard"})
        self.task["steps"][0].update(status="SUCCEEDED",operation_id="cal-1/step/1",evidence={"detected":False})
        for chosen,args in (("record_board_sample",{}),("observe_target",{"target":"checkerboard"})):
            steps=self.decide(chosen,args=args)
            self.assertEqual(steps[-1]["tool"],chosen)
            self.assertEqual(steps[:-1],self.task["steps"])

    def test_goal_gate_and_model_required(self):
        with self.assertRaises(planning.Refused):
            self.decide("observe_target","object_observation",{"target":"checkerboard"})
        self.task["text"]="请识别当前校园标志牌"
        with self.assertRaises(planning.Refused):
            self.decide("record_board_sample","object_observation")
        with self.assertRaises(planning.Refused):
            planning.feedback_plan(self.task,self.caps,self.policy,None,model_profile=dict(self.profile,url=""))
        self.task["steps"]=[self.decide("observe_target","object_observation",{"target":"campus_sign"})[0]]*42
        with self.assertRaises(planning.Refused):
            self.decide("observe_target","object_observation",{"target":"campus_sign"})

    def test_feedback_retry_after_restart_has_no_duplicate_dispatch(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            ledger=Journal(root)
            ledger.create("cal-1",self.task["text"])
            task=ledger.get("cal-1")
            task["steps"]=self.decide("observe_target",args={"target":"checkerboard"})
            step=task["steps"][0]
            step["operation_id"]="cal-1/step/1"
            finish_step(ledger,task,step,step["operation_id"],{"detected":False})
            self.assertEqual(ledger.get("cal-1")["state"],"PLANNING")
            ledger.save(task,"BLOCKED","等待操作员",kind="blocked")
            ledger.db.close()
            ledger.owner.close()
            ledger=Journal(root)
            try:
                self.assertTrue(ledger.get("cal-1")["can_retry"])
                resumed=ledger.retry("cal-1")
                self.assertEqual((resumed["state"],resumed["cursor"],len(resumed["steps"])),("PLANNING",1,1))
            finally:
                ledger.db.close()
                ledger.owner.close()

    def test_legacy_status_plan(self):
        with patch.dict("os.environ",{},clear=False):
            provider,steps=planning.plan("查询当前状态",planning.BASE)
        self.assertEqual((provider,steps[0]["tool"]),("local","status"))

    def test_temporal_goal_exposes_only_clock_tools(self):
        self.task["text"]="请为当前两台 MID360 与 D435 自主标定软件时间偏移。"
        self.assertEqual(self.decide("record_clock_motion","clock_calibration")[-1]["tool"],"record_clock_motion")
        with self.assertRaises(planning.Refused):
            self.decide("record_board_sample","sensor_calibration")

    def test_all_station_services_resolve(self):
        registry=ServiceSnapshot(ROOT/"plugins/runtime")
        self.assertFalse(registry.invalid)
        for service in ("calibration.synced_frame","perception.target_detection","calibration.board_plane_measurement",
                        "calibration.extrinsic_fit","calibration.extrinsic_validate","calibration.clock_epoch",
                        "calibration.clock_motion","calibration.clock_offset_fit"):
            self.assertTrue(registry.pin(service)["binding"])


if __name__=="__main__":
    unittest.main()
