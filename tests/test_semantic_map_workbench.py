"""SDD-063 read-only semantic map and calibration corridor status projection tests."""
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from edge_contracts.semantic_costmap import validate_semantic_layers
from edge_contracts.semantic_scene import compact_scene
from exploration.survey_session import Survey

try:
    from calibration.board_session import Station, _compact_view_plan
except ModuleNotFoundError as exc:
    if exc.name not in ("numpy", "cv2"):
        raise
    Station = _compact_view_plan = None


def _sample_grid():
    width, height = 24, 20
    cells = [0] * (width * height)
    for x in range(width):
        cells[x] = 100
        cells[(height - 1) * width + x] = 100
    for y in range(height):
        cells[y * width] = 100
        cells[y * width + (width - 1)] = 100
    cells[10 * width + 12] = 100
    cells[2 * width + 2] = -1
    return {
        "width": width,
        "height": height,
        "resolution": 0.2,
        "origin": [-2.4, -2.0, 0.0],
        "frame": "odom",
        "cells": cells,
    }


def _sample_view_plan_reply():
    digest = "a" * 64
    return {
        "planner_version": "1.5.0",
        "outcome": "views_ranked",
        "stamp_ns": 1700000000123456789,
        "reachability_checked": True,
        "base_goal_available": True,
        "captured_lateral_left_m": 0.02,
        "captured_lateral_right_m": 0.34,
        "captured_rgb_cells": "middle_center, middle_right",
        "rejected": {"back_facing": 2, "outside_observed_clearance": 3},
        "navigation_context": {
            "stamp_ns": 1700000000123456789,
            "robot": {"x": 0.0, "y": 0.0, "yaw": 0.0, "epoch": "boot-1"},
            "nearest_observed_m": {
                "front": 1.65, "front_left": 1.4, "left": 1.2, "rear_left": 1.1,
                "rear": 1.5, "rear_right": 1.3, "right": 1.25, "front_right": 1.45,
            },
            "target": {"position_m": [1.55, 0.05, 0.42], "range_m": 1.55, "bearing": "front"},
        },
        "scene_slice": compact_scene({
            "version": 1,
            "scope": "sensor_snapshot",
            "frame": "camera_color_optical_frame",
            "source_ref": digest,
            "stamp_ns": "1700000000123456789",
            "map_ref": None,
            "nodes": [
                {"id": "target", "label": "four_hole_board", "kind": "object", "state": "observed",
                 "position_m": [0.02, -0.05, 1.52], "plane_normal": [0.0, 0.0, -1.0]},
                {"id": "camera", "label": "camera_color_optical_frame", "kind": "sensor", "state": "observed"},
                {"id": "lidar.front", "label": "livox_front", "kind": "sensor", "state": "observed"},
                {"id": "capture_zone", "label": "active_capture_corridor", "kind": "region", "state": "observed",
                 "position_m": [0.02, -0.05, 1.52]},
            ],
            "edges": [
                ["target", "observed_by", "camera"],
                ["target", "observed_by", "lidar.front"],
                ["target", "guards", "capture_zone"],
            ],
            "omitted": 0,
        }),
        "view_options": [
            {
                "ref": "goal-left-1",
                "path": [[0.0, 0.0], [0.18, 0.36]],
                "base_xy_yaw": [0.18, 0.36, -0.24],
                "end_xyz_m": [0.18, 0.36, 0.0],
                "end_heading_deg": -13.8,
                "board_lateral_m": -0.35,
                "clearance_margin_m": 0.31,
                "corridor_risk": 0.11,
                "preferred_zone": 0.89,
                "semantic_cost": 1.44,
                "new_rgb_cells": 3,
                "fills_missing_side": True,
                "secret_internal_blob": "must_not_leak",
            }
        ],
    }


class SemanticMapWorkbenchTest(unittest.TestCase):
    def test_survey_show_projects_validated_semantic_costmap_monotonic_veto_and_ttl_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            rover = SimpleNamespace(snapshot=lambda: None, observations=[], motion_events=[], action_results={})
            cfg = {
                "robot_id": "rover-1",
                "calibration_id": "cal-1",
                "radius": 0.22,
                "max_step": 0.6,
                "max_goals": 12,
                "max_refocus": 2,
                "timeout_s": 120,
                "semantic_ttl_s": 12.0,
                "area": {"aliases": ["一楼门厅"], "bounds": [-2.2, -1.8, 2.2, 1.8]},
            }
            survey = Survey(rover, cfg, Path(tmp) / "sessions.json", Path(tmp), "http://127.0.0.1:9")
            row = survey.create("task-sem-1", "task-sem-1/step/1", "探索一楼门厅", {"task_id": "task-sem-1"}, "survey", "一楼门厅")
            row["area"] = [-2.2, -1.8, 2.2, 1.8]
            row["grid"] = _sample_grid()
            row["pose"] = {"x": -0.6, "y": 0.0, "yaw": 0.15, "speed": 0.12, "yaw_rate": 0.0, "frame": "odom"}
            row["scene_geometry"] = {
                "frame": "odom",
                "zones": [
                    {"id": "lobby", "label": "一楼门厅", "kind": "indoor", "bounds": [-2.2, -1.8, 0.4, 1.8]},
                    {"id": "door", "label": "门洞过渡区", "kind": "transition", "bounds": [0.4, -0.8, 1.0, 0.8]},
                    {"id": "plaza", "label": "北侧步道", "kind": "outdoor", "bounds": [1.0, -1.8, 2.2, 1.8]},
                ],
                "features": [
                    {"id": "cone-line", "label": "施工围栏", "kind": "barrier", "xy": [0.8, 1.0], "size": [0.4, 0.4, 0.8]},
                ],
            }
            row["markers"] = [
                {"id": "sign-lobby", "label": "门厅导览牌", "x": -0.4, "y": 0.2, "status": "confirmed", "entrance": [-0.6, 0.0, 0.0]},
            ]
            row["avoid"] = [{"center": [1.4, -0.4], "radius": 0.38}]
            row["anomaly"] = {"id": "review-1", "label": "门牌待补扫", "xy": [1.2, 0.6], "status": "reviewing"}
            row["updated"] = time.time()

            snap = survey.show("task-sem-1")
            mission = snap["mission"]
            self.assertNotIn("semantic_layers", mission)
            sem = mission["semantic_costmap"]
            scene = mission["semantic_scene"]
            cell_count = row["grid"]["width"] * row["grid"]["height"]
            validated = validate_semantic_layers(
                {
                    "stamp_ns": int(sem["stamp_ns"]),
                    "valid_until_ns": int(sem["valid_until_ns"]),
                    "source_ref": sem["source_ref"],
                    "risk": sem["risk"],
                    "traversable": sem["traversable"],
                    "preferred": sem["preferred"],
                },
                cell_count,
                now_ns=int(sem["stamp_ns"]),
            )
            self.assertFalse(validated["stale"])
            self.assertFalse(sem["stale"])
            self.assertGreater(sem["summary"]["geometric_blocked_cells"], 0)
            self.assertGreater(sem["summary"]["semantic_vetoed_cells"], 0)
            self.assertTrue(sem["summary"]["robot_on_preferred"])
            # Obstacle cell inside indoor lobby is still monotonically blocked
            obstacle_idx = 10 * 24 + 12
            self.assertEqual(sem["risk"][obstacle_idx], 1.0)
            self.assertEqual(sem["traversable"][obstacle_idx], 0.0)
            # Validate compact_scene contract and relational edges
            self.assertEqual(compact_scene(scene), scene)
            relations = {edge[1] for edge in scene["edges"]}
            self.assertIn("inside", relations)
            self.assertIn("attached_to", relations)
            self.assertIn("guards", relations)
            self.assertLess(len(json.dumps(snap).encode("utf-8")), 512 * 1024)

            # Force TTL expiration: semantic soft-veto must lift while geometric blocks remain
            row["updated"] = time.time() - 30.0
            survey._semantic_cache.clear()
            expired_sem = survey.show("task-sem-1")["mission"]["semantic_costmap"]
            self.assertTrue(expired_sem["stale"])
            self.assertEqual(expired_sem["ttl_remaining_s"], 0.0)
            self.assertEqual(expired_sem["summary"]["semantic_vetoed_cells"], 0)
            self.assertEqual(
                expired_sem["summary"]["admitted_cells"],
                expired_sem["summary"]["known_free_cells"],
            )
            self.assertEqual(
                expired_sem["summary"]["geometric_blocked_cells"],
                sem["summary"]["geometric_blocked_cells"],
            )

    def test_survey_navigation_plan_forwards_explicit_semantic_layers_and_tracks_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            rover = SimpleNamespace(snapshot=lambda: None, observations=[], motion_events=[], action_results={})
            cfg = {
                "robot_id": "rover-1",
                "calibration_id": "cal-1",
                "radius": 0.2,
                "max_step": 0.5,
                "max_goals": 8,
                "max_refocus": 1,
                "timeout_s": 60,
                "area": {"aliases": ["测试区"], "bounds": [-2.0, -1.6, 2.0, 1.6]},
            }
            survey = Survey(rover, cfg, Path(tmp) / "sessions.json", Path(tmp), "http://127.0.0.1:9")
            row = survey.create("nav-sem-1", "nav-sem-1/step/1", "前往门厅", {"task_id": "nav-sem-1"}, "navigate", "门厅")
            grid = _sample_grid()
            n = len(grid["cells"])
            row.update(
                area=[-2.0, -1.6, 2.0, 1.6],
                grid=grid,
                loaded=grid,
                goal=[0.8, 0.0],
                semantic_layers={
                    "stamp_ns": 1000,
                    "valid_until_ns": 2000,
                    "source_ref": "b" * 64,
                    "risk": [0.9 if i == 9 * 24 + 10 else 0.05 for i in range(n)],
                    "traversable": [1.0] * n,
                    "preferred": [0.8] * n,
                },
            )
            captured = {}

            def fake_service(_row, name, body):
                captured["name"] = name
                captured["body"] = body
                return {"state": "WAYPOINT", "goal": [0.4, 0.0], "path": [[0.0, 0.0], [0.4, 0.0]],
                        "reason": "ok", "semantic_prior_stale": True, "semantic_source_ref": "b" * 64}

            with patch.object(survey, "service", side_effect=fake_service):
                plan = survey.navigation_plan(row, {"grid": grid, "pose": {"x": 0.0, "y": 0.0}, "stamp": 5000})
            self.assertEqual(captured["body"]["stamp_ns"], 5000)
            self.assertEqual(captured["body"]["semantic_layers"]["source_ref"], "b" * 64)
            self.assertTrue(plan["semantic_prior_stale"])
            shown = survey.show("nav-sem-1")["mission"]["semantic_costmap"]
            self.assertTrue(shown["stale"])
            self.assertEqual(shown["origin_mode"], "explicit_prior")

    @unittest.skipIf(Station is None, "numpy/cv2 not installed in minimal Python environment")
    def test_calibration_station_persists_bounded_extrinsic_view_plan_across_recording(self):
        profile = {
            "robot_id": "rover-cal",
            "environment": "simulation",
            "clock_domain": "sim_clock",
            "calibration_method": "four_hole_board",
            "camera": {"id": "d435", "frame": "camera_color_optical_frame", "topic": "/rgb", "info_topic": "/info"},
            "capture_navigation": {"minimum_lateral_m": 0.28},
            "solver": {
                "lidar_frames": {"front": "livox_front"},
                "four_hole_board": {
                    "confirmed": True,
                    "board_id": "board-01",
                    "marker_size_m": 0.11,
                    "marker_spacing_m": [0.85, 0.60],
                    "hole_diameter_m": 0.20,
                    "hole_spacing_m": [0.40, 0.30],
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            station = Station(profile, Path(tmp) / "cal", ROOT / "calibration/station/plugins/runtime")
            # Initial status has no capture_feedback
            initial = station.status()
            self.assertIsNone(initial["capture_feedback"])

            # Compact helper strips unknown internal fields and bounds payload
            raw_reply = _sample_view_plan_reply()
            compact = _compact_view_plan(raw_reply, op="cal-1/step/2", minimum_lateral_m=0.28)
            self.assertEqual(compact["planner_version"], "1.5.0")
            self.assertEqual(compact["minimum_lateral_each_m"], 0.28)
            self.assertNotIn("secret_internal_blob", compact["view_options"][0])
            self.assertEqual(compact["view_options"][0]["corridor_risk"], 0.11)

            # Active spatial extrinsic task executes plan-views then enters RECORDING
            task = station.data["tasks"].setdefault(
                "cal-1",
                {"profile": station.fingerprint, "poses": [], "bindings": {},
                 "goal_tool": "verify_sensor_extrinsics", "active_view_requested": True, "phase": "FRONT_BOARD_CONFIRMED"},
            )
            station.data["last_calibration"] = "cal-1"
            station.data["latest"] = "cal-1"
            with patch.object(station, "inspect_view", return_value={"scene_slice": raw_reply["scene_slice"]}), \
                    patch.object(station, "service", return_value=raw_reply):
                outcome = station.perform("cal-1", task, "plan-views", "cal-1/step/2", {})
            self.assertEqual(outcome["outcome"], "views_ranked")

            # Simulate capture_board overwriting task['capture_feedback'] during RECORDING
            task["capture_feedback"] = {
                "condition": "hold_still",
                "guidance": "正在录制同一姿态",
                "clip": {"frames": 2, "required_frames": 4, "duration_s": 0.5, "required_duration_s": 1.0},
            }
            task["phase"] = "RECORDING"
            live_status = station.status()
            encoded_bytes = json.dumps(live_status, ensure_ascii=False).encode("utf-8")
            self.assertLess(len(encoded_bytes), 65536)
            fb = live_status["capture_feedback"]
            self.assertEqual(fb["condition"], "hold_still")
            self.assertEqual(fb["view_plan"]["operation_id"], "cal-1/step/2")
            self.assertEqual(fb["capture_view_options"][0]["semantic_cost"], 1.44)
            self.assertIn(["target", "guards", "capture_zone"], fb["scene_slice"]["edges"])

            # Unowned passive probe on another task must not overwrite cal-1's status projection
            other = station.data["tasks"].setdefault(
                "probe-only",
                {"profile": station.fingerprint, "poses": [], "bindings": {}, "goal_tool": "verify_sensor_extrinsics"},
            )
            other_reply = {**raw_reply, "view_options": [{**raw_reply["view_options"][0], "ref": "other-ref"}]}
            with patch.object(station, "inspect_view", return_value={}), \
                    patch.object(station, "service", return_value=other_reply):
                station.perform("probe-only", other, "plan-views", "probe-only/step/1", {})
            self.assertEqual(station.status()["capture_feedback"]["view_plan"]["view_options"][0]["ref"], "goal-left-1")


if __name__ == "__main__":
    unittest.main()
