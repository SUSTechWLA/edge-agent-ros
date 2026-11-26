"""Spatial station lifecycle, physical geometry, timed paired capture and replay."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import cv2

from calibration import board_provider
from calibration.board_clip import Window
from calibration.board_session import Station
from system.four_hole_scene import K, POSES, PROFILE, SENSORS, cloud, render

SITE = Path(__file__).resolve().parents[1]/"calibration/station"


class CaptureTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.profile=dict(robot_id="bench",environment="simulation",calibration_method="four_hole_board",
                          solver={k:v for k,v in PROFILE.items() if k!="four_hole_board"})
        self.station=Station(self.profile,self.root,SITE/"plugins/runtime")
        self.task=dict(profile=self.station.fingerprint,poses=[],bindings={},goal_tool="verify_sensor_extrinsics")
        self.station.data["tasks"]["cal"]=self.task
        self.station.data["last_calibration"]="cal"

    def register(self):
        request = dict(board_id="fixture-01",confirmed=True,expected_profile_sha256=self.station.fingerprint,
                       metrics=deepcopy(PROFILE["four_hole_board"]))
        result = self.station.register_geometry(request)
        self.assertEqual(result["profile_sha256"],self.task["profile"])
        return request

    def test_registration_checks_and_recovery_bindings(self):
        self.assertFalse(self.station.status()["board_geometry"]["confirmed"])
        self.profile["four_hole_board"]=PROFILE["four_hole_board"]
        self.assertFalse(Station(self.profile,self.root,SITE/"plugins/runtime").geometry)
        del self.profile["four_hole_board"]
        request=self.register()
        reopened=Station(self.profile,self.root,SITE/"plugins/runtime")
        self.assertEqual(reopened.geometry,self.station.geometry)
        with self.assertRaisesRegex(ValueError,"reload_station"):
            self.station.register_geometry(request)
        request["expected_profile_sha256"]=self.station.fingerprint
        for metrics in ({},dict(request["metrics"],hole_diameter_m=float("nan")),
                        dict(request["metrics"],hole_spacing_m=[.1,.1])):
            with self.assertRaises(ValueError):
                self.station.register_geometry(dict(request,metrics=metrics))
        self.task.update(poses=[{"sha256":"some"}],phase="RECORDED")
        with patch.object(self.station,"agent_state",return_value="RUNNING"),self.assertRaisesRegex(ValueError,"in_use"):
            self.station.register_geometry(dict(request,board_id="different"))
        with self.assertRaisesRegex(ValueError,"时间偏移"):
            self.station.clock_operation("cal",self.task,"capture_clock","cal/step/2")

    def test_suggestions_are_never_commissioned_measurements(self):
        values=dict(marker_size_m=.2,hole_diameter_m=.24)
        hints=dict(suggested_metrics=values,suggestion_source="fixture nominal dimensions; check with ruler")
        profile={**self.profile,"four_hole_board":{}}
        baseline=Station(profile,self.root/"inline",SITE/"plugins/runtime")
        hinted=Station(dict(profile,four_hole_board=hints),self.root/"inline",SITE/"plugins/runtime")
        self.assertEqual(hinted.fingerprint,baseline.fingerprint)
        self.assertEqual(hinted.geometry_status()["suggested_metrics"],values)
        self.assertFalse(hinted.geometry)
        path=self.root/"hints.json"
        with patch.dict("os.environ",EDGE_CALIBRATION_BOARD_SUGGESTIONS=str(path)):
            record={"robot_id":"bench","calibration_method":"four_hole_board",**hints}
            path.write_text(json.dumps(record))
            self.assertEqual(self.station.geometry_status()["suggested_metrics"],values)
            path.write_text(json.dumps({**record,"robot_id":"other"}))
            self.assertNotIn("suggested_metrics",self.station.geometry_status())
            self.assertFalse(self.station.geometry_status()["confirmed"])

    def frames(self, pose, start, count=4, missing=False):
        axis,shift=POSES[pose]
        image = render(axis,shift)
        for i in range(count):
            ns = start+i*360_000_000
            clouds={k:cloud(axis,shift,k,seed=100+i).astype("float32") for k in SENSORS}
            if missing:
                clouds["right"]=clouds["right"][:0]
            frame=dict(stamp_ns=ns,stamps_ns={k:ns for k in ("camera","info",*SENSORS)},
                       frames={"camera":PROFILE["camera_frame"],**PROFILE["lidar_frames"]},rgb=image,k=K,clouds=clouds)
            yield board_provider.encode_frame(frame)

    def collect(self, frames, kind="record"):
        def service(task,name,body):
            solver=dict(self.profile["solver"])
            if self.station.geometry:
                solver["four_hole_board"]=self.station.geometry["metrics"]
            if name=="frame":
                return {"archive":next(frames)}
            if name=="detect":
                return board_provider.detect(body,solver,SITE/"recognizers")
            if name=="views":
                from calibration.view_planner import plan_views
                return plan_views(body["task_id"],body["operation_id"],body["observation"],body["camera"],
                                  body["prior"],dict(self.profile,solver=solver))
            return board_provider.measure(body,solver,self.station.method)
        with patch.object(self.station,"service",side_effect=service),patch.object(self.station,"intent"):
            return self.station.perform("cal",self.task,kind,"cal/step/2",{})

    def test_inspection_reports_one_paired_view_without_recording_or_validating(self):
        result=self.collect(self.frames(0,1000000000,1),kind="inspect")
        self.assertEqual(result["outcome"],"board_geometry_required")
        self.register()
        result=self.collect(self.frames(0,2000000000,1,missing=True),kind="inspect")
        self.assertEqual(result["scope"],"single_paired_frame")
        self.assertEqual(result["missing_lidar_targets"],"right")
        self.assertEqual(result["lidar_targets_seen"],"left")
        self.assertEqual(result["camera_marker_count"],4)
        self.assertEqual(sorted(result["camera_marker_ids"]), [1, 2, 3, 4])
        self.assertEqual(len(result["target_center_m"]), 3)
        self.assertEqual(len(result["target_normal"]), 3)
        self.assertIn("scene_slice", result)
        self.assertEqual(result["lidar_measurements"]["right"]["point_count"],0)
        self.assertEqual(result["camera_stamp_ns"],"2000000000")
        result=self.collect(self.frames(0,3000000000,1),kind="inspect")
        self.assertEqual(result["outcome"],"distinct_pose")
        self.assertEqual(result["lidar_targets_seen"],"left, right")
        self.assertFalse(result.get("verified"))
        self.assertFalse(self.task["poses"])
        self.assertFalse((self.root/"captures").exists())
        self.assertFalse(self.station.status()["revision"])

    def test_live_view_planning_with_missing_lidar_does_not_claim_a_new_sample(self):
        self.register()
        self.task.update(phase="PAUSED_FOR_OPERATOR")
        before = deepcopy(self.task)
        result = self.collect(self.frames(0, 2000000000, 1, missing=True), kind="plan-views")
        self.assertEqual(result["outcome"], "candidate_views")
        self.assertEqual(result["observation"]["missing_lidar_targets"], "right")
        self.assertEqual(result["source"]["task_id"], "cal")
        self.assertEqual(result["source"]["operation_id"], "cal/step/2")
        self.assertEqual(result["source"]["stamp_ns"], "2000000000")
        self.assertFalse(result["base_goal_available"])
        self.assertEqual(self.task, before)
        self.assertFalse((self.root/"captures").exists())

    def test_inspection_has_its_own_receipt_and_preserves_paused_capture(self):
        self.station.data["latest"]="cal"
        self.task.update(phase="PAUSED_FOR_OPERATOR",message="之前的采集仍可续办")
        streams={"right":{"fresh":False},"camera":{"fresh":True}}
        body=dict(operation_id="review/step/1",version="1",arguments={},
                  trace=dict(task_id="review",parent_event_id="event-1"))
        def inline_thread(*, target, args, daemon):
            class Inline:
                def start(self):
                    target(*args)
            return Inline()
        with patch.object(self.station,"intent",return_value="verify_sensor_extrinsics"), \
                patch.object(self.station,"agent_state",return_value="BLOCKED"), \
                patch("calibration.board_session.Thread",side_effect=inline_thread), \
                patch.object(self.station,"service",return_value={"archive":None,"streams":streams}) as snapshot:
            result=self.station.poll("inspect",body)
            self.assertEqual(self.station.poll("inspect",body), result)
            self.assertEqual(snapshot.call_count,1)
        self.assertEqual(result["evidence"]["outcome"],"no_synchronized_observation")
        self.assertEqual(result["evidence"]["stale_streams"],"right")
        self.assertNotIn("missing_lidar_targets",result["evidence"])
        self.assertEqual(self.station.data["latest"],"cal")
        self.assertEqual(self.station.status()["task_id"],"cal")
        self.assertEqual(self.station.status()["state"],"PAUSED_FOR_OPERATOR")

    def test_resume_after_absent_dimensions_missing_lidar_and_a_repeated_pose(self):
        with self.assertRaises(StopIteration):
            self.collect(self.frames(0,1000000000,1))
        feedback=self.task["capture_feedback"]
        self.assertEqual(feedback["condition"],"board_geometry_required")
        self.assertEqual(feedback["camera"]["marker_count"],4)
        self.assertTrue(self.station.media("/preview.jpg","cal"))
        self.assertFalse(self.task["poses"])
        self.register()
        with self.assertRaises(StopIteration):
            self.collect(self.frames(0,2000000000,missing=True))
        feedback=self.task["capture_feedback"]
        self.assertEqual(feedback["condition"],"board_missing_in_lidar")
        self.assertEqual(feedback["missing_sensors"],["right"])
        self.assertEqual(feedback["lidars"]["left"]["hole_count"],4)
        result=self.collect(self.frames(0,4000000000))
        self.assertEqual((result["accepted_poses"],result["required_poses"]),(1,5))
        row=self.task["poses"][0]
        folder=self.root/"captures"/"cal"
        manifest=json.loads((folder/(row["sha256"]+".json")).read_bytes())
        self.assertEqual(len(manifest["frames"]),4)
        for item in manifest["frames"]:
            raw=(folder/(item["sha256"]+".npz")).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),item["sha256"])
        path=folder/(manifest["video_sha256"]+".avi")
        player=cv2.VideoCapture(str(path))
        try:
            self.assertEqual(player.get(cv2.CAP_PROP_FRAME_COUNT),4)
            self.assertTrue(player.read()[0])
        finally:
            player.release()
        with self.assertRaises(StopIteration):
            self.collect(self.frames(0,6000000000))
        self.assertEqual(self.task["capture_feedback"]["condition"],"repeated_pose")
        self.assertEqual(len(self.task["poses"]),1)
        self.assertEqual(self.station.media("/capture.avi","cal","1"),path.read_bytes())
        self.assertIsNone(self.station.media("/capture.avi","cal","2"))
        self.assertIsNone(self.station.media("/capture.avi","other"))
        with self.assertRaises(ValueError):
            self.station.media("/capture.avi","../cal")
        restarted=Station(self.profile,self.root,SITE/"plugins/runtime")
        restored=restarted.status()["capture_feedback"]
        self.assertEqual(restored["preview_source"],"archive")
        self.assertEqual(restored["preview_stamp_ns"],manifest["frames"][-1]["stamps_ns"]["camera"])
        self.assertTrue(restarted.media("/preview.jpg","cal").startswith(b"\xff\xd8"))

    def test_motion_duplicate_or_time_gap_resets_capture(self):
        window=Window(self.station.method.spec["clip"])
        pose=dict(normal=[0,0,1],center_m=[0,0,1])
        def push(stamp,cam=pose):
            window.add("archive",{},b"jpg",(320,240),cam,dict(camera=stamp,left=stamp,right=stamp))
        push(100_000_000)
        push(600_000_000)
        push(600_000_000)
        self.assertEqual(len(window.frames),1)
        push(3_000_000_000)
        self.assertEqual(len(window.frames),1)
        push(3_500_000_000,dict(pose,center_m=[.1,0,1]))
        self.assertFalse(window.ready())
        self.assertEqual(len(window.frames),1)

    def test_idle_capture_pauses_with_original_pose_count(self):
        self.register()
        self.task["poses"].append(dict(sha256="prior",video="prior-video"))
        with patch("calibration.board_session.time.monotonic",side_effect=(0,0,181)):
            result=self.station.capture_board("cal",self.task,"cal/step/3","record")
        self.assertTrue(result["paused_for_operator"])
        self.assertEqual(result["accepted_poses"],1)
        self.assertFalse(result.get("verified"))
        self.assertEqual(self.station.status()["state"],"PAUSED_FOR_OPERATOR")
        self.assertIsNone(self.station.status()["revision"])

    def test_single_lidar_guidance_and_new_task_adopts_installed_adapter_revision(self):
        directory=self.root/"methods"
        directory.mkdir()
        path=directory/"four_hole_board.json"
        path.write_bytes((SITE/"methods"/path.name).read_bytes())
        (directory/"broken.json").write_text("{}")
        profile={**self.profile,"solver":{"camera_frame":"rgb","lidar_frames":{"front":"front_mid360"}}}
        with patch.dict("os.environ",EDGE_CALIBRATION_METHOD_DIR=str(directory)):
            station=Station(profile,self.root/"front",SITE/"plugins/runtime")
            self.assertIn("前雷达（front）",station.status()["message"])
            self.assertEqual(len(station.status()["methods"]),1)
            sha=station.method.sha256
            path.write_bytes(path.read_bytes()+b"\n")
            self.assertFalse(station.status()["method_available"])
            with self.assertRaisesRegex(ValueError,"method_changed"):
                station.service({"bindings":{},"method_sha256":sha},"fit",{})
            req=dict(operation_id="new/step/1",arguments={},version="1",
                     trace={"task_id":"new","parent_event_id":"authorized"})
            with patch.object(station,"intent",return_value="verify_sensor_extrinsics"),patch("threading.Thread.start"):
                station.poll("status",req)
            self.assertTrue(station.status()["method_available"])
            self.assertNotEqual(station.method.sha256,sha)

    def test_restart_recovers_only_a_proven_read_only_geometry_wait(self):
        op="cal/step/2"
        self.task.update(phase="WAITING_FOR_GEOMETRY",method_sha256=self.station.method.sha256,
                         capture_feedback=dict(condition="board_geometry_required"))
        self.station.data["ops"][op]=dict(operation_id=op,kind="record",state="RUNNING",arguments={})
        self.station.save()
        original=self.station.path.read_bytes()
        restarted=Station(self.profile,self.root,SITE/"plugins/runtime")
        row=restarted.data["ops"][op]
        self.assertEqual(row["state"],"SUCCEEDED")
        self.assertEqual((row["evidence"]["verified"],row["evidence"]["accepted_poses"]),(False,0))
        self.assertTrue(row["evidence"]["paused_for_operator"])
        self.assertEqual(restarted.status()["state"],"PAUSED_FOR_OPERATOR")
        for changed in ("recording","artifact","different_method","candidate"):
            data=json.loads(original)
            task=data["tasks"]["cal"]
            if changed=="recording":
                task["phase"]="RECORDING"
            elif changed=="different_method":
                task["method_sha256"]="other"
            elif changed=="candidate":
                task["candidate"]={"transforms":{"front":{}}}
            else:
                folder=self.root/"captures"/"cal"
                folder.mkdir(parents=True,exist_ok=True)
                (folder/"unfinished.npz").write_bytes(b"partial")
            self.station.path.write_text(json.dumps(data))
            reopened=Station(self.profile,self.root,SITE/"plugins/runtime")
            self.assertEqual(reopened.data["ops"][op]["state"],"RECOVERY_REQUIRED")
            if changed=="artifact":
                (folder/"unfinished.npz").unlink()
