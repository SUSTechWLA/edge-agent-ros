"""Plan one bounded straight leg using an observed ROS occupancy grid, in metres."""
from collections import deque
import math

from edge_contracts.occupancy import Grid, finite


def _masks(grid, bounds, radius):
    """Integral image of nonfree cells allows constant-cost footprint checks per cell."""
    w, h = grid.width, grid.height
    stride = w+1
    prefix = [0]*(stride*(h+1))
    inside = bytearray(w*h)
    safe = bytearray(w*h)
    unknown = bytearray(w*h)
    x0, y0, x1, y1 = bounds
    pad = math.ceil(radius/grid.resolution)
    for y in range(h):
        row_sum = 0
        for x in range(w):
            i = y*w+x
            row_sum += (grid.cells[i] != 0)
            prefix[(y+1)*stride+x+1] = prefix[y*stride+x+1] + row_sum
            wx, wy = grid.center(i)
            inside[i] = x0 <= wx <= x1 and y0 <= wy <= y1
            unknown[i] = inside[i] and grid.cells[i] < 0
            safe[i] = (pad <= x < w-pad and pad <= y < h-pad and
                       x0+radius <= wx <= x1-radius and y0+radius <= wy <= y1-radius)
    for i, allowed in enumerate(safe):
        if allowed:
            y, x = divmod(i, w)
            a, b = (y-pad)*stride, (y+pad+1)*stride
            left, right = x-pad, x+pad+1
            safe[i] = prefix[b+right]-prefix[a+right]-prefix[b+left]+prefix[a+left] == 0
    return inside, unknown, safe, pad


def plan_frontier(grid, pose_xy, *, bounds, footprint_radius=0.28, min_step=0, max_step=0.65, visited=()):
    """Return WAYPOINT, COMPLETE, PARTIAL or BLOCKED. Only zero-valued cells can be traversed.

    bounds is [xmin,ymin,xmax,ymax] in the grid frame. visited is a sequence of
    already reached waypoint coordinates for this attempt. The next leg is one
    straight subset of a reachable path. Set min_step above the controller's
    arrival tolerance. Re-observe after executing that leg.
    """
    result = {"state": "BLOCKED", "goal": None, "path": [], "known_cells": 0, "total_cells": 0}
    try:
        g = Grid(grid)
        x0, y0, x1, y1 = bounds
        px, py = pose_xy
        if not all(finite(v) for v in (*bounds,px,py,footprint_radius,min_step,max_step)):
            raise ValueError("invalid_limits_or_pose")
        if x0 >= x1 or y0 >= y1 or footprint_radius < 0 or max_step <= 0 or not 0 <= min_step <= max_step:
            raise ValueError("invalid_limits_or_pose")
        if footprint_radius > min(x1-x0,y1-y0,g.width*g.resolution,g.height*g.resolution)/2:
            raise ValueError("pose_without_clearance")
        if len(visited) > 4096 or any(len(p) != 2 or not all(finite(v) for v in p) for p in visited):
            raise ValueError("invalid_visited")
        for point in ((x0,y0),(x1,y0),(x0,y1),(x1,y1)):
            u,v = g.local(point)
            if not (-1e-7 <= u <= g.width+1e-7 and -1e-7 <= v <= g.height+1e-7):
                raise ValueError("bounds_outside_grid")
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        return {**result, "reason": str(exc)}

    inside, unknown, safe, pad = _masks(g,bounds,footprint_radius)
    total = sum(inside)
    result.update(known_cells=total-sum(unknown), total_cells=total, frame=g.frame)
    if not (x0+footprint_radius <= px <= x1-footprint_radius and
            y0+footprint_radius <= py <= y1-footprint_radius):
        return {**result, "reason": "pose_without_clearance"}
    start = g.index(pose_xy)
    if start < 0 or not safe[start]:
        return {**result, "reason": "pose_without_clearance"}
    if not any(unknown):
        return {**result, "state": "COMPLETE", "reason": "roi_observed"}

    # Approach the free side of the frontier through free cells. A wall must not
    # create a candidate simply because unknown cells lie close on its far side.
    ring = {i for i,v in enumerate(g.cells) if inside[i] and v == 0
            and any(unknown[j] for j in g.neighbors(i))}
    seen = set(ring)
    candidates = set()
    for _ in range(2*pad+2):
        candidates.update(i for i in ring if safe[i])
        if not ring:
            break
        ring = {j for i in ring for j in g.neighbors(i)
                if inside[j] and g.cells[j] == 0 and j not in seen}
        seen.update(ring)

    # Path admission and frontier discovery are independent. Never cross an unknown gap.
    # Exclude the current stop and already observed vantages within one position tolerance.
    stops = [pose_xy, *visited]
    min_advance = max(g.resolution, min(.25, max_step/2))
    # Compare only nearby stops while searching. A long journey never requires
    # scanning the whole fine-resolution grid once for each previous stop.
    stop_bins = {}
    def bin_at(point):
        return (math.floor((point[0]-x0)/min_advance), math.floor((point[1]-y0)/min_advance))
    for stop in stops:
        if x0 <= stop[0] <= x1 and y0 <= stop[1] <= y1:
            stop_bins.setdefault(bin_at(stop),set()).add(tuple(stop))
    def already_seen(i):
        point = g.center(i)
        bx,by = bin_at(point)
        return any(math.dist(point,p)<min_advance
                   for x in range(bx-1,bx+2) for y in range(by-1,by+2)
                   for p in stop_bins.get((x,y),()))
    # Estimate potential new observations, not visibility through obstacles.
    # A prefix sum keeps ranking independent of the pixel area of the lookahead window.
    stride = g.width+1
    summed = [0]*(stride*(g.height+1))
    for y in range(g.height):
        acc = 0
        for x in range(g.width):
            acc += unknown[y*g.width+x]
            summed[(y+1)*stride+x+1] = summed[y*stride+x+1]+acc
    look = 2*pad+3
    def gain(i):
        y,x = divmod(i,g.width)
        top, bottom = max(0,y-look)*stride, min(g.height,y+look+1)*stride
        left, right = max(0,x-look), min(g.width,x+look+1)
        return summed[bottom+right]-summed[top+right]-summed[bottom+left]+summed[top+left]
    # Seed the BFS with straight rays. A ray becomes a normal search path once
    # its first leg is long enough to move the controller. Short ray prefixes
    # do not mark a cell visited: another admissible path may reach that cell.
    reached = {start}
    queue = deque([(start,0,0,start,False)])
    short_leg = clipped_leg = False
    target = None
    goal = direction = None
    best = None
    while queue:
        i, distance, heading, first_end, admitted = queue.popleft()
        if admitted and i in candidates and not already_seen(i):
            new_area = gain(i)
            rank = (new_area/(distance+look),new_area,-distance,-i)
            if best is None or rank > best:
                target, goal, direction, best = i, first_end, heading, rank
        for j in g.neighbors(i):
            if not safe[j] or j == start:
                continue
            step = j-i
            straight = heading == 0 or (i == first_end and heading == step)
            if not admitted and not straight:
                continue
            end = first_end
            if straight:
                advance = math.dist(g.center(j),pose_xy)
                if advance <= max_step+1e-9:
                    end = j
                    if not admitted and advance < min_step:
                        short_leg = True
                        queue.append((j,distance+1,heading or step,end,False))
                        continue
                elif not admitted:
                    clipped_leg = True
                    continue
            if j not in reached:
                reached.add(j)
                queue.append((j,distance+1,heading or step,end,True))
    if target is None:
        if len(reached) == 1 and (short_leg or clipped_leg):
            reason = "no_leg_above_controller_tolerance" if short_leg else "step_below_grid_spacing"
            return {**result, "reason":reason}
        return {**result, "state":"PARTIAL", "reason":"no_new_reachable_frontier"}
    leg = [g.center(i) for i in range(start+direction,goal+direction,direction)]
    return {**result, "state":"WAYPOINT", "reason":"reachable_frontier", "goal":leg[-1],
            "path":[list(pose_xy), *leg], "frontier":g.center(target),"potential_unknown_cells":best[1]}
