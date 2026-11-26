"""Current stream metadata is evidence about a published camera model, not an intrinsic fit."""
from copy import deepcopy
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock

import numpy as np

from calibration.board_provider import encode_frame, observation, stream_camera_model
from calibration.board_ros import published_camera_info
from calibration.board_session import camera_model_receipt


class CameraModelTest(unittest.TestCase):
    def setUp(self):
        self.camera = dict(id="rgb", frame="optical", topic="/color", info_topic="/info", image_geometry="rectified")
        self.profile = dict(camera=self.camera, robot_id="bench", environment="simulation")
        self.info = SimpleNamespace(width=8,height=6,k=[6,0,3,0,6,2,0,0,1],
                                    p=[5,0,3.5,0,0,5.5,2,0,0,0,1,0],d=[.1,0,0,0,0],distortion_model="plumb_bob",
                                    binning_x=0,binning_y=0,roi=SimpleNamespace(x_offset=0,y_offset=0,width=0,
                                                                              height=0,do_rectify=False))
        self.frame = dict(rgb=np.zeros((6,8,3),np.uint8),k=np.array(self.info.p).reshape(3,4)[:,:3].ravel(),
                          frames={"camera":"optical"},stamp_ns=85,stamps_ns={"camera":85,"info":84},
                          received_unix_ns=time.time_ns(),camera_info=published_camera_info(self.info),clouds={})
        self.body = dict(operation_id="inspect/step/3",version="1",arguments={},
                         trace=dict(task_id="inspect",parent_event_id="evt"))

    def test_rectified_and_raw_input_have_different_projection(self):
        model=stream_camera_model(self.profile,self.frame)
        self.assertEqual(model["fx_px"],5)
        self.assertEqual(model["published_camera_info"]["k"][0],6)
        self.assertEqual(model["reported_d"],self.info.d)
        self.assertEqual(model["published_d_scope"],"raw_image_camera_info")
        self.assertFalse(model["independently_calibrated"])
        self.assertEqual(model["image_stamp_ns"],"85")
        self.assertEqual(model["info_stamp_ns"],"84")
        self.camera["image_geometry"]="raw"
        with self.assertRaisesRegex(ValueError,"differs"):
            stream_camera_model(self.profile,self.frame)
        self.frame["k"]=np.array(self.info.k)
        self.assertEqual(stream_camera_model(self.profile,self.frame)["fx_px"],6)

    def test_unsupplied_distortion_and_nontrivial_roi(self):
        self.info.d=[]
        self.info.roi.width,self.info.roi.height=3,2
        self.frame["camera_info"]=published_camera_info(self.info)
        model=stream_camera_model(self.profile,self.frame)
        self.assertIsNone(model["reported_d"])
        self.assertFalse(model["full_frame_geometry"])
        self.assertEqual(model["factory_provenance"],"unknown")
        with observation({"archive":encode_frame(self.frame)}) as (_, meta, _):
            self.assertEqual(meta["camera_info"],self.frame["camera_info"])
        del self.frame["camera_info"]
        with observation({"archive":encode_frame(self.frame)}) as (_, meta, _):
            self.assertNotIn("camera_info",meta)

    def test_regular_operation_binds_sensor_identity_and_observation_freshness(self):
        station=Mock()
        station.profile=self.profile
        model=stream_camera_model(self.profile,self.frame)
        station.service.return_value=model
        receipt=camera_model_receipt(self.body,station)
        self.assertEqual(receipt["operation_id"],self.body["operation_id"])
        self.assertTrue(receipt["evidence"]["available"])
        self.assertEqual(receipt["evidence"]["source_geometry"],"rectified")
        station.service.return_value=dict(model,received_unix_ns=str(time.time_ns()-2_000_000_000))
        self.assertEqual(camera_model_receipt(self.body,station)["evidence"]["outcome"],"camera_model_stale")
        station.service.return_value=dict(available=False)
        self.assertFalse(camera_model_receipt(self.body,station)["evidence"]["available"])
        station.service.return_value=dict(model,robot_id="other")
        with self.assertRaisesRegex(ValueError,"mismatch"):
            camera_model_receipt(self.body,station)
        malformed=deepcopy(self.body)
        malformed["trace"]["task_id"]="other"
        with self.assertRaisesRegex(ValueError,"invalid_camera_model_operation"):
            camera_model_receipt(malformed,station)
