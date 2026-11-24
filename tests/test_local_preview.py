"""Contracts for the independent static occupancy preview provider."""
from functools import partial
from http.server import ThreadingHTTPServer
import json
import math
from pathlib import Path
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edge_contracts.navigation import MAX_WAYPOINTS
from edge_contracts.occupancy import Grid, clearance_mask
from navigation.local_preview import Handler, HORIZON_M, bounded_points, evaluate


def request():
    return {"schema_version": 1, "task_id": "walk", "operation_id": "walk/step/1/waypoint/2",
            "frame": "odom", "grid": {"width": 40, "height": 40, "resolution": .1,
                                        "origin_x": -1., "origin_y": -1., "origin_yaw": 0.,
                                        "cells": [0]*1600, "frame": "odom"},
            "start": [.05, .05], "goal": [1.85, .05], "pose_stamp_s": 10.3, "grid_stamp_s": 10.1}


def length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


class LocalPreview(unittest.TestCase):
    def test_static_plan_is_bound_and_horizon_limited(self):
        raw = request()
        result = evaluate(raw)
        for key in ("schema_version", "task_id", "operation_id", "frame"):
            self.assertEqual(result[key], raw[key])
        self.assertEqual(result["dimension"], 2)
        self.assertEqual(result["kind"], "planned")
        self.assertEqual(result["source_stamp_s"], 10.1)
        self.assertIs(result["motion_authorized"], False)
        self.assertEqual(result["points"][0], raw["start"])
        self.assertAlmostEqual(length(result["points"]), HORIZON_M)
        self.assertLess(result["points"][-1][0], raw["goal"][0])
        self.assertTrue(all(y == raw["start"][1] for _, y in result["points"]))

    def test_dense_path_meets_the_consumer_point_budget(self):
        dense = [[i*.002, 0.] for i in range(MAX_WAYPOINTS+6)]
        self.assertEqual(bounded_points(dense), dense[:MAX_WAYPOINTS])

    def test_rotated_grid_preserves_world_coordinates_and_goal(self):
        raw = request()
        raw["grid"].update(origin_x=2., origin_y=-1., origin_yaw=math.pi/2)
        raw["start"], raw["goal"] = [.95, .05], [.95, .85]
        result = evaluate(raw)
        self.assertEqual(result["kind"], "planned")
        self.assertEqual(result["points"][0], raw["start"])
        self.assertEqual(result["points"][-1], raw["goal"])
        self.assertTrue(all(abs(x-.95) < 1e-8 for x, _ in result["points"]))

    def test_detour_avoids_obstacle_cells_and_their_footprint_band(self):
        raw = request()
        raw["goal"] = [.95, .05]
        raw["grid"]["cells"][10*40+14] = 100
        clearance = .09
        result = evaluate(raw, clearance)
        self.assertEqual(result["kind"], "planned")
        points = result["points"]
        self.assertTrue(any(abs(y-.05) >= .19 for _, y in points))
        self.assertLessEqual(length(points), HORIZON_M + 1e-9)
        grid = Grid({**raw["grid"], "origin": [-1., -1., 0.]})
        safe, _ = clearance_mask(grid, clearance)
        for a, b in zip(points, points[1:]):
            for i in range(17):
                p = [x+(y-x)*i/16 for x, y in zip(a, b)]
                index = grid.index(p)
                self.assertGreaterEqual(index, 0)
                self.assertTrue(safe[index])

    def test_unknown_footprint_and_map_edges_block(self):
        raw = request()
        raw["grid"]["cells"][10*40+8] = -1
        result = evaluate(raw)
        self.assertEqual((result["kind"], result["reason"], result["points"]),
                         ("blocked", "start_footprint_unknown", []))
        raw["grid"]["cells"][10*40+8] = 0
        raw["start"] = [-.95, .05]
        self.assertEqual(evaluate(raw)["reason"], "start_footprint_map_edge")
        raw["start"] = [-1.05, .05]
        self.assertEqual(evaluate(raw)["reason"], "start_outside_grid")

    def test_unknown_wall_between_clear_endpoints_is_never_crossed(self):
        raw = request()
        raw["goal"] = [1.05, .05]
        for y in range(40):
            raw["grid"]["cells"][y*40+15] = -1
        result = evaluate(raw, .09)
        self.assertEqual((result["kind"], result["points"], result["reason"]),
                         ("blocked", [], "local_path_unreachable"))

    def test_wrong_frame_or_nonfinite_stamp_is_refused(self):
        raw = request()
        raw["grid"]["frame"] = "world"
        with self.assertRaisesRegex(ValueError, "frame_mismatch"):
            evaluate(raw)
        raw["grid"]["frame"] = "odom"
        raw["pose_stamp_s"] = math.nan
        with self.assertRaisesRegex(ValueError, "invalid_sample_stamp"):
            evaluate(raw)

    def test_http_evaluate_and_request_bound(self):
        handler = partial(Handler, clearance_m=.32)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/evaluate"
                with urlopen(Request(url, json.dumps(request()).encode(), method="POST"), timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    result = json.load(response)
                self.assertEqual(result["kind"], "planned")
                self.assertFalse(result["motion_authorized"])
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(url, b"{}", method="POST"), timeout=3)
                self.assertEqual(caught.exception.status, 400)
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(url, b"{}", {"Content-Length": "3000000"}, method="POST"), timeout=3)
                self.assertEqual(caught.exception.status, 413)
            finally:
                server.shutdown()
                worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
