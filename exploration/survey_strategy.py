"""Read-only frontier selection and alternate observation poses on a measured grid."""

import math

from edge_contracts.occupancy import Grid, clearance_mask, finite
from exploration.frontier import plan_frontier


def xy(value):
    return isinstance(value, (list, tuple)) and len(value) == 2 and all(finite(v) for v in value)


def _bounds(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4 or not all(finite(x) for x in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise ValueError("invalid_bounds")
    return value


def validate_avoid_regions(value):
    if (not isinstance(value, (list, tuple)) or len(value) > 64
            or any(not isinstance(item, dict) or not xy(item.get("center"))
                   or not finite(item.get("radius")) or not 0 < item["radius"] <= 5 for item in value)):
        raise ValueError("invalid_avoid_regions")
    return value


def _masked(raw, deferred):
    if not deferred:
        return raw
    grid = Grid(raw)
    cells = list(grid.cells)
    # Give the planner a temporary barrier without generating a new survey fact.
    # This also prevents a deferred region from becoming a new frontier itself.
    for index, value in enumerate(cells):
        if value <= 0 and any(math.dist(grid.center(index), a["center"]) <= a["radius"] for a in deferred):
            cells[index] = 100
    return {**raw, "cells": cells}


def frontier(body):
    raw, pose, bounds = body["grid"], body["pose"], _bounds(body["bounds"])
    radius = body.get("footprint_radius", .28)
    minimum, maximum = body.get("min_step", 0), body.get("max_step", .65)
    visited, deferred = body.get("visited", []), validate_avoid_regions(body.get("avoid", []))
    mode = body.get("mode", "frontier")
    if (not xy(pose) or not all(finite(v) for v in (radius, minimum, maximum))
            or not 0 <= radius <= 5 or not 0 <= minimum <= maximum <= 10 or maximum <= 0
            or not isinstance(visited, (list, tuple)) or len(visited) > 4096 or not all(xy(p) for p in visited)):
        raise ValueError("invalid_limits_or_pose")
    if mode not in ("frontier", "refocus"):
        raise ValueError("unknown_observation_strategy")
    adjusted = _masked(raw, deferred)
    result = plan_frontier(adjusted, pose, bounds=bounds, footprint_radius=radius,
                           min_step=minimum, max_step=maximum, visited=visited)
    if mode == "refocus":
        result = _refocus(adjusted, pose, bounds, radius, minimum, maximum, body.get("review"), result)
    if deferred:
        # Existing evidence and coverage do not change when an area is deferred.
        grid = Grid(raw)
        x0, y0, x1, y1 = bounds
        inside = [i for i in range(len(grid.cells))
                  if (p := grid.center(i)) and x0 <= p[0] <= x1 and y0 <= p[1] <= y1]
        result.update(total_cells=len(inside), known_cells=sum(grid.cells[i] >= 0 for i in inside))
        if result["state"] == "COMPLETE" and result["known_cells"] < result["total_cells"]:
            result.update(state="PARTIAL", reason="deferred_unknown_region")
    return {"mode": mode, **result}


def _refocus(raw, pose, bounds, radius, minimum, maximum, review, baseline):
    if (not isinstance(review, dict) or not xy(review.get("point"))
            or not isinstance(review.get("viewpoints", []), list)
            or len(review.get("viewpoints", [])) > 16 or not all(xy(p) for p in review.get("viewpoints", []))):
        raise ValueError("invalid_review_target")
    grid = Grid(raw)
    safe, _ = clearance_mask(grid, radius)
    source = grid.index(pose)
    x0, y0, x1, y1 = bounds
    reference = review["point"]
    prior = [*review.get("viewpoints", []), pose]
    coverage = {key: baseline[key] for key in ("known_cells", "total_cells")}
    base = {**coverage, "frame": grid.frame, "state": "PARTIAL", "goal": None, "path": [],
            "reason": "no_reachable_alternate_view", "review_point": reference}
    if source < 0 or not safe[source]:
        return {**base, "state": "BLOCKED", "reason": "pose_without_clearance"}
    ul, vl = grid.local(pose)
    search = math.ceil(maximum/grid.resolution)
    winner, best = None, None
    for row in range(max(0, int(vl)-search), min(grid.height, int(vl)+search+1)):
        for col in range(max(0, int(ul)-search), min(grid.width, int(ul)+search+1)):
            index = row*grid.width+col
            if not safe[index]:
                continue
            candidate = grid.center(index)
            step = math.dist(pose, candidate)
            distance = math.dist(candidate, reference)
            if not (max(minimum, .16) <= step <= maximum and .32 <= distance <= 4
                    and x0+radius <= candidate[0] <= x1-radius and y0+radius <= candidate[1] <= y1-radius
                    and all(math.dist(candidate, p) >= .17 for p in prior)):
                continue
            angle = math.atan2(reference[1]-candidate[1], reference[0]-candidate[0])
            diversity = min(_angle_separation(angle, math.atan2(reference[1]-p[1], reference[0]-p[0])) for p in prior)
            if diversity < math.radians(9) or not _ray_allowed(grid, safe, pose, candidate):
                continue
            # Use clearance-checked goals with a distinct viewing ray. Lower travel cost breaks ties.
            rank = (diversity, -step, -index)
            if best is None or rank > best:
                winner, best = candidate, rank
    if winner is None:
        return base
    return {**base, "state": "WAYPOINT", "goal": winner, "path": [list(pose), winner],
            "reason": "alternate_observation_view",
            "view_yaw_rad": math.atan2(reference[1]-winner[1], reference[0]-winner[0])}


def _angle_separation(a, b):
    return abs(math.atan2(math.sin(a-b), math.cos(a-b)))


def _ray_allowed(grid, safe, start, end):
    steps = max(1, math.ceil(math.dist(start, end)/(grid.resolution/4)))
    for n in range(steps+1):
        index = grid.index([a+(b-a)*n/steps for a, b in zip(start, end)])
        if index < 0 or not safe[index]:
            return False
    return True
