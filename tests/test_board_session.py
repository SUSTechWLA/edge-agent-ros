import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from calibration.board_session import Station
from calibration.clock_observer import Epoch, point_window, validate_environment


class StationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site=Path(__file__).resolve().parents[1]/"calibration/station"
        self.profile=json.loads((self.site/"gazebo-board.json").read_bytes())
        self.station=self.open()

    def open(self):
        return Station(self.profile,self.tmp.name,self.site/"plugins/runtime")

    def test_read_only_observation_preserves_previous_verified_revision(self):
        self.station.data["published"]={"revision":"prior","profile_sha256":self.station.fingerprint}
        self.station.data["tasks"]["read"]={"phase":"OBSERVING","poses":[],"bindings":{}}
        self.station.data["latest"]="read"
        self.station.active="read/step/1"
        status=self.station.status()
        self.assertEqual((status["state"],status["revision"],status["active_operation"]),("VERIFIED","prior",None))

    def test_same_operation_replays_without_starting_another_recording(self):
        op="cal-1/step/2"
        req=dict(operation_id=op,version="1",arguments={},trace={"task_id":"cal-1","parent_event_id":"dispatch"})
        self.station.data["ops"][op]={"operation_id":op,"kind":"record","arguments":{},
                                     "state":"SUCCEEDED","evidence":{"outcome":"new_sample_recorded"}}
        self.station.save()
        restarted=self.open()
        with patch.object(restarted,"intent",side_effect=AssertionError("unexpected dispatch")):
            self.assertEqual(restarted.poll("record",req)["state"],"SUCCEEDED")
            with self.assertRaises(ValueError):
                restarted.poll("fit",req)

    def test_clock_status_binds_verified_spatial_revision_epoch_and_latest_task(self):
        station=self.station
        station.data["published"]={"revision":"spatial","profile_sha256":station.fingerprint}
        station.data["last_clock_task"]="clock"
        station.data["published_clock"]={"task_id":"clock","revision":"temporal","clock_epoch":"boot",
            "spatial_revision":"spatial","profile_sha256":station.fingerprint,"bindings":{}}
        with patch.object(station,"service",return_value={"streams_fresh":True,"clock_epoch":"boot"}):
            self.assertEqual(station.clock_status()["state"],"VERIFIED")
        with patch.object(station,"service",return_value={"streams_fresh":True,"clock_epoch":"new-boot"}):
            self.assertEqual(station.clock_status()["state"],"STALE")
        station.data["last_clock_task"]="new-clock"
        self.assertNotIn("revision",station.clock_status())
        self.assertEqual(station.status()["revision"],"spatial")

    def test_missing_spatial_calibration_and_unrecorded_clock(self):
        with self.assertRaisesRegex(ValueError,"空间外参"):
            self.station.clock_operation("clock",{},"capture_clock","clock/step/1")
        self.station.data["published"]={"revision":"spatial","profile_sha256":self.station.fingerprint}
        result=self.station.clock_operation("clock",{},"fit_clock","clock/step/1")
        self.assertFalse(result["motion_window_ready"])

    def test_clock_reset_and_publisher_replacement_break_epoch(self):
        epoch=Epoch(["camera","left"])
        epoch.update("camera",1000,b"a",1.)
        epoch.update("left",1010,b"b",1.)
        begin=epoch.id
        epoch.update("camera",900,b"a",1.2)
        self.assertNotEqual(begin,epoch.id)
        self.assertNotIn("left",epoch.stamps)
        began_again=epoch.id
        epoch.update("camera",950,b"x",1.3)
        self.assertNotEqual(began_again,epoch.id)

    def test_per_point_time_extracts_eight_ms_slice_in_sensor_clock(self):
        import numpy as np
        header=100_000_000_000
        times=np.array([0,18,20,22,40])*1_000_000
        xyz=np.arange(15).reshape((-1,3))
        for cfg, stamp in (({"mode":"relative","unit":"ns"},times),
                           ({"mode":"absolute","unit":"s"},(times+header)/1e9)):
            signal_ns,selected=point_window(header,xyz,stamp,cfg)
            self.assertEqual(signal_ns,header+20_000_000)
            np.testing.assert_equal(selected,xyz[1:4])
        with self.assertRaisesRegex(ValueError,"not resolved"):
            point_window(header,xyz,np.zeros(5),{"mode":"relative","unit":"ns"})

    def test_hardware_capture_requires_deployment_opt_in(self):
        with patch.dict("os.environ",{},clear=True):
            with self.assertRaises(ValueError):
                validate_environment({"environment":"hardware"})
            validate_environment({"environment":"simulation"})

    def test_generic_detect_receipt_has_only_perception_facts(self):
        task={"goal_tool":"observe_target","poses":[],"bindings":{}}
        detection={"target":"campus_sign","detector":{"id":"sign"},
                   "detections":[{"label":"campus_sign","bbox_xyxy":[1,2,8,11]}],
                   "frame":"rgbd_optical","stamp_ns":42,"archive_sha256":"indexed-image"}
        with patch.object(self.station,"service",side_effect=[{"archive":"frame"},detection]):
            result=self.station.observe("read",task,"read/step/1","observe","campus_sign")
        self.assertEqual(set(result), set(detection)|{"outcome","detected"})

    def test_interrupted_record_requires_reconciliation_at_restart(self):
        self.station.data["last_calibration"]="cal"
        self.station.data["ops"]["cal/step/1"]={"state":"RUNNING","kind":"record"}
        self.station.save()
        self.assertEqual(self.open().status()["state"],"RECOVERY_REQUIRED")

    def test_declines_early_fit_and_verify_without_publishing(self):
        task={"profile":self.station.fingerprint,"poses":[],"bindings":{}}
        for name in ("fit","verify"):
            self.assertEqual(self.station.perform("cal",task,name,"cal/step/1",{})["outcome"],
                             "insufficient_distinct_poses")
        self.assertIsNone(self.station.status()["revision"])

    def test_camera_visible_but_lidar_missing_prompts_without_recording(self):
        task={"poses":[],"bindings":{}}
        calls={"frame":{"archive":"frame"},
               "detect":{"target":"checkerboard","detections":[{"label":"checkerboard"}],
                         "detector":{"id":"board","version":"test"}},
               "measure":{"condition":"board_missing_in_lidar","missing":["lidar_left"]}}
        with patch.object(self.station,"service",side_effect=lambda t,k,b:calls[k]), \
             patch("calibration.board_session.time.monotonic",side_effect=[0,0,1,1,14]), \
             patch("calibration.board_session.time.sleep"):
            result=self.station.observe("cal",task,"cal/step/1","record","checkerboard")
        self.assertEqual(task["phase"],"ADJUST_BOARD")
        self.assertEqual(task["capture_feedback"]["missing_sensors"],["lidar_left"])
        self.assertEqual(result["outcome"],"board_missing_in_lidar")
        self.assertFalse(task["poses"])


if __name__=="__main__":
    unittest.main()
