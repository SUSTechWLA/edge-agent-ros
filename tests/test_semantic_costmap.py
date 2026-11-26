"""SDD-061 tests for ROAM semantic-spatial vocabulary, 3D sign attachment, and monotonic costmap routing."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import planning
from edge_contracts.semantic_costmap import (
    OTHER_FALLBACK,
    SemanticPriorMemory,
    conditional_preference_cost,
    corridor_semantic_score,
    monotonic_cell_admitted,
    synthesize_semantic_field,
    validate_semantic_prior,
)
from edge_contracts.semantic_scene import attach_sign_entities, compact_scene, target_scene
from exploration.survey_route import route


def open_corridor_grid(width=30, height=20):
    cells = [0 if 1 < (i % width) < width - 2 and 1 < (i // width) < height - 2 else 100
             for i in range(width * height)]
    return {"width": width, "height": height, "origin": [0.0, 0.0, 0.0], "frame": "odom",
            "resolution": 0.1, "cells": cells}


class SemanticCostmapTest(unittest.TestCase):
    def test_ac1_prior_vocabulary_directional_ban_memory_smoothing_and_softmax(self):
        vocab = validate_semantic_prior({
            "paved_path": {"risk": 0.05, "traversable": 1.0, "preferred": 1.0},
            "lawn_grass": {"risk": 0.25, "traversable": 1.0, "preferred": 0.0},
            "work_zone": {"risk": 0.95, "traversable": 1.0, "preferred": 0.0},
        })
        self.assertEqual(vocab["other"], OTHER_FALLBACK)

        for bad_label in ("left_grass", "path_ahead", "near_fence", "sky", "tree_shadow", "CamelCase"):
            with self.assertRaises(ValueError, msg=bad_label):
                validate_semantic_prior({bad_label: {"risk": 0.2, "traversable": 1.0, "preferred": 0.0}})
        for bad_spec in ({"risk": 1.2, "traversable": 1.0, "preferred": 0.0}, {"risk": 0.2}):
            with self.assertRaises(ValueError):
                validate_semantic_prior({"gravel": bad_spec})

        mem = SemanticPriorMemory(window=3, ttl_ns=10_000_000_000)
        self.assertTrue(mem.snapshot(1_000)["stale"])
        s1 = mem.update(stamp_ns=1_000_000_000, source_ref="a" * 64, memory_note="road ahead closed 10m",
                        vocabulary={"paved_path": {"risk": 0.9, "traversable": 1.0, "preferred": 0.0}})
        self.assertTrue(s1["active"])
        s2 = mem.update(stamp_ns=5_000_000_000, source_ref="b" * 64, memory_note="detouring on grass",
                        vocabulary={"paved_path": {"risk": 0.9, "traversable": 1.0, "preferred": 0.0},
                                    "lawn_grass": {"risk": 0.2, "traversable": 1.0, "preferred": 0.0}})
        # Sign temporarily leaves FOV on frame 3, yet 3-frame moving average retains elevated road risk.
        s3 = mem.update(stamp_ns=9_000_000_000, source_ref="c" * 64, memory_note="passing barrier",
                        vocabulary={"paved_path": {"risk": 0.3, "traversable": 1.0, "preferred": 1.0}})
        self.assertAlmostEqual(s3["vocabulary"]["paved_path"]["risk"], 0.7, places=4)
        self.assertEqual(len(s3["memory"]), 3)
        self.assertTrue(mem.snapshot(20_000_000_000)["stale"])
        self.assertEqual(s2["vocabulary"]["other"], OTHER_FALLBACK)

        field = synthesize_semantic_field(
            [{"paved_path": 8.0, "lawn_grass": -4.0, "other": -8.0},
             {"paved_path": -8.0, "lawn_grass": -8.0, "other": 8.0}],
            {"paved_path": {"risk": 0.1, "traversable": 1.0, "preferred": 1.0},
             "lawn_grass": {"risk": 0.4, "traversable": 1.0, "preferred": 0.0}},
            temperature=0.5,
        )
        self.assertLess(field["risk"][0], 0.12)
        self.assertGreater(field["preferred"][0], 0.98)
        self.assertGreater(field["risk"][1], 0.98)
        self.assertLess(field["traversable"][1], 0.02)

    def test_ac2_3d_sign_attachment_and_calibration_corridor_scene_guards(self):
        sign = {"id": "sign.01", "label": "do_not_step_sign", "kind": "object", "state": "observed",
                "position_m": [2.0, 0.3, 0.4]}
        pallet = {"id": "obj.pallet", "label": "wooden_pallet", "kind": "object", "state": "observed",
                  "position_m": [2.1, 0.35, 0.05]}
        distant_lawn = {"id": "reg.lawn", "label": "distant_lawn", "kind": "region", "state": "observed",
                        "position_m": [6.5, -2.0, 0.0]}
        nodes, edges = attach_sign_entities(sign, [distant_lawn, pallet], max_distance_m=1.0, relation="attached_to")
        self.assertEqual([n["id"] for n in nodes], ["obj.pallet"])
        self.assertEqual(edges, [["sign.01", "attached_to", "obj.pallet"]])
        validated = compact_scene({
            "version": 1, "scope": "sensor_snapshot", "frame": "camera_optical",
            "source_ref": "d" * 64, "stamp_ns": "1000000", "map_ref": None,
            "nodes": [sign, *nodes], "edges": edges,
        })
        self.assertEqual(validated["edges"], [["sign.01", "attached_to", "obj.pallet"]])

        cal_scene = target_scene(
            target="four_hole_board", frame="camera_color_optical_frame", stamp_ns=2_000_000,
            archive="e" * 64, plane={"center_m": [0.0, -0.1, 2.5], "normal": [0.0, 0.0, 1.0]},
            detector={"id": "four_hole", "version": "1", "manifest_sha256": "f" * 64},
            lidar_roles={"front": "front_mid360"}, seen=["front"],
            guards_region={"id": "capture_zone", "label": "active_capture_corridor", "position_m": [0.0, -0.1, 2.5]},
        )
        self.assertIn(["target", "guards", "capture_zone"], cal_scene["edges"])

    def test_ac3_monotonic_obstacle_veto_detour_preference_and_stale_degradation(self):
        base = open_corridor_grid(30, 20)
        n = len(base["cells"])
        # Monotonic law: geometric obstacle is NEVER erased by risk=0, traversable=1.
        self.assertFalse(monotonic_cell_admitted(False, risk=0.0, traversable=1.0, stale=False))
        self.assertTrue(monotonic_cell_admitted(True, risk=0.99, traversable=0.0, stale=True))

        # Upper half (y >= 10) is grass (preferred=0.0, risk=0.15); lower band y in 5..9 is paved trail (preferred=1.0, risk=0.05).
        risk = [0.05 if 5 <= (i // 30) <= 9 else 0.15 for i in range(n)]
        pref = [1.0 if 5 <= (i // 30) <= 9 else 0.0 for i in range(n)]
        trav = [1.0] * n
        layers = {"stamp_ns": 1_000, "valid_until_ns": 5_000, "source_ref": "1" * 64,
                  "risk": risk, "traversable": trav, "preferred": pref}

        req = {
            "published_grid": base, "live_grid": deepcopy(base),
            "pose": [0.45, 0.75], "goal": [2.45, 0.75], "bounds": [0.1, 0.1, 2.9, 1.9],
            "footprint_radius": 0.12, "max_step": 0.65, "execution_mode": "continuous_path",
            "semantic_layers": layers, "stamp_ns": 2_000,
        }
        on_trail = route(req)
        self.assertEqual(on_trail["state"], "WAYPOINT")
        self.assertFalse(on_trail["semantic_prior_stale"])
        self.assertTrue(all(p[1] < 1.0 for p in on_trail["through_points"]))

        # Sign marks middle of paved trail (x in 1.1..1.8, y <= 1.0) as construction zone (risk=0.95 >= 0.80).
        blocked_layers = deepcopy(layers)
        for i in range(n):
            x_m, y_m = (i % 30) * 0.1 + 0.05, (i // 30) * 0.1 + 0.05
            if 1.1 <= x_m <= 1.8 and y_m <= 1.05:
                blocked_layers["risk"][i] = 0.95
        detour = route({**req, "semantic_layers": blocked_layers})
        self.assertEqual(detour["state"], "WAYPOINT")
        self.assertGreater(max(p[1] for p in detour["through_points"]), 1.10)

        # Once robot is already on non-preferred grass (pose y=1.35, P(x_r)=0.0), virtual shoulder penalty lifts.
        self.assertGreater(
            conditional_preference_cost(risk=0.15, preferred=0.0, robot_preferred=1.0),
            conditional_preference_cost(risk=0.15, preferred=0.0, robot_preferred=0.0) + 2.0,
        )
        from_grass = route({**req, "pose": [1.45, 1.35], "semantic_layers": blocked_layers})
        self.assertEqual(from_grass["state"], "WAYPOINT")

        # Expired TTL (stamp_ns=9_000 > valid_until_ns=5_000) degrades cleanly to pure geometry.
        expired = route({**req, "semantic_layers": blocked_layers, "stamp_ns": 9_000})
        self.assertEqual(expired["state"], "WAYPOINT")
        self.assertTrue(expired["semantic_prior_stale"])
        self.assertTrue(all(p[1] < 1.0 for p in expired["through_points"]))

        # Hard LiDAR wall across entire corridor is NEVER bypassed even if semantic risk is 0 everywhere.
        walled = deepcopy(base)
        for y in range(20):
            walled["cells"][y * 30 + 15] = 100
        denied = route({**req, "live_grid": walled})
        self.assertEqual(denied["state"], "PARTIAL")

    def test_ac4_calibration_corridor_scores_exposed_to_feedback_planner(self):
        low_risk = corridor_semantic_score(clearance_margin_m=0.45, path_margin_px=110.0,
                                           fills_missing_side=True, new_rgb_cells=3, robot_on_preferred=False)
        tight_risk = corridor_semantic_score(clearance_margin_m=0.04, path_margin_px=24.0,
                                             fills_missing_side=False, new_rgb_cells=0, robot_on_preferred=True)
        self.assertLess(low_risk["corridor_risk"], tight_risk["corridor_risk"])
        self.assertLess(low_risk["semantic_cost"], tight_risk["semantic_cost"])

        station_root = ROOT / "calibration/station"
        caps = {t["id"]: t for p in (station_root / "tools").glob("*.json") if (t := json.loads(p.read_bytes()))}
        nav = dict(
            stamp_ns=1_000_000_000,
            robot=dict(frame="boot_odom", epoch="a" * 32 + ":1", stamp_ns=1_000_000_000,
                       position_m=[0, 0, 0], orientation_xyzw=[0, 0, 0, 1], yaw_rad=0),
            target=dict(position_m=[3, 0, 0.5], range_m=3, bearing="front"),
            nearest_observed_m=dict(front=3, left=8, rear=8, right=8),
        )
        prior_step = dict(
            tool="plan_capture_views", status="SUCCEEDED",
            feedback={"goal": {"tool": "verify_sensor_extrinsics"}},
            evidence=dict(
                outcome="candidate_views",
                navigation_context=nav,
                view_options=[dict(
                    ref="view.01", path="reverse_left", board_view="left",
                    clearance_margin_m=0.42, corridor_risk=low_risk["corridor_risk"],
                    semantic_cost=low_risk["semantic_cost"], base_xy_yaw=[-0.4, 0.2, -0.1],
                )],
            ),
        )
        task = {"id": "cal-sem-1", "text": "请帮我标定当前传感器", "steps": [prior_step]}
        observed_opts = []

        def fake_model(url, body, timeout):
            observed_opts.extend(body["observations"][-1]["options"])
            return {"goal": "sensor_calibration", "tool": "sensor_calibration_status", "args": {}, "reason": "核对进度"}

        planning.feedback_plan(task, caps, station_root / "feedback.json", None,
                               model_profile={"version": "t", "protocol": "http-planner", "url": "http://local"},
                               model_request=fake_model)
        self.assertEqual(observed_opts[0]["corridor_risk"], low_risk["corridor_risk"])
        self.assertEqual(observed_opts[0]["semantic_cost"], low_risk["semantic_cost"])


if __name__ == "__main__":
    unittest.main()
