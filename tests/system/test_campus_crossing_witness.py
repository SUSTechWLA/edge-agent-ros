"""Negative controls for surface association and transverse velocity grading."""
import unittest

from campus_local_plan_witness import matching_face, static_dynamic_behavior, track_errors


class CrossingWitnessTests(unittest.TestCase):
    def test_report_must_match_bearing_and_range_on_one_visible_face(self):
        faces=[{"shape":"cart","range_m":1.61,"bearing_rad":.75}]
        self.assertIs(matching_face(faces,1.607,.751),faces[0])
        self.assertIsNone(matching_face(faces,1.607,-.75))
        self.assertIsNone(matching_face(faces,1.9,.75))
        # A second raycast face within one resolution bin can match only at its own angle.
        rail={"shape":"rail","range_m":1.615,"bearing_rad":-.75}
        self.assertIs(matching_face([*faces,rail],1.616,-.75),rail)
        self.assertIsNone(matching_face([*faces,rail],1.616,0.))

    def test_transverse_truth_rejects_static_and_opposing_tracks(self):
        cart={"xy":(7.65,.3),"yaw":0.,"vx":0.,"vy":.13}
        track={"x_m":1.45,"y_m":.3,"vx_mps":0.,"vy_mps":.13}
        def errors():
            return track_errors(track,cart,(6.,0.),0.,(.40,.50,.65))
        position,speed,vector=errors()
        self.assertLess(position,.001)
        self.assertAlmostEqual(speed,0.)
        self.assertAlmostEqual(vector,0.)
        for wrong in (0.,-.13,.30):
            track["vy_mps"]=wrong
            self.assertGreater(abs(errors()[1]),.075)
        track.update(x_m=1.45,y_m=-1.15,vy_mps=.13)
        self.assertGreater(errors()[0],.10)

    def test_static_scene_permits_only_a_zero_velocity_initial_observation(self):
        reports={"a":{"stamp_ns":100_000_000},"b":{"stamp_ns":1_300_000_000}}
        wait={"report_ref":"a","dynamic_phase":"observing","decision":"wait",
              "linear_mps":0.,"angular_radps":0.,"reason_code":"navigation.dynamic_observing"}
        moving={"report_ref":"b","decision":"forward"}
        self.assertTrue(static_dynamic_behavior([], [wait,moving],reports)["passed"])
        self.assertFalse(static_dynamic_behavior([{"code":"navigation.dynamic_yield"}],[wait,moving],reports)["passed"])
        self.assertFalse(static_dynamic_behavior([],[wait,{**wait,"report_ref":"b"}],reports)["passed"])
        self.assertFalse(static_dynamic_behavior([],[{**wait,"linear_mps":.1},moving],reports)["passed"])

    def test_static_ttc_pause_requires_matching_report_and_zero_output(self):
        reports={"s":{"stamp_ns":3_000_000_000,"ttc_status":"unobserved","ttc_s":None}}
        wait={"report_ref":"s","reason_code":"navigation.ttc_unobserved","decision":"wait",
              "linear_mps":0.,"angular_radps":0.,"ttc_s":None}
        self.assertTrue(static_dynamic_behavior([],[wait],reports)["passed"])
        self.assertFalse(static_dynamic_behavior([],[{**wait,"linear_mps":.1}],reports)["passed"])
        reports["s"].update(ttc_status="closing",ttc_s=1.5)
        self.assertFalse(static_dynamic_behavior([],[wait],reports)["passed"])


if __name__=="__main__":
    unittest.main()
