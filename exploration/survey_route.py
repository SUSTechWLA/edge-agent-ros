"""Read-only route to a verified map target with live clearance admission."""
import math

from edge_contracts.navigation import NavigationError
from edge_contracts.occupancy import finite
from exploration.survey_strategy import validate_avoid_regions, xy
from navigation.grid_corridor import route_admission, segment_admitted
from navigation.planner import _shortest_path
from navigation.route_display import rounded_route


def clearance_distances(grid, published, preferred):
    """Lower bound on cell-to-cell gap to known obstacles from either observation."""
    # A two-pass chessboard distance transform leaves unknown and ROI handling to route_admission.
    stride = grid.width + 2
    cap = min(max(grid.width,grid.height)+1, math.ceil(preferred/grid.resolution)+2)
    field = [cap] * (stride*(grid.height+2))
    for i, (a,b) in enumerate(zip(grid.cells,published["cells"])):
        if a > 0 or b > 0:
            field[(i//grid.width+1)*stride+i%grid.width+1] = 0
    for y in range(1,grid.height+1):
        for i in range(y*stride+1,(y+1)*stride-1):
            field[i] = min(field[i],1+min(field[i-1],field[i-stride-1],field[i-stride],field[i-stride+1]))
    for y in range(grid.height,0,-1):
        for i in range((y+1)*stride-2,y*stride,-1):
            field[i] = min(field[i],1+min(field[i+1],field[i+stride-1],field[i+stride],field[i+stride+1]))
    return [max(0,field[(i//grid.width+1)*stride+i%grid.width+1]-1)*grid.resolution for i in range(len(grid.cells))]


def adjacent_cells(grid, i, admitted, gaps=None, preferred=0.):
    yield from (j for j in grid.neighbors(i) if admitted(j))
    if gaps is None:
        return
    width = grid.width
    x,y = i%width,i//width
    for dx,dy in ((-1,-1),(-1,1),(1,-1),(1,1)):
        if not (0<=x+dx<width and 0<=y+dy<grid.height):
            continue
        side_x,side_y = i+dx,i+dy*width
        j = side_y+dx
        # A diagonal touches both side cells. Keep the hard admission and the clearance
        # of the entire edge when connecting centres on opposite sides of that corner.
        if (admitted(j) and admitted(side_x) and admitted(side_y)
                and min(gaps[side_x],gaps[side_y]) >= min(preferred,gaps[i],gaps[j])):
            yield j


def through_points(full, grid, admitted, horizon, rejected, gaps=None, preferred=0., departure=0.):
    """Compress adjacent A* edges within a bounded arc length and a checked chord."""
    points = [full[0]]
    for p in full[1:]:
        if math.dist(points[-1], p) > 1e-7:
            points.append(p)
    points[-1] = full[-1]
    result = points[:1]
    start = 0
    while start < len(points)-1:
        arc = 0.
        candidates = []
        for i in range(start+1, len(points)):
            arc += math.dist(points[i-1], points[i])
            if arc > (2*horizon if start == 0 and departure else horizon) + 1e-8:
                break
            if math.dist(points[start],points[i]) > horizon + 1e-8:
                continue
            if start == 0 and any(math.dist(points[0], r["start"]) <= .20 and
                                  math.dist(points[i], r["goal"]) <= .19 for r in rejected):
                continue
            candidates.append(i)
        end = None
        order = (sorted(candidates,key=lambda i:abs(math.dist(points[0],points[i])-departure))
                 if start==0 and departure else reversed(candidates))
        for i in order:
            check = admitted
            if gaps is not None:
                minimum = min(preferred, *(gaps[grid.index(p)] for p in points[start:i+1]))
                check = lambda j: admitted(j) and gaps[j] >= minimum
            if segment_admitted(points[start], points[i], grid, check):
                end = i
                break
        if end is None or len(result) >= 256:
            return []
        result.append(points[end])
        start = end
    return result


def route(request):
    if request.get("observed_prefix") and request.get("execution_mode") == "continuous_path":
        live = request["live_grid"]
        # Unobserved live cells carry no evidence for a global route on the published
        # site. Only the independently checked, positively observed prefix is executable.
        reference_layer = {**live, "cells":[max(0,c) for c in live["cells"]]}
        reference = plan_route({**request, "live_grid":reference_layer})
        if reference["state"] not in ("WAYPOINT","COMPLETE"):
            return reference
        grid, safe = route_admission(request["published_grid"], live, request["bounds"],
                                     request["footprint_radius"], validate_avoid_regions(request.get("avoid", [])))
        if reference["state"] == "COMPLETE":
            if safe(grid.index(request["pose"])) and safe(grid.index(request["goal"])):
                return reference
            return {"state":"PARTIAL","reason":"endpoint_without_live_clearance"}
        full = reference["through_points"]
        prefix = [full[0]]
        for a,b in zip(full, full[1:]):
            if not segment_admitted(a,b,grid,safe):
                break
            prefix.append(b)
        if len(prefix) < 2:
            return {"state":"PARTIAL","reason":"no_observed_departure","global_path":full}
        return {**reference,"through_points":prefix,"global_path":full,"path":prefix}
    return plan_route(request)


def plan_route(request):
    start, goal, bounds = (request[key] for key in ("pose", "goal", "bounds"))
    radius, horizon = request["footprint_radius"], request["max_step"]
    rejected = request.get("rejected", [])
    mode = request.get("execution_mode", "waypoint")
    margin = request.get("clearance_margin_m", 0.)
    departure = request.get("departure_lookahead_m", 0.)
    deferred = validate_avoid_regions(request.get("avoid", []))
    if (len(start) != 2 or len(goal) != 2 or len(bounds) != 4 or
            not all(finite(v) for v in (*start, *goal, *bounds, radius, horizon)) or
            not 0 < radius < 1 or not .15 <= horizon <= 1.5 or not finite(margin) or not 0 <= margin <= 3
            or not finite(departure) or not 0 <= departure <= horizon or mode not in ("waypoint","continuous_path")
            or not isinstance(rejected,list) or
            len(rejected)>12 or not all(isinstance(x,dict) and xy(x.get("start")) and xy(x.get("goal")) for x in rejected)):
        raise ValueError("exploration.route_input_invalid")
    current, safe = route_admission(request["published_grid"], request["live_grid"], bounds, radius, deferred)

    first, last = current.index(start), current.index(goal)
    if not safe(first) or not safe(last):
        return {"state": "PARTIAL", "reason": "endpoint_without_clearance"}
    if math.dist(start, goal) <= .085:
        return {"state": "COMPLETE", "path": [start], "reason": "at_target"}
    gaps = None
    preferred = radius + margin + 2*current.resolution
    cost = None
    if margin:
        gaps = clearance_distances(current, request["published_grid"], preferred)
        def cost(a,b,length):
            gap = min(gaps[a],gaps[b])
            return length * (1 + 16 * (max(0,preferred-gap)/preferred)**2)
    def adjacent(i):
        return adjacent_cells(current,i,safe,gaps if mode=="continuous_path" else None,preferred)
    try:
        cells = _shortest_path(first,last,adjacent,current.center,math.dist,edge_cost=cost)
    except NavigationError as error:
        return {"state": "PARTIAL", "reason": error.code}
    full = [start, *cells, goal]
    if mode == "continuous_path":
        points = through_points(full, current, safe, horizon, rejected, gaps, preferred, departure)
        if len(points) < 2:
            return {"state": "PARTIAL", "reason": "no_safe_continuous_path"}
        result = {"state":"WAYPOINT", "reason":"continuous_map_route", "goal":points[1],
                  "through_points": points, "path": points, "frame": current.frame}
        if departure and len(points)>2:
            # Alternatives join the same remaining route after the second chord. Both connecting
            # chords must be legal on both maps. The session separately proves observed clearance.
            nominal = current.index(points[1])
            neighbours = list(current.neighbors(nominal))
            if gaps is not None:
                neighbours.sort(key=lambda j:(-gaps[j],math.dist(start,current.center(j))))
            candidates = [points[1]]
            for j in neighbours:
                p = current.center(j)
                if (safe(j) and all(.01 < math.dist(p,q) <= horizon for q in (start,points[2]))
                        and segment_admitted(start,p,current,safe) and segment_admitted(p,points[2],current,safe)):
                    candidates.append(p)
            result["departure_options"] = candidates
        return result
    for end in reversed(full[1:]):
        distance = math.dist(start, end)
        if any(math.dist(start,x["start"]) <= .20 and math.dist(end,x["goal"]) <= .19 for x in rejected):
            continue
        if .09 <= distance <= horizon and all(
            safe(current.index([a+(b-a)*offset for a,b in zip(start,end)]))
            for offset in (i / max(1, math.ceil(distance/.02)) for i in range(math.ceil(distance/.02)+1))
        ):
            return {"state": "WAYPOINT", "reason": "map_route_after_local_rejection" if rejected else "published_map_route",
                    "goal": end, "path": full,
                    "route_curve": rounded_route(full, current, safe, retain=(end,)),
                    "frame": current.frame}
    return {"state": "PARTIAL", "reason": "no_safe_local_segment", "path": full}
