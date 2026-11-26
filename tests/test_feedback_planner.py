"""Check feedback choices and durable resume independently of model quality."""
from contextlib import ExitStack
import json
import os
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

    def motion_observation_budget(self, value, permitted=None):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy = json.loads(self.policy.read_bytes())
        for args in policy.get("argument_catalogs", {}).values():
            for source in args.values():
                source["directory"] = str(ROOT / source["directory"])
        if value is None:
            policy.pop("unavailable_motion_observation_budget", None)
        else:
            policy["unavailable_motion_observation_budget"] = value
        if permitted is None:
            policy.pop("unavailable_motion_observation_tools", None)
        else:
            policy["unavailable_motion_observation_tools"] = permitted
        self.policy = Path(directory.name) / "feedback.json"
        self.policy.write_text(json.dumps(policy), encoding="utf-8")

    def test_next_action_uses_observation_and_preserves_prior_operation(self):
        self.task["steps"]=self.decide("observe_target",args={"target":"checkerboard"})
        self.task["steps"][0].update(status="SUCCEEDED",operation_id="cal-1/step/1",evidence={"detected":False})
        for chosen,args in (("record_board_sample",{}),("observe_target",{"target":"checkerboard"})):
            steps=self.decide(chosen,args=args)
            self.assertEqual(steps[-1]["tool"],chosen)
            self.assertEqual(steps[:-1],self.task["steps"])

    def test_solver_availability_tracks_latest_owner_receipt(self):
        self.task["steps"] = self.decide("sensor_calibration_status")
        status = self.task["steps"][0]
        status.update(status="SUCCEEDED",evidence={"enough_distinct_poses_for_fit":False,"candidate_ready":False})
        with self.assertRaises(planning.Refused):
            self.decide("fit_sensor_extrinsics")
        self.task["steps"] = self.decide("record_board_sample")
        self.task["steps"][-1].update(status="SUCCEEDED",evidence={"enough_distinct_poses_for_fit":True,"candidate_ready":False})
        self.task["steps"] = self.decide("fit_sensor_extrinsics")
        self.task["steps"][-1].update(status="SUCCEEDED",evidence={"candidate_ready":True})
        # Once candidate_ready is true, fit_sensor_extrinsics is superseded until invalidated.
        with self.assertRaises(planning.Refused):
            self.decide("fit_sensor_extrinsics")
        self.assertEqual(self.decide("verify_sensor_extrinsics")[-1]["tool"], "verify_sensor_extrinsics")
        # If verification rejects the candidate, candidate_ready resets to False so fitting is allowed again.
        self.task["steps"] = self.decide("verify_sensor_extrinsics")
        self.task["steps"][-1].update(status="SUCCEEDED",evidence={"outcome":"validation_rejected","candidate_ready":False})
        with self.assertRaises(planning.Refused):
            self.decide("verify_sensor_extrinsics")
        self.assertEqual(self.decide("fit_sensor_extrinsics")[-1]["tool"], "fit_sensor_extrinsics")
        self.task["steps"] = self.decide("record_board_sample")
        self.task["steps"][-1].update(status="SUCCEEDED",evidence={"candidate_ready":False})
        with self.assertRaises(planning.Refused):
            self.decide("verify_sensor_extrinsics")

    def test_attended_angle_tool_requires_positive_current_plan(self):
        goal = "camera_intrinsic_estimation"
        self.task["text"] = "请帮我标定当前D435的内参和前MID360的外参"
        with self.assertRaises(planning.Refused):
            self.decide("adjust_camera_calibration_board",goal)
        self.task["steps"] = self.decide("plan_capture_views",goal)
        step = self.task["steps"][-1]
        step.update(status="SUCCEEDED",evidence={"camera_operator_needed":True})
        self.assertEqual(self.decide("adjust_camera_calibration_board",goal)[-1]["tool"],
                         "adjust_camera_calibration_board")
        step["evidence"]["camera_operator_needed"] = False
        with self.assertRaises(planning.Refused):
            self.decide("adjust_camera_calibration_board",goal)

    def test_spatial_feedback_keeps_only_current_route_choices(self):
        nav = dict(stamp_ns=1_000_000_000,
                   robot=dict(frame="boot_odom", epoch="a"*32+":1", stamp_ns=1_000_000_000,
                              position_m=[0,0,0],orientation_xyzw=[0,0,0,1],yaw_rad=0),
                   target=dict(position_m=[3,0,.5],range_m=3,bearing="front"),
                   nearest_observed_m=dict(front=3,left=8,rear=8,right=8))
        plan = dict(tool="plan_capture_views",status="SUCCEEDED",
                    feedback={"goal":{"tool":"verify_sensor_extrinsics"}},
                    evidence=dict(outcome="candidate_views", navigation_context=nav,
                                  view_options=[dict(ref="view.example",path="reverse_left",end_xyz_m=[-.6,.2,0])]))
        self.task["steps"] = [plan, {**plan}]

        def model(url,body,timeout):
            old, current = body["observations"]
            self.assertNotIn("options",old)
            self.assertNotIn("navigation",old)
            self.assertEqual(current["options"][0]["end_xyz_m"],[-.6,.2,0])
            self.assertEqual(current["navigation"]["target_bearing"],"front")
            return dict(tool="sensor_calibration_status",goal="sensor_calibration",args={},reason="核实状态")

        planning.feedback_plan(self.task,self.caps,self.policy,None,model_profile=self.profile,model_request=model)

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

    def test_deployment_denial_after_reads_cannot_be_retried_as_operator_pause(self):
        with TemporaryDirectory() as directory, ExitStack() as cleanup:
            journal = Journal(Path(directory))
            cleanup.callback(journal.owner.close)
            cleanup.callback(journal.db.close)
            journal.create("cal-1", self.task["text"])
            task = journal.get("cal-1")
            task["steps"] = self.decide("sensor_calibration_status")
            step = task["steps"][0]
            finish_step(journal, task, step, "cal-1/step/1", {"outcome":"inventory"})
            for code in ("planner.required_capability_unavailable", "planner.observation_policy_invalid"):
                with self.subTest(code=code):
                    journal.save(task, "BLOCKED", "测试受阻事件", kind="blocked", diagnostic={"code":code})
                    self.assertFalse(journal.get("cal-1")["can_retry"])
                    with self.assertRaises(planning.Refused):
                        journal.retry("cal-1")
            journal.save(task, "BLOCKED", "测试传输失败", kind="retry_exhausted",
                         diagnostic={"code":"transport.unavailable"})
            self.assertTrue(journal.get("cal-1")["can_retry"])
            self.assertEqual(journal.retry("cal-1")["cursor"], 1)

    def test_registered_operator_pause_resumes_same_feedback_task(self):
        with TemporaryDirectory() as directory:
            journal=Journal(Path(directory))
            try:
                journal.create("cal-1",self.task["text"])
                task=journal.get("cal-1")
                task["steps"]=self.decide("record_board_sample")
                step=task["steps"][0]
                step["operation_id"]="cal-1/step/1"
                result={"paused_for_operator":True,"accepted_poses":2,"message":"板就位后请续办"}
                finish_step(journal,task,step,step["operation_id"],result)
                paused=journal.get("cal-1")
                self.assertEqual((paused["state"],paused["can_retry"]),("BLOCKED",True))
                resumed=journal.retry("cal-1")
                self.assertEqual((resumed["state"],resumed["cursor"]),("PLANNING",1))
                self.assertEqual(resumed["steps"][0]["evidence"]["accepted_poses"],2)
            finally:
                journal.db.close()
                journal.owner.close()

    def test_temporal_goal_exposes_only_clock_tools(self):
        self.task["text"]="请为当前两台 MID360 与 D435 自主标定软件时间偏移。"
        self.assertEqual(self.decide("record_clock_motion","clock_calibration")[-1]["tool"],"record_clock_motion")
        with self.assertRaises(planning.Refused):
            self.decide("record_board_sample","sensor_calibration")

    def test_intermediate_recognition_cannot_complete_a_requested_calibration(self):
        for request in (
            "请标定当前前向 MID360 雷达与 D435 彩色相机的空间外参。识别四孔标定板，在每个停稳姿态录制图像和点云，使用独立姿态验证结果。",
            "请识别四孔板，然后标定当前传感器，返回标定结果。",
            "请标定当前传感器，然后查看标定结果。",
            "Recognize the board and calibrate the front camera and LiDAR.",
        ):
            with self.subTest(request=request):
                self.task.update(text=request,steps=[])
                with self.assertRaises(planning.Refused):
                    self.decide("observe_target","object_observation",{"target":"four_hole_board"})
                steps=self.decide("observe_target",args={"target":"four_hole_board"})
                self.assertEqual(steps[-1]["feedback"]["goal"]["tool"],"verify_sensor_extrinsics")
                self.task["steps"]=steps
                steps[-1].update(status="SUCCEEDED",evidence={"detected":True})
                self.assertEqual(self.decide("record_board_sample")[-1]["tool"],"record_board_sample")

    def test_read_only_board_and_calibration_queries_cannot_arm_recording(self):
        for request,tool,goal,args in (
            ("只识别这块四孔标定板，不要重新标定传感器。","observe_target","object_observation",{"target":"four_hole_board"}),
            ("请问当前雷达是否已经标定？","sensor_calibration_status","calibration_status",{}),
            ("请查看当前传感器标定结果。","sensor_calibration_status","calibration_status",{}),
            ("Is the camera already calibrated?","sensor_calibration_status","calibration_status",{}),
            ("请检测图中的标定板","observe_target","object_observation",{"target":"four_hole_board"}),
        ):
            with self.subTest(request=request):
                self.task.update(text=request,steps=[])
                self.assertEqual(self.decide(tool,goal,args)[-1]["tool"],tool)
                with self.assertRaises(planning.Refused):
                    self.decide("record_board_sample")

    def test_moving_board_detection_is_intermediate_to_explicit_clock_goal(self):
        self.task["text"]="识别移动靶标，标定当前两传感器的软件时间偏移。"
        with self.assertRaises(planning.Refused):
            self.decide("observe_target","object_observation",{"target":"four_hole_board"})
        self.assertEqual(self.decide("record_clock_motion","clock_calibration")[-1]["tool"],"record_clock_motion")

    def test_fixed_board_robot_motion_is_not_downgraded_to_static_calibration(self):
        self.motion_observation_budget(None)
        for request in (
            "请让小车自主换位，固定标定板并完成前雷达和相机的标定。",
            "请标定前雷达和相机，控制底盘移动完成整个标定过程。",
            "你可以控制小车自主移动，不需要我来移动标定板。",
        ):
            with self.subTest(request=request):
                self.task.update(text=request, steps=[])
                with self.assertRaisesRegex(planning.Refused, "本站没有机体运动权限"):
                    self.decide("observe_target", args={"target":"four_hole_board"})

    def test_missing_effect_read_snapshot_is_bounded_and_cannot_complete_goal(self):
        self.motion_observation_budget(1)
        self.task["text"] = "请让小车自主移动，然后识别四孔板。"
        with TemporaryDirectory() as directory, ExitStack() as cleanup:
            journal = Journal(Path(directory))
            cleanup.callback(journal.owner.close)
            cleanup.callback(journal.db.close)
            journal.create("cal-1", self.task["text"])
            task = journal.get("cal-1")

            def inspect(url, body, timeout):
                self.assertEqual(body["unmet_requirement"]["capability"], "body.motion")
                self.assertFalse(body["unmet_requirement"]["available"])
                self.assertEqual(body["unmet_requirement"]["remaining_observations"], 1)
                self.assertEqual(set(body["tools"]), {"observe_target"})
                return {"goal":"object_observation", "tool":"observe_target", "args":{"target":"four_hole_board"},
                        "reason":"先从本机读取当前视野"}

            _, task["steps"] = planning.feedback_plan(task, self.caps, self.policy, None,
                                                      model_profile=self.profile, model_request=inspect)
            step = task["steps"][0]
            self.assertEqual(step["feedback"]["unmet_requirement"], "body.motion")
            finish_step(journal, task, step, "cal-1/step/1", {"detected":True})
            task = journal.get("cal-1")
            self.assertEqual((task["state"], task["cursor"]), ("PLANNING", 1))

            def report(url, body, timeout):
                self.assertFalse(body["tools"])
                self.assertEqual(body["unmet_requirement"]["remaining_observations"], 0)
                self.assertTrue(body["observations"][0]["facts"]["detected"])
                return {"goal":"object_observation", "tool":"decline", "args":{},
                        "reason":"观测到目标，缺少移动能力"}

            with self.assertRaisesRegex(planning.Refused, "观测到目标") as error:
                planning.feedback_plan(task, self.caps, self.policy, None,
                                       model_profile=self.profile, model_request=report)
            self.assertEqual(error.exception.code, "planner.required_capability_unavailable")
            self.assertEqual(error.exception.details["observations"], 1)
            self.assertEqual(error.exception.diagnostic["provider"], "http-planner")

    def test_motion_assessment_excludes_writes_and_physical_or_soft_tools(self):
        self.motion_observation_budget(2, ["observe_target", "sensor_calibration_status", "record_board_sample"])
        self.task["text"] = "请控制小车后退，标定雷达与相机。"
        for marker in ("_physical", "_soft"):
            with self.subTest(marker=marker):
                caps = {**self.caps, "observe_target":{**self.caps["observe_target"], marker:{"pin":"x"}}}
                def model(url, body, timeout):
                    self.assertEqual(set(body["tools"]), {"sensor_calibration_status"})
                    return {"tool":"record_board_sample", "goal":"sensor_calibration", "args":{},
                            "reason":"unsupported tool"}
                with self.assertRaisesRegex(planning.Refused, "机体运动权限"):
                    planning.feedback_plan(self.task, caps, self.policy, None,
                                           model_profile=self.profile, model_request=model)

    def test_site_limits_missing_effect_reads_to_installed_trusted_tools(self):
        self.task["text"] = "请控制小车后退，标定雷达与相机。"
        for allowed, expected in ((["sensor_calibration_status", "not_installed"], {"sensor_calibration_status"}),
                                  (["not_installed"], set()), ([], set())):
            with self.subTest(allowed=allowed):
                self.motion_observation_budget(2, allowed)
                def report(url, body, timeout):
                    self.assertEqual(set(body["tools"]), expected)
                    return {"goal":"sensor_calibration", "tool":"decline", "args":{}, "reason":"原任务需要底盘"}
                with self.assertRaisesRegex(planning.Refused, "原任务需要底盘"):
                    planning.feedback_plan(self.task, self.caps, self.policy, None,
                                           model_profile=self.profile, model_request=report)

        for invalid in (True, "sensor_calibration_status", ["x", "x"], [None]):
            with self.subTest(invalid=invalid):
                self.motion_observation_budget(2, invalid)
                with self.assertRaisesRegex(planning.Refused, "站点只读观察范围配置无效"):
                    self.decide("sensor_calibration_status")

    def commission(self, final="verify_sensor_extrinsics"):
        self.motion_observation_budget(2)
        policy = json.loads(self.policy.read_bytes())
        policy["commissioned_motion_tools"] = ["nav_execute"]
        goal = policy["goals"]["sensor_calibration"]
        goal["tools"].append("nav_execute")
        goal["tool"] = final
        self.policy.write_text(json.dumps(policy), encoding="utf-8")
        pin = {"class":"physical", "name":"nav_execute", "version":"1.0.0", "binding":"sha256:" + "a"*64,
               "content_digest":"sha256:" + "b"*64, "provider_id":"nav.executor", "verifier_id":"nav.observer",
               "resource_id":"fixture.rover", "plugin_id":"nav.fixture"}
        self.caps["nav_execute"] = {"id":"nav_execute", "version":"1.0.0", "effect":"actuation",
                                    "description":"Execute a commissioned fixture operation", "parameters":{}, "_physical":pin}
        self.task["text"] = "请控制小车后退，标定前雷达与相机。"

    def test_commissioned_motion_combines_model_choices_and_independent_receipts(self):
        self.commission()
        with self.assertRaises(planning.Refused):
            self.decide("verify_sensor_extrinsics")
        with TemporaryDirectory() as directory, ExitStack() as cleanup:
            journal = Journal(Path(directory))
            cleanup.callback(journal.owner.close)
            cleanup.callback(journal.db.close)
            journal.create("cal-1", self.task["text"])
            self.task = journal.get("cal-1")
            for tool, result in (("sensor_calibration_status", {"outcome":"inventory"}),
                                 ("nav_execute", {"verifier_id":"nav.observer",
                                                  "verification":{"operation_id":"cal-1/step/2"}}),
                                 ("verify_sensor_extrinsics", {"verified":True})):
                self.task["steps"] = self.decide(tool)
                step = self.task["steps"][-1]
                op = f"cal-1/step/{len(self.task['steps'])}"
                step["operation_id"] = op
                self.assertEqual(step["feedback"]["required_motion_tools"], ["nav_execute"])
                if tool == "nav_execute":
                    self.assertEqual(step["physical_pin"], self.caps[tool]["_physical"])
                    self.assertNotIn("_physical", step["physical_cap"])
                    step["physical_pin"] = dict(step["physical_pin"], reservation_id=op)
                    for bad in ({"verified":True}, {**result,"verifier_id":"nav.executor"},
                                {**result,"verification":{"operation_id":"another/step/2"}}):
                        with self.assertRaises(planning.OutcomeUnknown):
                            finish_step(journal, self.task, step, op, bad)
                    self.assertEqual(self.task["cursor"], 1)
                finish_step(journal, self.task, step, op, result)
                self.task = journal.get("cal-1")
            self.assertEqual(self.task["state"], "SUCCEEDED")

    def test_deployment_goal_grant_waits_for_original_task_placement(self):
        self.commission()
        grant = json.loads((ROOT/"autonomous-board-motion.json").read_bytes())
        grant["goals"]["verify_sensor_extrinsics"]["tools"] = ["nav_execute"]
        grant_path = self.policy.with_name("test-grant.json")
        grant_path.write_text(json.dumps(grant),encoding="utf-8")
        self.task["text"] = "请帮我标定当前传感器"
        with patch.dict(os.environ,EDGE_FEEDBACK_GOAL_MOTION_GRANTS=str(grant_path)):
            for denied in ("nav_execute","plan_capture_views","record_board_sample"):
                with self.assertRaises(planning.Refused):
                    self.decide(denied)
            self.task["steps"] = self.decide("inspect_calibration_view")
            step = self.task["steps"][0]
            self.assertTrue(step["feedback"]["placement_required"])
            step.update(status="SUCCEEDED",operation_id="foreign/step/1",evidence={"front_placement_confirmed":True})
            with self.assertRaises(planning.Refused):
                self.decide("nav_execute")
            step["operation_id"] = self.task["id"]+"/step/1"
            self.assertEqual(self.decide("nav_execute")[-1]["tool"],"nav_execute")
            # Once front placement confirmed a distinct_pose on a motion station, record_board_sample
            # is confirmed ready and supersedes redundant RGB-only observe_target / repeat inspect.
            step["evidence"].update(outcome="distinct_pose", board_view_recordable=True)
            self.assertEqual(self.decide("record_board_sample")[-1]["tool"], "record_board_sample")
            with self.assertRaises(planning.Refused):
                self.decide("observe_target", args={"target":"four_hole_board"})
            with self.assertRaises(planning.Refused):
                self.decide("inspect_calibration_view")
            self.task["text"] = "只查询当前传感器的标定状态"
            with self.assertRaises(planning.Refused):
                self.decide("nav_execute")

    def test_physical_role_requires_explicit_intent_and_current_match(self):
        self.commission()
        cap = self.caps["nav_execute"]
        self.task["text"] = "请标定当前传感器"
        with self.assertRaises(planning.Refused):
            self.decide("nav_execute")
        self.assertEqual(self.decide("sensor_calibration_status")[-1]["tool"], "sensor_calibration_status")
        self.task["text"] = "请控制小车后退，标定前雷达与相机。"
        for changes in ({"_physical":None, "endpoint":"http://example.invalid/legacy"},
                        {"_soft":{}}, {"effect":"read"}, {"version":"2.0.0"},
                        {"binding":"swapped"}, {"_physical":dict(cap["_physical"], name="another_tool")},
                        {"_physical":dict(cap["_physical"], reservation_id="other/step/1")},
                        {"_physical":dict(cap["_physical"], verifier_id="nav.executor")}):
            with self.subTest(changes=changes):
                self.caps["nav_execute"] = {**cap, **changes}
                with self.assertRaisesRegex(planning.Refused, "机体运动权限"):
                    self.decide("nav_execute")
        del self.caps["nav_execute"]
        self.task["steps"] = self.decide("sensor_calibration_status")
        self.assertEqual(self.task["steps"][0]["feedback"]["unmet_requirement"], "body.motion")
        self.caps["nav_execute"] = cap
        with self.assertRaisesRegex(planning.Refused, "机体运动权限"):
            self.decide("nav_execute")

    def test_motion_final_goal_can_be_completed_by_its_own_proof(self):
        self.commission(final="nav_execute")
        with TemporaryDirectory() as directory, ExitStack() as cleanup:
            journal = Journal(Path(directory))
            cleanup.callback(journal.owner.close)
            cleanup.callback(journal.db.close)
            journal.create("cal-1", self.task["text"])
            self.task = journal.get("cal-1")
            self.task["steps"] = self.decide("nav_execute")
            step = self.task["steps"][0]
            step["operation_id"] = "cal-1/step/1"
            step["physical_pin"] = dict(step["physical_pin"], reservation_id=step["operation_id"])
            finish_step(journal, self.task, step, step["operation_id"], {"verified":True, "verifier_id":"nav.observer",
                                                                        "verification":{"operation_id":step["operation_id"]}})
            self.assertEqual(journal.get("cal-1")["state"], "SUCCEEDED")

    def test_kernel_rejects_partial_goal_on_approved_site_even_if_planned(self):
        self.commission()
        with TemporaryDirectory() as directory, ExitStack() as cleanup:
            journal = Journal(Path(directory))
            cleanup.callback(journal.owner.close)
            cleanup.callback(journal.db.close)
            journal.create("cal-1", self.task["text"])
            self.task = journal.get("cal-1")
            self.task["steps"] = self.decide("sensor_calibration_status")
            step = self.task["steps"][0]
            step["feedback"]["goal"].update(tool=step["tool"], field="verified", equals=True)
            finish_step(journal, self.task, step, "cal-1/step/1", {"verified":True})
            self.assertEqual(journal.get("cal-1")["state"], "PLANNING")

    def test_all_station_services_resolve(self):
        registry=ServiceSnapshot(ROOT/"plugins/runtime")
        self.assertFalse(registry.invalid)
        for service in ("calibration.synced_frame","perception.target_detection","calibration.board_plane_measurement",
                        "calibration.extrinsic_fit","calibration.extrinsic_validate","calibration.clock_epoch",
                        "calibration.clock_motion","calibration.clock_offset_fit"):
            self.assertTrue(registry.pin(service)["binding"])


if __name__=="__main__":
    unittest.main()
