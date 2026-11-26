"""Read-only planning on an approved occupancy map or directed spatial corridor graph."""
import heapq
from itertools import count
import math

from edge_contracts.navigation import (MAX_WAYPOINTS, ROUTE_DIMENSIONS, NavigationError, digest, identifier,
                                       map_reference, position, route_reference, validate_route)
from edge_contracts.occupancy import Grid, clearance_mask


def _binding(catalog, actor_ref):
    if not identifier(actor_ref):
        raise NavigationError("navigation.invalid_actor")
    try:
        actor = catalog["actors"][actor_ref]
        profile = catalog["profiles"][actor["profile_ref"]]
        record = catalog["maps"][actor["map_key"]]
        dimension = ROUTE_DIMENSIONS[profile["route_type"]]
    except (KeyError, TypeError) as exc:
        raise NavigationError("navigation.binding_unavailable") from exc
    if type(profile.get("dimensions")) is not int or profile["dimensions"] not in dimension:
        raise NavigationError("navigation.profile_dimensions_mismatch")
    return actor, profile, record


def resolve_destination(catalog, actor_ref, destination_ref):
    """Select one entrance qualified for this body and map. A duplicate remains ambiguous."""
    actor, profile, record = _binding(catalog, actor_ref)
    if not identifier(destination_ref):
        raise NavigationError("navigation.invalid_destination")
    places = catalog.get("destinations")
    if not isinstance(places, dict):
        raise NavigationError("navigation.invalid_destination")
    candidates = places.get(destination_ref, [])
    if not isinstance(candidates, list):
        raise NavigationError("navigation.invalid_destination")
    if any(not isinstance(p, dict) or set(p) != {"map_key", "profiles", "position_m"}
           or not identifier(p["map_key"]) or not isinstance(p["profiles"], list)
           or not all(identifier(v) for v in p["profiles"]) for p in candidates):
        raise NavigationError("navigation.invalid_destination")
    selected = [p for p in candidates if p["map_key"] == actor["map_key"] and actor["profile_ref"] in p["profiles"]]
    if not selected:
        raise NavigationError("navigation.destination_unavailable")
    if len(selected) != 1:
        raise NavigationError("navigation.destination_ambiguous")
    return {"actor_ref": actor_ref, "destination_ref": destination_ref, "profile_ref": actor["profile_ref"],
            "map_ref": map_reference(record), "frame_id": record["frame_id"],
            "site_id": record["site_id"], "map_id": record["map_id"], "revision": record["revision"],
            "position_m": position(selected[0]["position_m"], profile["dimensions"])}


def _shortest_path(start, goal, adjacent, point, heuristic, edge_cost=None):
    serial = count()
    queue = [(0.0, 0.0, next(serial), start)]
    costs, previous = {start: 0.0}, {}
    while queue:
        _, cost, _, node = heapq.heappop(queue)
        if cost != costs[node]:
            continue
        if node == goal:
            path = [node]
            while node != start:
                node = previous[node]
                path.append(node)
                if len(path) > MAX_WAYPOINTS:
                    raise NavigationError("navigation.route_too_long")
            return [point(node) for node in reversed(path)]
        for child in adjacent(node):
            distance = math.dist(point(node), point(child))
            updated = cost + (edge_cost(node, child, distance) if edge_cost else distance)
            if updated < costs.get(child, math.inf):
                previous[child], costs[child] = node, updated
                priority = updated + heuristic(point(child), point(goal))
                heapq.heappush(queue, (priority, updated, next(serial), child))
    raise NavigationError("navigation.unreachable")


def _grid_path(record, profile, start, goal, heuristic):
    try:
        grid = Grid(record["geometry"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise NavigationError("navigation.invalid_grid") from exc
    if profile["dimensions"] != 2 or grid.frame != record["frame_id"]:
        raise NavigationError("navigation.grid_frame_or_dimension_mismatch")
    radius = profile.get("clearance_m")
    if type(radius) not in (int, float) or not 0 <= radius <= 1000:
        raise NavigationError("navigation.invalid_footprint_radius")
    safe, _ = clearance_mask(grid, radius)
    first, last = grid.index(start), grid.index(goal)
    if first < 0 or last < 0 or not (safe[first] and safe[last]):
        raise NavigationError("navigation.endpoint_without_clearance")
    if first == last:
        return [start] if start == goal else [start, goal]
    def adjacent(node):
        return (child for child in grid.neighbors(node) if safe[child])
    cells = _shortest_path(first, last, adjacent, grid.center, heuristic)
    # Connect arbitrary endpoints within their own free cell. Connecting an off-center
    # start directly to the next turn could leave the checked footprint corridor.
    return ([start] if start != cells[0] else []) + cells + ([goal] if goal != cells[-1] else [])


def _graph_path(record, profile, start, goal, heuristic):
    geometry = record["geometry"]
    nodes, edges = geometry.get("nodes"), geometry.get("edges")
    if (not isinstance(nodes, dict) or not 1 <= len(nodes) <= 4096
            or not isinstance(edges, list) or len(edges) > 16384):
        raise NavigationError("navigation.invalid_graph")
    points, anchors, adjacent = {}, {}, {}
    for key, raw in nodes.items():
        p = position(raw, profile["dimensions"])
        if not identifier(key) or tuple(p) in anchors:
            raise NavigationError("navigation.graph_node_ambiguous")
        points[key], anchors[tuple(p)], adjacent[key] = p, key, []
    def tags(value):
        if not isinstance(value, list) or not all(identifier(tag) for tag in value):
            raise NavigationError("navigation.invalid_corridor_tags")
        return set(value)
    allowed = tags(profile.get("allowed_tags"))
    for edge in edges:
        if (not isinstance(edge, dict) or set(edge) != {"from", "to", "tags"}
                or not all(identifier(edge[k]) and edge[k] in nodes for k in ("from", "to"))):
            raise NavigationError("navigation.invalid_graph_edge")
        if tags(edge["tags"]) <= allowed:
            adjacent[edge["from"]].append(edge["to"])
    # Every traversed segment must be an approved edge, including the first one.
    if tuple(start) not in anchors or tuple(goal) not in anchors:
        raise NavigationError("navigation.endpoint_outside_corridors")
    return _shortest_path(anchors[tuple(start)], anchors[tuple(goal)], adjacent.__getitem__, points.__getitem__, heuristic)


def plan_route(catalog, actor_ref, destination_ref, localization, algorithm_ref=None):
    """Compute geometry without acquiring a device or execution lease; localization supplies the starting pose."""
    actor, profile, record = _binding(catalog, actor_ref)
    destination = resolve_destination(catalog, actor_ref, destination_ref)
    if not isinstance(localization, dict) or any(localization.get(k) != destination[k]
                                                for k in ("actor_ref", "map_ref", "frame_id")):
        raise NavigationError("navigation.localization_binding_mismatch")
    epoch, clock, stamp = (localization.get(k) for k in ("localization_epoch", "clock_id", "stamp_ns"))
    if not identifier(epoch) or not identifier(clock) or type(stamp) is not int or not 0 <= stamp < 2**63:
        raise NavigationError("navigation.invalid_localization")
    start = position(localization.get("position_m"), profile["dimensions"])
    selected = profile.get("algorithm_ref") if algorithm_ref is None else algorithm_ref
    heuristics = {"astar": math.dist, "dijkstra": lambda _a, _b: 0.0}
    if not identifier(selected) or selected not in heuristics:
        raise NavigationError("navigation.algorithm_unavailable")
    search = {"occupancy": _grid_path, "graph": _graph_path}.get(record["kind"])
    if search is None:
        raise NavigationError("navigation.map_kind_unsupported")
    path = search(record, profile, start, destination["position_m"], heuristics[selected])
    if len(path) > MAX_WAYPOINTS:
        raise NavigationError("navigation.route_too_long")
    route = dict(schema_version=1, actor_ref=actor_ref, destination_ref=destination_ref,
                 profile_ref=actor["profile_ref"], profile_digest=digest(profile), map_ref=destination["map_ref"],
                 frame_id=record["frame_id"], localization_epoch=epoch, localization_stamp_ns=str(stamp), clock_id=clock,
                 route_type=profile["route_type"], algorithm_ref=selected, waypoints=path,
                 length_m=math.fsum(math.dist(a, b) for a, b in zip(path, path[1:])))
    route["route_ref"] = route_reference(route)
    validate_route(route, actor_ref=actor_ref, map_ref=destination["map_ref"], frame_id=record["frame_id"],
                   localization_epoch=epoch, profile_ref=actor["profile_ref"])
    return route
