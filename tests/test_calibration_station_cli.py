"""Unit tests for the unified calibration station launcher and hardware profile validator."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from calibration.hardware_profile import validate_board_metrics, validate_hardware_profile
from scripts import calibration_station as station


def valid_hw_profile():
    return {
        "environment": "hardware",
        "robot_id": "jetson-lingao-01",
        "clock_domain": "jetson_monotonic",
        "calibration_method": "four_hole_board",
        "four_hole_board": {
            "confirmed": True,
            "board_id": "four-hole-1400x1000",
            "marker_size_m": 0.20,
            "marker_spacing_m": [1.10, 0.70],
            "hole_spacing_m": [0.50, 0.40],
            "hole_diameter_m": 0.24,
        },
        "camera": {
            "id": "d435_color", "serial": "123456789", "frame": "camera_color_optical_frame",
            "topic": "/camera/color/image_raw", "info_topic": "/camera/color/camera_info",
            "image_geometry": "raw",
        },
        "lidars": [
            {"id": "front", "serial": "3JEDL0001", "frame": "front_mid360",
             "topic": "/livox/lidar", "message_type": "sensor_msgs/msg/PointCloud2"}
        ],
        "max_skew_ns": 25_000_000,
        "solver": {"camera_frame": "camera_color_optical_frame", "lidar_frames": {"front": "front_mid360"}},
    }


class CalibrationStationCliTest(unittest.TestCase):
    def test_simulation_plans_and_preflight_pass_out_of_the_box(self):
        for mode, port, expected_count in (("sim-stationary", "8911", 4), ("sim-mobile", "8916", 5)):
            plan = station.build_plan(mode, env={})
            self.assertEqual(len(plan["compose_files"]), expected_count)
            self.assertIn(port, plan["console_url"])
            report = station.preflight(plan, env={})
            self.assertTrue(report["ok"], report)

    def test_hardware_preflight_derives_robot_id_and_catches_missing_env_or_mm_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof_path = Path(tmp) / "installed-sensors.json"
            prof = valid_hw_profile()
            prof_path.write_text(json.dumps(prof), encoding="utf-8")

            plan = station.build_plan("hardware", profile_path=prof_path, passive_livox=True, env={})
            self.assertIn("compose.calibration-passive-livox.yaml", plan["compose_files"])
            self.assertEqual(plan["compose_profiles"], [])

            missing = station.preflight(plan, env={})
            self.assertFalse(missing["ok"])
            self.assertTrue(any("EDGE_CAL_HOST_GATEWAY" in msg for msg in missing["issues"]))

            hw_env = {
                "EDGE_CAL_HOST_GATEWAY": "172.17.0.1",
                "EDGE_CAL_ROS_DOMAIN": "61",
                "EDGE_CAL_OLLAMA_URL": "http://172.17.0.1:11434",
                "EDGE_CAL_HW_OBSERVER_IMAGE": "edge-agent-ros:hardware-calibration-observer",
                "EDGE_CAL_LIVOX_IFACE": "eth0",
                "EDGE_CAL_LIVOX_DEST": "192.168.1.5",
                "EDGE_CAL_LIVOX_IP": "192.168.1.105",
                "EDGE_CAL_LIVOX_ID": "front",
                "EDGE_CAL_LIVOX_FRAME": "front_mid360",
            }
            ready = station.preflight(plan, env=hw_env)
            self.assertTrue(ready["ok"], ready)
            self.assertEqual(ready["derived_env"]["EDGE_CAL_ROBOT_ID"], "jetson-lingao-01")

            # Enabling --with-clock requires exposure and per-point timing fields.
            clock_plan = station.build_plan("hardware", profile_path=prof_path, with_clock=True, env=hw_env)
            self.assertEqual(clock_plan["compose_profiles"], ["time-calibration"])
            clock_report = station.preflight(clock_plan, env=hw_env)
            self.assertFalse(clock_report["ok"])
            self.assertTrue(any("exposure" in msg for msg in clock_report["issues"]))

            # Accidentally entering millimetres (200 instead of 0.20 m) is rejected before startup.
            bad = valid_hw_profile()
            bad["four_hole_board"]["marker_size_m"] = 200
            with self.assertRaisesRegex(ValueError, "metres"):
                validate_hardware_profile(bad, require_clock=False)

            bad_method = valid_hw_profile()
            bad_method["calibration_method"] = "nonexistent_board"
            with self.assertRaisesRegex(ValueError, "unknown calibration_method"):
                validate_board_metrics(bad_method)

    def test_hardware_compose_gates_clock_observer_behind_profile(self):
        raw = (ROOT / "compose.calibration-hardware.yaml").read_text(encoding="utf-8")
        self.assertIn('profiles: ["time-calibration"]', raw)
        self.assertIn("EDGE_CALIBRATION_PROFILE: /etc/edge/installed-sensors.json", raw)


if __name__ == "__main__":
    unittest.main()
