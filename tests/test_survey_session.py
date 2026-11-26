import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
import time
from unittest.mock import patch

from exploration.survey_session import FRONTIER, LOAD, RESOLVE, ROUTE, Survey
from exploration.survey_route import route
from exploration.survey_semantic import resolve_place


class Rover:
    def __init__(self):
        self.snapshot_value = dict(stamp=12, pose=dict(x=0.,y=0.,speed=0.,yaw_rate=0.))
        self.dispatched = []
        self.motion_events = []

    def snapshot(self):
        return self.snapshot_value

    def dispatch(self, op, goal):
        self.dispatched.append((op, goal))
        return SimpleNamespace(accepted=True)


class SurveySessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"sessions.json"
        self.rover = Rover()
        self.config = {"radius":.21, "max_step":.8, "max_refocus":2, "timeout_s":120}
        self.service = Survey(self.rover,self.config,self.path,self.path.parent,"http://unused")
        self.row = self.service.create("task","task/step/1","探索并建图",{},"survey","office")
        self.row.update(area=[-2,-2,2,2],coverage={"known":350,"total":400,"unknown":50})
        self.grid = dict(width=40,height=40,resolution=.1,frame="odom",origin=[-2,-2,0],cells=[0]*1600)

    def start_waiting(self):
        self.row.update(status="WAITING_OPERATOR", phase="WAITING_OPERATOR",
                        anomaly=dict(id="review-1", xy=[1,.5],status="waiting"))

    def test_skip_requires_stop_and_resumes_same_durable_operation(self):
        self.start_waiting()
        body = dict(task_id="task",anomaly_id="review-1",decision="skip")
        self.rover.snapshot_value["pose"]["speed"] = .12
        with self.assertRaisesRegex(ValueError,"stop_or_observation"):
            self.service.intervene(body)
        self.rover.snapshot_value["pose"]["speed"] = 0.
        self.service.intervene(body)
        reopened = Survey(self.rover,self.config,self.path,self.path.parent,"http://unused")
        duplicate = reopened.intervene(body)["mission"]
        self.assertEqual(duplicate["operation_id"],"task/step/1")
        self.assertEqual(duplicate["phase"],"RESUMED")
        self.assertEqual(len(duplicate["history"]),1)
        self.assertEqual(len([e for e in duplicate["events"] if e["kind"]=="resumed"]),1)
        self.assertEqual(reopened.rows["task"]["resume_after"],12)
        self.assertEqual(reopened.rows["task"]["avoid"][0]["center"],[1,.5])

    def test_rejected_viewpoint_is_excluded_from_next_rescan(self):
        self.row["anomaly"] = dict(id="review-1",xy=[1,.5],status="reviewing",area_kind="coverage",
                                   initial_known=350,viewpoints=[],attempts=0)
        captured = []
        def provider(row,name,body):
            self.assertEqual(name,FRONTIER)
            captured.append(body["review"]["viewpoints"].copy())
            row["bindings"][FRONTIER] = {"binding":"test"}
            goal = [.4,0.] if len(captured)==1 else [0.,.4]
            return {"state":"WAYPOINT","goal":goal,"path":[[0.,0.],goal],"reason":"alternate_observation_view"}
        self.service.service = provider
        snapshot = {**self.rover.snapshot_value,"grid":self.grid}
        self.service.review(self.row,snapshot,{"pose":[0.,0.]})
        self.row["active"] = None
        self.service.review(self.row,snapshot,{"pose":[0.,0.]})
        self.assertIn([.4,0.],captured[1])
        self.assertEqual([g for _,g in self.rover.dispatched],[[.4,0.],[0.,.4]])

    def test_no_motion_through_unobserved_real_grid(self):
        for n in range(40):
            self.grid["cells"][n*40+23]=-1
        snap = {**self.rover.snapshot_value,"grid":self.grid}
        with self.assertRaisesRegex(ValueError,"segment_not_clear"):
            self.service.start_leg(self.row,snap,{"goal":[.65,0.],"reason":"unsafe"},"frontier")
        self.assertEqual(self.rover.dispatched,[])

    def test_navigation_wire_matches_agent_version_and_replays_old_operations(self):
        self.row["status"] = "SUCCEEDED"
        args = {"place":"教学楼门厅"}
        op = "visit/step/1"
        wire = dict(version="2", operation_id=op, arguments=args, trace={"task_id":"visit","parent_event_id":"e1"})
        step = dict(operation_id=op, tool="navigate_survey_place", args=args, version="1")
        record = dict(id="visit",text="前往教学楼门厅",state="RUNNING",steps=[step])
        with patch("exploration.survey_session.request",return_value=record):
            with self.assertRaisesRegex(ValueError,"agent_intent_unavailable"):
                self.service.poll("navigate",wire)
            step["version"] = "2"
            self.assertEqual(self.service.poll("navigate",wire)["state"],"RUNNING")
        recovered = Survey(self.rover,self.config,self.path,self.path.parent,"http://unused")
        self.assertEqual(recovered.rows["visit"]["tool_version"],"2")
        self.assertEqual(recovered.poll("navigate",wire)["state"],"RUNNING")
        with self.assertRaisesRegex(ValueError,"operation_identity_conflict"):
            recovered.poll("navigate",{**wire,"version":"1"})
        old = recovered.rows["visit"]
        old.pop("tool_version")
        old.update(status="SUCCEEDED",evidence={"observed_xy":[0,0]})
        self.assertEqual(recovered.poll("navigate",{**wire,"version":"1"})["evidence"],old["evidence"])

    def test_semantic_gate_validates_the_map_and_stop_before_any_route_request(self):
        ref = dict(id="campus",frame="odom",digest="a"*64,revision="a"*64)
        source = dict(kind="map_import", status="SUCCEEDED", map_ref=ref, area=[-2,-2,2,2],
                      coverage={},markers=[])
        self.service.rows["campus"] = source
        self.row.update(kind="navigate", target="门厅")
        marker = dict(entity_id="lobby", name="门厅", state="confirmed", entrance_pose=[1.,0,0])
        publication = dict(status="ok",map_ref=ref,grid=self.grid,semantic=[marker])
        snap = dict(grid=self.grid, stamp=14,pose={**self.rover.snapshot_value["pose"], "frame":"odom"})
        tamper = {}
        calls = []
        def provider(row, name, body):
            calls.append(name)
            if name == LOAD:
                return publication
            if name == ROUTE:
                return route(body)
            self.assertEqual(name,RESOLVE)
            return {**resolve_place(body), **tamper}
        self.service.service = provider
        for altered in (dict(map_ref={**ref,"revision":"f"*64}), dict(entrance_pose=[-1.,0.,0.]),
                        dict(candidates=[]), dict(entity_id="another")):
            with self.subTest(altered=altered):
                tamper.clear()
                tamper.update(altered)
                with self.assertRaisesRegex(ValueError,"semantic_"):
                    self.service.nav_step(self.row,snap)
        tamper.clear()
        self.row["target"] = "未登记地点"
        self.service.nav_step(self.row,snap)
        self.assertEqual(self.row["destination"]["state"],"unknown")
        self.row.update(target="门厅",status="RUNNING")
        self.service.nav_step(self.row,{**snap,"pose":{**snap["pose"],"frame":"other"}})
        self.assertEqual(self.row["destination"]["state"],"unavailable")
        self.row.update(status="RUNNING")
        self.grid["cells"][20*40+30] = 100
        self.service.nav_step(self.row,snap)
        self.assertEqual(self.row["status"],"BLOCKED")
        self.assertEqual(self.row["destination"]["state"],"unavailable")
        self.assertNotIn(ROUTE,calls)
        self.assertEqual(self.rover.dispatched,[])
        self.grid["cells"][20*40+30] = 0
        self.row.update(status="RUNNING")
        marker["entrance_pose"] = [0.,0.,0.]
        self.service.nav_step(self.row,snap)
        self.assertEqual(self.row["status"],"SUCCEEDED")
        self.assertEqual(self.row["evidence"]["destination"]["entity_id"],"lobby")
        self.assertEqual(self.rover.dispatched,[])

    def test_map_switch_and_missing_active_map_hold_the_original_navigation(self):
        self.row.update(kind="navigate",map_ref=dict(id="campus", revision="old"))
        self.service.rows["campus"] = dict(kind="map_import",status="SUCCEEDED",map_ref=dict(id="campus",revision="new"))
        self.service.nav_step(self.row,{})
        self.assertEqual(self.row["status"],"BLOCKED")
        self.config["navigation_map_id"] = "other-campus"
        self.row.update(map_ref=None,status="RUNNING")
        self.service.nav_step(self.row,{})
        self.assertEqual(self.row["destination"]["state"],"unavailable")
        self.assertEqual(self.rover.dispatched,[])

    def test_uninstalled_semantic_provider_blocks_with_a_visible_receipt_and_zero_motion(self):
        self.row.update(kind="navigate",target="门厅")
        ref = dict(id="campus",frame="odom",digest="a"*64,revision="a"*64)
        self.service.rows["campus"] = dict(kind="map_import",status="SUCCEEDED",map_ref=ref,
                                          area=[-2,-2,2,2],markers=[],coverage={})
        self.rover.snapshot_value.update(scan_m=1,grid=self.grid)
        self.rover.snapshot_value["pose"]["frame"] = "odom"
        from_catalog = self.service.service
        def available_map_only(row, name, body):
            if name == LOAD:
                return dict(status="ok",map_ref=ref,grid=self.grid,semantic=[])
            self.assertEqual(name,RESOLVE)
            return from_catalog(row,name,body)
        self.service.service = available_map_only
        for _ in range(5):
            self.service.next_tick = 0
            self.service.tick()
        self.assertEqual(self.row["status"],"BLOCKED")
        self.assertIn(RESOLVE,self.row["destination"]["reason"])
        self.assertEqual(self.row["destination"]["state"],"unavailable")
        self.assertEqual(self.rover.dispatched,[])

    def test_prefix_admission_binds_the_full_goal_and_checks_local_evidence_independently(self):
        self.config.update(execution_mode="continuous_path",observed_prefix=True)
        self.rover.dispatch_path = self.rover.dispatch
        self.row.update(loaded=deepcopy(self.grid),goal=[1.4,.4])
        self.row["bindings"][ROUTE] = {"binding":"test"}
        snap = {**self.rover.snapshot_value,"grid":self.grid}
        for idx in range(len(self.grid["cells"])):
            if idx%40>32:
                self.grid["cells"][idx] = -1
        plan = route(dict(published_grid=self.row["loaded"],live_grid=self.grid,pose=[0.,0.],
                          goal=self.row["goal"],bounds=self.row["area"],footprint_radius=.21,
                          max_step=.8,execution_mode="continuous_path",observed_prefix=True))
        with self.assertRaisesRegex(ValueError,"outside_corridor"):
            self.service.start_leg(self.row,snap,{**plan,"through_points":plan["global_path"]},"navigate")
        self.assertFalse(self.rover.dispatched)
        self.service.start_leg(self.row,snap,plan,"navigate")
        self.assertEqual(self.row["initial_global_path"]["points"][-1],self.row["goal"])
        self.assertNotEqual(self.row["active"]["goal"],self.row["goal"])
        self.assertEqual(self.row["events"][-1]["data"]["admitted_endpoint"],self.row["active"]["goal"])

    def test_continuous_admission_uses_both_grids_then_submits_one_terminal_operation(self):
        self.config["execution_mode"] = "continuous_path"
        self.rover.dispatch_path = self.rover.dispatch
        self.row.update(loaded=deepcopy(self.grid), goal=[1.4,.4])
        self.row["bindings"][ROUTE] = {"binding":"test"}
        snap = {**self.rover.snapshot_value, "grid":self.grid}
        plan = route(dict(published_grid=self.row["loaded"], live_grid=self.grid, pose=[0.,0.],
                          goal=self.row["goal"], bounds=self.row["area"], footprint_radius=.21,
                          max_step=.8, execution_mode="continuous_path"))
        points = plan["through_points"]
        self.assertGreater(len(points), 2)
        for key in ("grid","loaded"):
            grid = snap["grid"] if key == "grid" else self.row["loaded"]
            grid["cells"][24*40+34] = -1   # destination, far past the first segment
            with self.assertRaisesRegex(ValueError,"outside_corridor"):
                self.service.start_leg(self.row,snap,plan,"navigate")
            grid["cells"][24*40+34] = 0
        with self.assertRaisesRegex(ValueError,"continuous_path_invalid"):
            self.service.start_leg(self.row,snap,{**plan,"through_points":None},"navigate")
        self.assertFalse(self.rover.dispatched)
        self.service.start_leg(self.row,snap,plan,"navigate")
        self.assertEqual(self.rover.dispatched,[("task/leg/1",points)])
        self.assertEqual(self.row["active"]["goal"],self.row["goal"])
        self.assertEqual(self.row["frontier"],points[1])

    def test_alternative_proof_expiring_during_checkpoint_never_dispatches(self):
        self.config["execution_mode"] = "continuous_path"
        self.rover.dispatch_path = self.rover.dispatch
        self.row.update(loaded=deepcopy(self.grid),goal=[1.4,.4])
        self.row["bindings"][ROUTE] = {"binding":"test"}
        snap = {**self.rover.snapshot_value,"grid":self.grid}
        plan = route(dict(published_grid=self.grid,live_grid=self.grid,pose=[0.,0.],goal=self.row["goal"],
                          bounds=self.row["area"],footprint_radius=.21,max_step=.8,execution_mode="continuous_path"))
        recovery = dict(previous_goal=[.65,0.],boot="boot",operation_id="task/leg/0",deadline=time.time()+30,
                        proof_valid_until=time.monotonic()+.30,proof={"result":"clear"})
        write = self.service.event
        def expire(row,kind,detail,**data):
            if kind == "route_planned":
                recovery["proof_valid_until"] = 0.
            write(row,kind,detail,**data)
        self.service.event = expire
        self.service.start_leg(self.row,snap,plan,"navigate",recovery)
        self.assertEqual(self.rover.dispatched,[])
        self.assertIsNone(self.row["active"])
        self.assertEqual(self.row["phase"],"RECHECKING_CLEARANCE")
        self.assertEqual(self.row["events"][-1]["data"]["reason"],"proof_expired_before_dispatch")


if __name__ == "__main__":
    unittest.main()
