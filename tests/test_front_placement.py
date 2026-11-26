import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calibration import front_placement
from calibration.board_session import Station


class FrontPlacementTest(unittest.TestCase):
    def setUp(self):
        directory=Path(__file__).resolve().parents[1]/"calibration/station"
        self.profile=json.loads((directory/"gazebo-fixed-rover.json").read_bytes())
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.station=Station(self.profile,tmp.name,directory/"plugins/runtime")
        self.task={"poses":[],"bindings":{},"profile":self.station.fingerprint,"placement_operation":"cal/step/1"}
        centers=[[-.55,-.45],[.55,-.45],[.55,.25],[-.55,.25]]
        corners=[[x+dx,y+dy,2.93] for x,y in centers for dx,dy in [(-.1,-.1),(.1,-.1),(.1,.1),(-.1,.1)]]
        holes=[[-.25,-.3,2.93],[.25,-.3,2.93],[.25,.1,2.93],[-.25,.1,2.93]]
        self.obs={"camera":{"center_m":[0.,-.1,2.93],"normal":[0.,0.,1.],"holes_m":holes,"all_marker_corners_m":corners},
                  "lidars":{"front":{"center_m":[2.,0.,0.],"normal":[1,0,0],"holes_m":holes}}}
        self.inspect=dict(camera_frame=self.profile["camera"]["frame"],camera_pose_observed=True,camera_detections=1,
                          camera_marker_ids=[1,2,3,4],archive_sha256="a"*64,camera_stamp_ns="100",
                          lidar_measurements={"front":{"condition":"board_observed","hole_count":4,"stamp_ns":100}})

    def test_stable_pairs_in_both_sensors_with_an_independent_timestamp_are_required(self):
        _,sample=front_placement.placement_sample(self.profile,self.inspect,self.obs)
        self.assertAlmostEqual(sample["estimated_bottom_z_base_m"],0.)
        self.assertFalse(front_placement.same_placement(sample,sample,self.station.method.spec["clip"]))
        def observation(task,*,placement_observation):
            result=copy.deepcopy(self.inspect)
            result["archive_sha256"]=str(probes.call_count%10)*64
            if probes.call_count==3:
                result["camera_stamp_ns"]="1000000100"
                result["lidar_measurements"]["front"]["stamp_ns"]=1000000100
            placement_observation.update(copy.deepcopy(self.obs))
            return result
        with patch.object(self.station,"inspect_view",side_effect=observation) as probes, \
                patch.object(self.station,"intent"), patch("calibration.board_session.time.sleep"):
            receipt=self.station.perform("cal",self.task,"inspect","cal/step/1",{})
        self.assertTrue(receipt["front_placement_confirmed"])
        self.assertEqual(probes.call_count,3)
        self.assertEqual(self.task["poses"],[])
        self.assertEqual(self.task["placement_receipt"]["operation_id"],"cal/step/1")

    def test_partial_sensors_wrong_facing_raised_or_noncentral_board_do_not_confirm(self):
        for changes,geometry in [({"camera_marker_ids":[1,3,4]},{}),({"camera_frame":"other"},{}),
                ({"lidar_measurements":{"front":{"condition":"board_observed","hole_count":2}}},{}),
                ({},{"center_m":[.7,-.1,2.93]}),({},{"center_m":[0.,-.4,2.93]}),({},{"normal":[1.,0.,0.]})]:
            obs=copy.deepcopy(self.obs)
            obs["camera"].update(geometry)
            _,sample=front_placement.placement_sample(self.profile,dict(self.inspect,**changes),obs)
            self.assertIsNone(sample)
        with patch.object(self.station,"inspect_view",return_value={"front_placement_confirmed":False}) as read:
            self.assertFalse(self.station.perform("cal",self.task,"inspect","cal/step/2",{})["front_placement_confirmed"])
            read.assert_called_once()
