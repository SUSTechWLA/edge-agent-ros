"""SDD-032. Commissioning cannot combine evidence from different sessions or frozen clocks."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"client"))
import hardware_preflight as probe


class BoardPreflight(unittest.TestCase):
    def setUp(self):
        self.config=probe.configuration({})
        self.state={"mode":"live","environment":"hardware","robot_id":"hardware-edge","now":10.,
                    "session_id":"first","sources":{"odom":{"topic":"/odom","fresh":True,"available":True,
                        "stamp_s":10.,"ttl_s":1.,"data":{"frame":"odom","child_frame":"base_link"}}}}

    def test_stationary_frame_valid_but_time_must_advance(self):
        def read(*args,**kwargs):
            return io.BytesIO(json.dumps(self.state).encode())
        with patch.object(probe,"build_opener") as opener:
            opener.return_value.open.side_effect=read
            report=probe.check(self.config,["odom"],.30)
        self.assertFalse(report["passed"])
        self.assertEqual(report["issues"],["odom.stamp_not_advancing"])

    def test_restarted_console_cannot_splice_two_fresh_stamps(self):
        second={**self.state,"session_id":"second"}
        with patch.object(probe,"build_opener") as opener:
            opener.return_value.open.side_effect=[io.BytesIO(json.dumps(s).encode()) for s in (self.state,second)]
            report=probe.check(self.config,["odom"],1)
        self.assertEqual(report["issues"],["console.session_changed"])

    def test_other_robot_or_simulation_is_not_this_board(self):
        for patch_state in ({"mode":"replay"},{"robot_id":"another"},{"environment":"simulation"}):
            issues,_=probe.inspect({**self.state,**patch_state},self.config,["odom"])
            self.assertIn("console.identity_mismatch",issues)

    def test_configuration_fails_before_ros_starts(self):
        for val in ({"ROS_DOMAIN_ID":"255"},{"EDGE_HTTP_PORT":"65536"},{"EDGE_IMU_TOPIC":"/odom"},
                    {"EDGE_BASE_FRAME":"/base"},{"EDGE_SCAN_TOPIC":"unresolved"}):
            with self.subTest(val=val),self.assertRaises(ValueError):
                probe.configuration(val)


if __name__=="__main__":
    unittest.main()
