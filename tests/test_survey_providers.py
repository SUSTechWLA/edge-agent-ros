"""SDD-058 exploration services use measured evidence and immutable map identities."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import math
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from edge_contracts.occupancy import Grid, clearance_mask
from exploration.survey_providers import MapStore, serve
from edge_contracts.preview_http import PreviewHTTP
from exploration.survey_route import adjacent_cells, route, through_points
from exploration.survey_semantic import assess_semantics, resolve_place
from exploration.survey_signs import detect, interpolation
from exploration.survey_strategy import frontier


def grid():
    result = {"width": 45, "height": 45, "origin": [-2.25, -2.25, 0], "frame": "odom", "resolution": .1}
    result["cells"] = [0 if 1 < i%45 < 36 and 1 < i//45 < 43 else -1 for i in range(2025)]
    return result


def observation(x, time, evidence, label="教学楼门厅"):
    return {"frame": "odom", "source_id": "test", "stamp_s": time, "label": label, "confidence": .98,
            "pose": [x, 0, 0], "point": [1.18, .25], "evidence_ref": evidence * 64,
            "source_kind": "gazebo_rgbd_fiducial"}


def samples(*, frame="fixture", depth_value=1.2, ns_depth=0):
    width = height = 16
    colors = bytearray(b"\x00\x00\x00" * (width*height))
    for y in range(5, 11):
        for x in range(4, 12):
            coord = (y*width+x)*3
            colors[coord:coord+3] = bytes((212, 17, 178))

    def header(ns=0):
        return SimpleNamespace(frame_id=frame, stamp=SimpleNamespace(sec=2, nanosec=ns))

    def image(encoding, size, data, ns=0):
        return SimpleNamespace(width=width, height=height, header=header(ns), encoding=encoding,
                               step=width*size, is_bigendian=0, data=data)

    color = image("rgb8", 3, colors)
    depth = image("32FC1", 4, struct.pack("<f", depth_value) * (width*height), ns_depth)
    info = SimpleNamespace(width=width, height=height, header=header(), k=[16, 0, 8, 0, 16, 8, 0, 0, 1])
    return color, depth, info


class SurveyProvidersTest(unittest.TestCase):
    def test_global_reference_preserves_unobserved_future_but_never_admits_motion_through_it(self):
        published = grid()
        live = deepcopy(published)
        for i in range(len(live["cells"])):
            if i % live["width"] > 23:
                live["cells"][i] = -1
        request = dict(published_grid=published,live_grid=live,pose=[-1.2,0.],goal=[1.,0.],
                       bounds=[-2.,-2.,2.,2.],footprint_radius=.2,max_step=.4,
                       execution_mode="continuous_path",observed_prefix=True)
        plan = route(request)
        self.assertEqual(plan["global_path"][-1],request["goal"])
        self.assertEqual(plan["global_path"][:len(plan["through_points"])],plan["through_points"])
        self.assertLess(plan["through_points"][-1][0],0.)
        for i in range(len(live["cells"])):
            if i % live["width"]==17:
                live["cells"][i]=100
        self.assertEqual(route(request)["state"],"PARTIAL")
        self.assertEqual(route({**request,"pose":request["goal"]})["state"],"PARTIAL")

    def test_shared_readonly_host_exposes_calculations_without_opening_a_map_store(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(PreviewHTTP, "serve_forever", autospec=True) as run:
            state = Path(temp)/"state"
            serve("127.0.0.1", 0, state, "readonly")
            server, = run.call_args.args
            self.assertEqual(set(server.node), {"/frontier", "/semantic", "/resolve", "/route", "/corridor-review"})
            self.assertFalse(state.exists())

    def test_oblique_route_cannot_cut_an_occupied_or_low_clearance_corner(self):
        model = Grid(grid())
        i = model.index([0.,0.])
        diagonal = i+model.width+1
        gaps = [1.]*len(model.cells)
        self.assertIn(diagonal,adjacent_cells(model,i,lambda j:True,gaps,.85))
        for side in (i+1,i+model.width):
            self.assertNotIn(diagonal,adjacent_cells(model,i,lambda j:j!=side,gaps,.85))
            gaps[side]=.55
            self.assertNotIn(diagonal,adjacent_cells(model,i,lambda j:True,gaps,.85))
            gaps[side]=1.

    def test_departure_probe_can_select_a_longer_clear_chord_around_a_grid_turn(self):
        model = Grid(grid())
        raw = [[.1*i,0.] for i in range(5)] + [[.4,.1*i] for i in range(1,11)]
        safe = lambda i: i >= 0 and model.cells[i] == 0
        normal = through_points(raw,model,safe,.65,[])
        departure = through_points(raw,model,safe,.65,[],departure=.64)
        self.assertGreater(math.dist(raw[0],departure[1]),math.dist(raw[0],normal[1])+.1)
        self.assertEqual(departure[-1],raw[-1])
        self.assertTrue(all(math.dist(a,b)<=.65 for a,b in zip(departure,departure[1:])))
        blocked = model.index([.22,.25])
        self.assertNotEqual(through_points(raw,model,lambda i:safe(i) and i!=blocked,.65,[],departure=.64)[1],departure[1])

    def test_continuous_routes_seek_the_door_center_in_both_directions_and_maps(self):
        # Offset and rotate the doorway to exercise the cost in map axes, independently of the campus.
        side, resolution = 72, .10
        empty = dict(width=side, height=side, origin=[0.,0.,0.], resolution=resolution, frame="odom",cells=[0]*(side*side))
        for transpose in (False,True):
            orient = (lambda x,y:[y,x]) if transpose else (lambda x,y:[x,y])
            wall = deepcopy(empty)
            for j in range(side):
                if 19 <= j < 41:
                    continue
                for i in (34,35,36):
                    x,y = orient(i,j)
                    wall["cells"][y*side+x] = 100
            a,b = orient(1.23,3.75),orient(5.87,3.75)
            for start, goal, published, sensed in ((a,b,wall,empty),(b,a,empty,wall)):
                request = dict(published_grid=published, live_grid=sensed, pose=start, goal=goal, bounds=[0,0,7.2,7.2],
                               max_step=.65, footprint_radius=.30, clearance_margin_m=.55, execution_mode="continuous_path")
                result = route(request)
                self.assertEqual(result["state"],"WAYPOINT",result)
                points = [orient(*p) for p in result["through_points"]]
                crossings = []
                for (x,y),(xx,yy) in zip(points,points[1:]):
                    if min(x,xx)<=3.55<=max(x,xx) and x!=xx:
                        crossings.append(y+(3.55-x)*(yy-y)/(xx-x))
                self.assertEqual(len(crossings),1)
                self.assertLess(abs(crossings[0]-3.),.13)
                self.assertGreater(min(4.1-crossings[0],crossings[0]-1.9)-.30,.55)
        sealed = deepcopy(wall)
        for j in range(side):
            x,y = orient(35,j)
            sealed["cells"][y*side+x] = 100
        self.assertEqual(route({**request,"live_grid":sealed})["state"],"PARTIAL")

    def test_resolve_places_only_in_a_verified_map_with_explicit_semantics(self):
        ref = {"id":"campus","frame":"odom","revision":"a"*64,"digest":"a"*64}
        lobby = {"entity_id":"lobby","name":"教学楼门厅","aliases":["门厅","等候点"],"environment":"indoor",
                 "source_kind":"simulation_layout_node","state":"confirmed","entrance_pose":[0,0,0]}
        outside = {**lobby,"entity_id":"outside","name":"门外北侧","aliases":["等候点"],"environment":"outdoor",
                   "entrance_pose":[3,.8,0],"source_kind":"simulation_site_annotation"}
        def lookup(query, markers=None):
            return resolve_place(dict(query=query, map_ref=ref, semantic=markers if markers is not None else [lobby,outside]))
        self.assertEqual(lookup("门厅")["entity_id"], "lobby")
        self.assertEqual(lookup("室内的等候点")["entity_id"], "lobby")
        self.assertEqual(lookup("室外等候点")["entity_id"], "outside")
        self.assertEqual(lookup("北")["state"], "unknown")
        self.assertEqual(lookup("室外的门厅")["state"], "unknown")
        self.assertEqual([p["state"] for p in lookup("等候点")["candidates"]], ["confirmed","confirmed"])
        self.assertEqual(lookup("等候点")["state"], "ambiguous")
        self.assertNotIn("entrance_pose",lookup("等候点"))
        self.assertEqual(lookup("门厅",[{**lobby,"state":"proposed"}])["state"], "unconfirmed")
        self.assertEqual(lookup("门厅",[{**lobby,"frame":"other"}])["state"], "unavailable")
        with self.assertRaisesRegex(ValueError,"invalid_place_catalog"):
            lookup("等候点", [lobby,lobby])
        with self.assertRaisesRegex(ValueError,"invalid_place_query"):
            resolve_place(dict(query="门厅", map_ref={**ref,"digest":"b"*64}, semantic=[lobby]))

    def test_revisit_uses_alternative_observation_path_without_rewriting_coverage(self):
        raw = grid()
        request = {"grid": raw, "pose": [0, 0], "bounds": [-1.7, -1.7, 1.7, 1.7],
                   "footprint_radius": .08, "min_step": .18, "max_step": .60}
        plan = frontier(request)
        review = frontier({**request, "mode": "refocus", "review": {"point": [1.45, .05],
                           "viewpoints": [[-.20, .1]]}})
        self.assertEqual(plan["state"], "WAYPOINT")
        self.assertEqual(review["state"], "WAYPOINT")
        self.assertEqual(review["reason"], "alternate_observation_view")
        self.assertGreater(abs(review["goal"][1]), .2)
        planned = frontier({**request, "avoid": [{"center": [1.2, 0], "radius": .25}]})
        self.assertEqual(planned["known_cells"], plan["known_cells"])
        self.assertEqual(planned["total_cells"], plan["total_cells"])

    def test_semantics_require_two_spatially_distinct_observed_views(self):
        request = {"grid": grid(), "map_id": "office", "frame": "odom", "pose": [0, 0],
                   "bounds": [-1.7, -1.7, 1.7, 1.7], "footprint_radius": .1}
        ob = observation(.15, 1, "a")
        repeated = assess_semantics({**request, "observations": [ob, ob, observation(.2, 2, "b")]})
        self.assertEqual(repeated["candidates"][0]["reason"], "need_alternate_view")
        result = assess_semantics({**request, "observations": [ob, observation(.4, 2, "b")]})
        confirmed, = result["candidates"]
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertLess(confirmed["entrance_pose"][0], confirmed["anchor"][0] - .5)
        impostor = observation(.4, 3, "c")
        impostor["point"] = [-.8, .25]
        competing = assess_semantics({**request, "observations": [ob, observation(.4, 2, "b"), impostor]})
        self.assertEqual(len(competing["candidates"]), 2)
        self.assertTrue(all(p["state"] == "proposed" for p in competing["candidates"]))

    def test_artifact_reloads_with_no_unregistered_substitution(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MapStore(temp)
            request = dict(map_id="office", grid=grid(), robot_id="rover", operation_id="office/1",
                           calibration_id="sim", stamp_ns=100, state="PARTIAL", reason="unobserved",
                           semantic=[])
            with ThreadPoolExecutor(2) as pool:
                a, b = pool.map(store.publish, (request, request))
            self.assertEqual(a["map_ref"], b["map_ref"])
            self.assertEqual(store.load({"map_id": "office"})["grid"]["cells"], request["grid"]["cells"])
            with self.assertRaisesRegex(ValueError, "map_operation_conflict"):
                store.publish({**request, "reason": "different"})
            with self.assertRaisesRegex(ValueError, "confirmed_marker_evidence_invalid"):
                store.publish({**request, "operation_id": "office/forged", "semantic": [
                    {"name": "教学楼门厅", "state": "confirmed", "anchor": [1, .2],
                     "entity_id": "forged", "entrance_pose": [.5, .2, 0]}]})
            avoid = [{"center": [1.2, .6], "radius": .2}]
            branch = store.publish({**request, "operation_id": "office/2", "stamp_ns": 200, "avoid": avoid})
            with self.assertRaisesRegex(ValueError, "map_revision_required"):
                store.load({"map_id": "office"})
            revision = branch["map_ref"]["revision"]
            reloaded = store.load({"map_id": "office", "revision": revision})
            self.assertEqual(reloaded["map_ref"], branch["map_ref"])
            self.assertEqual(reloaded["avoid"], avoid)
            (Path(temp)/"office"/revision/"map.pgm").write_text("modified", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "map_artifact_invalid"):
                store.load({"map_id": "office", "revision": revision})

    def test_route_requires_live_and_published_free_space(self):
        published = grid()
        request = {"published_grid": published, "live_grid": published,
                   "pose": [0, 0], "goal": [.70, .2], "bounds": [-1.7, -1.7, 1.7, 1.7],
                   "footprint_radius": .08, "max_step": .6}
        result = route(request)
        self.assertEqual(result["state"], "WAYPOINT")
        alternate = route({**request,"rejected":[{"start":request["pose"],"goal":result["goal"]}]})
        self.assertEqual(alternate["reason"],"map_route_after_local_rejection")
        self.assertGreater(math.dist(result["goal"],alternate["goal"]),.19)
        self.assertEqual(route({**request, "avoid": [{"center": request["goal"], "radius": .12}]})["state"], "PARTIAL")
        blocked = deepcopy(published)
        for row in range(45):
            blocked["cells"][row*45+27] = -1
        self.assertEqual(route({**request, "live_grid": blocked})["state"], "PARTIAL")
        with self.assertRaisesRegex(ValueError, "map_geometry_changed"):
            route({**request, "live_grid": {**blocked, "frame": "map"}})

    def test_route_curve_stays_inside_both_clearance_masks_and_keeps_waypoint(self):
        width = 24
        published = {"width": width, "height": width, "origin": [-1, .7, .37],
                     "frame": "odom", "resolution": .1, "cells": [0] * (width * width)}
        live = deepcopy(published)
        for y in range(4, 16):
            for x in (10, 11):
                published["cells"][y * width + x] = 100
        for x in range(13, 19):
            live["cells"][15 * width + x] = -1
        map_grid = Grid(published)
        start, goal = (map_grid.center(x + 10 * width) for x in (4, 20))
        radius = .08
        plan = route({"published_grid": published, "live_grid": live,
                      "pose": start, "goal": goal, "bounds": [-4, -4, 4, 4],
                      "footprint_radius": radius, "max_step": .6})
        self.assertEqual(plan["state"], "WAYPOINT")
        curve = plan["route_curve"]
        self.assertEqual(curve["frame"], map_grid.frame)
        self.assertEqual(curve["start"], start)
        self.assertEqual(curve["segments"][-1]["end"], goal)
        self.assertIn(plan["goal"], [segment["end"] for segment in curve["segments"]])
        self.assertTrue(any("control" in segment for segment in curve["segments"]))
        clear_published, _ = clearance_mask(map_grid, radius)
        clear_live, _ = clearance_mask(Grid(live), radius)
        origin = start
        for segment in curve["segments"]:
            end = segment["end"]
            control = segment.get("control", [(a + b) / 2 for a, b in zip(origin, end)])
            for i in range(201):
                t = i / 200
                position = [(1 - t)**2 * a + 2 * t * (1 - t) * b + t**2 * c
                            for a, b, c in zip(origin, control, end)]
                index = map_grid.index(position)
                self.assertGreaterEqual(index, 0)
                self.assertTrue(clear_published[index] and clear_live[index], position)
            origin = end

    def test_fiducial_projection_rejects_invalid_timing_frame_and_depth(self):
        config = {"camera_frame": "fixture", "source_id": "camera", "marker": "教学楼门厅", "mount": [.1, 0, 0]}
        valid = detect(samples(), config, [2, 3, 0], "odom")
        self.assertAlmostEqual(valid["point"][0], 3.3, places=5)
        self.assertAlmostEqual(valid["point"][1], 3.0375, places=5)
        self.assertEqual(valid["evidence"]["box_px"], [4, 5, 12, 11])
        self.assertEqual(len(valid["evidence_ref"]), 64)
        self.assertIsNone(detect(samples(frame="wrong"), config, [2, 3, 0], "odom"))
        self.assertIsNone(detect(samples(depth_value=float("inf")), config, [2, 3, 0], "odom"))
        self.assertIsNone(detect(samples(ns_depth=30_000_001), config, [2, 3, 0], "odom"))

    def test_capture_pose_requires_a_fresh_time_bracket(self):
        poses = deque([(0, [1, 2, 3.12]), (100_000_000, [3, 4, -3.12])])
        x, y, orientation = interpolation(poses, 50_000_000)
        self.assertEqual([x, y], [2, 3])
        self.assertGreater(orientation, 3.12)
        self.assertIsNone(interpolation(poses, 150_000_000))
        self.assertIsNone(interpolation(deque([poses[0], (250_000_000, poses[1][1])]), 50_000_000))


if __name__ == "__main__":
    unittest.main()
