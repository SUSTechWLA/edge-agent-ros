"""Shared grid admission for read-only planning and independent pre-dispatch checks."""
import math

from edge_contracts.occupancy import Grid, clearance_mask
from edge_contracts.semantic_costmap import monotonic_cell_admitted


def covered(points, grid, admitted):
    x, y = zip(*points)
    x0, x1 = math.floor(min(x) - 1e-9), math.floor(max(x) + 1e-9)
    y0, y1 = math.floor(min(y) - 1e-9), math.floor(max(y) + 1e-9)
    return (0 <= x0 <= x1 < grid.width and 0 <= y0 <= y1 < grid.height
            and all(admitted(i + j * grid.width) for j in range(y0, y1 + 1) for i in range(x0, x1 + 1)))


def segment_admitted(start, end, grid, admitted):
    # A closed box around each subsegment includes corner-touching cells.
    a, b = grid.local(start), grid.local(end)
    steps = max(1, math.ceil(2 * math.dist(a, b)))
    step = [(w - v) / steps for v, w in zip(a, b)]
    previous = a
    for i in range(1, steps + 1):
        current = [v + i * d for v, d in zip(a, step)]
        if not covered((previous, current), grid, admitted):
            return False
        previous = current
    return True


def route_admission(published_grid, live_grid, bounds, radius, deferred, *, semantic=None):
    published, current = Grid(published_grid), Grid(live_grid)
    def geometry(g):
        return g.frame, g.width, g.height, g.resolution, g.ox, g.oy, g.yaw
    if geometry(published) != geometry(current):
        raise ValueError("exploration.map_geometry_changed")
    clear, _ = clearance_mask(published, radius)
    live, _ = clearance_mask(current, radius)
    x0, y0, x1, y1 = bounds

    def safe(i):
        geom_ok = i >= 0 and clear[i] and live[i]
        if semantic is not None:
            if not monotonic_cell_admitted(
                geom_ok,
                risk=semantic["risk"][i] if 0 <= i < len(semantic["risk"]) else 1.0,
                traversable=semantic["traversable"][i] if 0 <= i < len(semantic["traversable"]) else 0.0,
                stale=semantic["stale"],
                risk_block=semantic["risk_block"],
                min_traversable=semantic["min_traversable"],
            ):
                return False
        elif not geom_ok:
            return False
        x, y = current.center(i)
        return (x0+radius <= x <= x1-radius and y0+radius <= y <= y1-radius
                and all(math.dist((x, y), region["center"]) > region["radius"]+radius for region in deferred))
    return current, safe
