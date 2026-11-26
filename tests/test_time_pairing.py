import unittest
from calibration.time_pairing import assess_pair


class PairingTest(unittest.TestCase):
    def setUp(self):
        self.binding = dict(robot_id="rover", environment="simulation", profile_sha256="profile",
                            spatial_revision="mount", clock_epoch="boot-a", revision="clock")
        source = dict(offset_ns=-40, uncertainty_ns=2, valid_raw_ns=[900, 1200])
        self.report = dict(self.binding, state="VERIFIED", sources={"front": source})
        self.request = dict(self.binding, source="front", lidar_stamp_ns=1040, camera_stamp_ns=1001,
                            lidar_radius_ns=1, camera_radius_ns=2, budget_ns=10)

    def test_corrected_stamps_and_acquisition_window(self):
        result = assess_pair(self.report, self.request, 10)
        self.assertTrue(result["accepted"])
        self.assertEqual((result["corrected_lidar_ns"], result["temporal_error_bound_ns"]), (1000, 6))
        self.assertFalse(assess_pair(self.report, dict(self.request, lidar_radius_ns=100), 10)["accepted"])
        self.assertFalse(assess_pair(self.report, self.request, 5)["accepted"])

    def test_no_extrapolation_or_substituting_revisions(self):
        for key in self.binding:
            with self.subTest(key=key):
                self.assertEqual(assess_pair(self.report, dict(self.request, **{key:"changed"}), 10)["field"], key)
        for change in (dict(lidar_stamp_ns=900), dict(camera_stamp_ns=1300), dict(source="rear")):
            self.assertFalse(assess_pair(self.report, dict(self.request, **change), 10)["accepted"])
        self.assertFalse(assess_pair(dict(self.report, state="NEEDS_CALIBRATION"), self.request, 10)["accepted"])

    def test_rejects_missing_exposure_or_scan_radius(self):
        for key in ("camera_radius_ns", "lidar_radius_ns"):
            self.request.pop(key)
            with self.assertRaises(ValueError):
                assess_pair(self.report, self.request, 10)
            self.request[key] = 0


if __name__ == "__main__":
    unittest.main()
