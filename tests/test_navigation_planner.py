"""SDD-051 read-only route planning, binding and handoff on a synthetic survey."""
from copy import deepcopy
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_contracts.navigation import NavigationError, map_reference, route_reference, validate_route
from edge_contracts.occupancy import Grid
from navigation.bench import catalog, start_pose
from navigation.planner import plan_route, resolve_destination


class NavigationPlanningTest(unittest.TestCase):
    def setUp(self):
        self.site = catalog()

    def plan(self, actor, **options):
        return plan_route(self.site, actor, "office", start_pose(actor), **options)

    def test_same_semantic_place_four_profiles(self):
        rover, dog, drone, person = (self.plan(actor) for actor in ("rover", "dog", "drone", "person"))
        self.assertEqual([r["route_type"] for r in (rover, dog, drone, person)], ["ground", "terrain", "flight", "guidance"])
        self.assertGreater(rover["length_m"], 6)
        self.assertEqual(dog["waypoints"], [[1.5, 1.5], [4.5, 5.5], [7.5, 1.5]])
        self.assertEqual(person["waypoints"], [[1.5, 1.5], [7.5, 1.5]])
        self.assertEqual(drone["waypoints"], [[1.5, 1.5, 1], [4.5, 1.5, 3], [7.5, 1.5, 3]])
        self.assertEqual(dog["map_ref"], person["map_ref"])
        self.assertNotEqual(dog["route_ref"], person["route_ref"])

    def test_astar_and_dijkstra_route_around_wall_with_footprint(self):
        grid = Grid(self.site["maps"]["ground"]["geometry"])
        paths = [self.plan("rover", algorithm_ref=algorithm) for algorithm in ("astar", "dijkstra")]
        for path in paths:
            # Independent clearance check against raw row-major cells, including map edges.
            for point in path["waypoints"]:
                y, x = divmod(grid.index(point), grid.width)
                self.assertTrue(0 < x < grid.width-1 and 0 < y < grid.height-1)
                self.assertTrue(all(grid.cells[(y+v)*grid.width+x+u] == 0 for v in (-1,0,1) for u in (-1,0,1)))
            self.assertEqual(path["length_m"], 16.0)
        self.assertNotEqual(paths[0]["route_ref"], paths[1]["route_ref"])
        self.assertEqual(paths[0], self.plan("rover"))

    def test_unreachable_unknown_wall_and_outside_endpoint_refused(self):
        record = self.site["maps"]["ground"]
        grid = record["geometry"]
        for row in range(grid["height"]):
            grid["cells"][row*grid["width"]+4] = -1
        pose = dict(start_pose("rover"), map_ref=map_reference(record))
        with self.assertRaisesRegex(NavigationError, "unreachable"):
            plan_route(self.site, "rover", "office", pose)
        pose["position_m"] = [-2, -2]
        with self.assertRaisesRegex(NavigationError, "without_clearance"):
            plan_route(self.site, "rover", "office", pose)

    def test_duplicate_and_profile_ineligible_destination(self):
        self.site["destinations"]["office"].append(deepcopy(self.site["destinations"]["office"][0]))
        with self.assertRaisesRegex(NavigationError, "ambiguous"):
            self.plan("rover")
        self.assertEqual(resolve_destination(self.site, "dog", "office")["profile_ref"], "quadruped")
        with self.assertRaisesRegex(NavigationError, "destination_unavailable"):
            resolve_destination(self.site, "person", "undeclared")

    def test_graph_edges_directed_and_filtered_no_imaginary_connectors(self):
        self.site["profiles"]["quadruped"]["allowed_tags"] = []
        with self.assertRaisesRegex(NavigationError, "unreachable"):
            self.plan("dog")
        for p in ([1.5, 1.5], [1.51, 1.5, 1]):
            with self.subTest(p=p), self.assertRaisesRegex(NavigationError, "invalid_position|outside_corridors"):
                plan_route(self.site, "drone", "office", dict(start_pose("drone"), position_m=p))
        self.site["destinations"]["office"][2]["position_m"] = [1.5, 1.5, 1]
        with self.assertRaisesRegex(NavigationError, "unreachable"):
            plan_route(self.site, "drone", "office", dict(start_pose("drone"), position_m=[7.5, 1.5, 3]))

    def test_identity_separates_same_named_frames_sites_and_revisions(self):
        pose = start_pose("rover")
        original = self.site["maps"]["ground"]
        for change in ({"site_id": "other"}, {"revision": "2"}):
            with self.subTest(change=change):
                self.site["maps"]["ground"] = dict(original, **change)
                with self.assertRaisesRegex(NavigationError, "localization_binding_mismatch"):
                    plan_route(self.site, "rover", "office", pose)

    def test_route_rebinding_tamper_and_handoff(self):
        route = self.plan("rover")
        active = {k: route[k] for k in ("actor_ref","map_ref","frame_id","localization_epoch","profile_ref")}
        validate_route(route, **active)
        with self.assertRaisesRegex(NavigationError, "binding_mismatch"):
            validate_route(route, **dict(active, localization_epoch="reset"))
        altered = deepcopy(route)
        altered["waypoints"][-1][0] += 0.5
        with self.assertRaisesRegex(NavigationError, "route_integrity"):
            validate_route(altered, **active)
        for sample in ([], [[math.nan, 0]], [[1, 2, 3]]):
            with self.subTest(sample=sample), self.assertRaises(NavigationError):
                validate_route(dict(route, waypoints=sample), **active)
        pose = dict(start_pose("rover"), stamp_ns=1770911200000000001)
        epoch_route = plan_route(self.site, "rover", "office", pose)
        self.assertEqual(epoch_route["localization_stamp_ns"], "1770911200000000001")
        self.assertEqual(route_reference(epoch_route), epoch_route["route_ref"])
        self.assertNotEqual(epoch_route["route_ref"], route["route_ref"])
        at_reset = plan_route(self.site, "rover", "office", dict(pose, stamp_ns=0))
        self.assertEqual(at_reset["localization_stamp_ns"], "0")

    def test_rotated_grid_subcell_endpoints(self):
        record = self.site["maps"]["ground"]
        grid = record["geometry"] = {"frame": "map", "origin": [10, 10, math.pi/2],
                                     "width": 5, "height": 5, "resolution": 1.0, "cells": [0]*25}
        self.site["destinations"]["office"][0]["position_m"] = [6.8, 13.2]
        pose = dict(start_pose("rover"), position_m=[8.6, 11.2], map_ref=map_reference(record))
        route = plan_route(self.site, "rover", "office", pose)
        self.assertEqual(route["waypoints"][0], pose["position_m"])
        self.assertEqual(route["waypoints"][-1], [6.8, 13.2])
        self.assertEqual(Grid(grid).index(route["waypoints"][0]), Grid(grid).index(route["waypoints"][1]))


if __name__ == "__main__":
    unittest.main()
