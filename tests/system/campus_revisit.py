"""Choose a repeat observation station using the saved occupancy grid and stopped survey receipts."""
from collections import deque
import math

from exploration.frontier import Grid, _masks, finite


def _clearance(grid, point, bounds):
    """Distance to the nearest nonfree cell face, grid edge or mission boundary in metres."""
    u, v = grid.local(point)
    x, y = point
    x0, y0, x1, y1 = bounds
    resolution = grid.resolution
    nearest = min(x-x0, y-y0, x1-x, y1-y, u*resolution, v*resolution,
                  (grid.width-u)*resolution, (grid.height-v)*resolution)
    for index, value in enumerate(grid.cells):
        if value == 0:
            continue
        row, col = divmod(index, grid.width)
        dx = max(col-u, u-col-1, 0)
        dy = max(row-v, v-row-1, 0)
        nearest = min(nearest, math.hypot(dx, dy)*resolution)
    return nearest


def select_inspection_site(grid, xy, home, completed_steps, bounds):
    """Return a previously measured station reachable from both the rover and home.

    Use only received odom-frame evidence. Return None if no such station is
    distinct from both ends and offers parking clearance for the 30 cm rover
    radius plus a 5 cm margin. path_m is the four-neighbour grid route length
    from the current pose's cell centre to the station's cell centre.
    """
    observed = Grid(grid)
    if observed.frame != "odom":
        raise ValueError("station_requires_odom_frame")
    if (len(bounds) != 4 or not all(finite(v) for v in bounds)
            or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]
            or any(len(point) != 2 or not all(finite(v) for v in point) for point in (xy, home))):
        raise ValueError("invalid_station_bounds_or_pose")
    _, _, safe, _ = _masks(observed, bounds, .30)
    start, finish = observed.index(xy), observed.index(home)
    if min(start, finish) < 0 or not (safe[start] and safe[finish]):
        return None
    distances = {start: 0}
    queue = deque([start])
    while queue:
        i = queue.popleft()
        for j in observed.neighbors(i):
            if safe[j] and j not in distances:
                distances[j] = distances[i] + 1
                queue.append(j)
    if finish not in distances:
        return None

    candidates = []
    for step in completed_steps:
        point = step.get("measured_xy")
        if (step.get("mode") != "survey" or step.get("core_state") != "SUCCEEDED"
                or step.get("accepted") is not True or type(step.get("index")) is not int
                or not isinstance(point, (tuple, list)) or len(point) != 2
                or not all(finite(v) for v in point)):
            continue
        index = observed.index(point)
        home_distance = math.dist(point, home)
        if index not in distances or min(home_distance, math.dist(point, xy)) < .25:
            continue
        clearance = _clearance(observed, point, bounds)
        if clearance < .35:
            continue
        path = distances[index] * observed.resolution
        station = dict(target=list(point), source_step=step["index"], clearance_m=clearance,
                       distance_from_home_m=home_distance, path_m=path)
        candidates.append(((clearance, -path, -step["index"]), station))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None
