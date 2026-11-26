"""Read-only static rover path candidate from an admitted occupancy snapshot."""
import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math

from edge_contracts.navigation import MAX_WAYPOINTS, NavigationError, identifier, position
from edge_contracts.occupancy import Grid, clearance_mask, finite
from navigation.planner import _shortest_path


DEFAULT_CLEARANCE_M = 0.32
HORIZON_M = 1.25
MAX_BODY = 2 * 1024 * 1024
REQUEST_FIELDS = {"schema_version", "task_id", "operation_id", "frame", "grid", "start", "goal",
                  "pose_stamp_s", "grid_stamp_s"}


def blocked_cell(grid, index, pad, label):
    if index < 0:
        return f"{label}_outside_grid"
    x, y = index % grid.width, index // grid.width
    if x < pad or x >= grid.width-pad or y < pad or y >= grid.height-pad:
        return f"{label}_footprint_map_edge"
    for row in range(y-pad, y+pad+1):
        first = row*grid.width+x-pad
        if -1 in grid.cells[first:first+2*pad+1]:
            return f"{label}_footprint_unknown"
    return f"{label}_footprint_occupied"


def bounded_points(points):
    result, remaining = [points[0]], HORIZON_M
    for p in points[1:]:
        first = result[-1]
        distance = math.dist(first, p)
        if not distance:
            continue
        if distance >= remaining:
            result.append([a + (b-a) * remaining/distance for a, b in zip(first, p)])
            break
        remaining -= distance
        result.append(p)
        if len(result) == MAX_WAYPOINTS:
            break
    return result


def evaluate(request, clearance_m=DEFAULT_CLEARANCE_M):
    """Compute geometry only. The caller owns freshness, clock epoch and operation binding."""
    if (not isinstance(request, dict) or set(request) != REQUEST_FIELDS
            or type(request["schema_version"]) is not int or request["schema_version"] != 1):
        raise ValueError("bad_request")
    task_id, operation_id, frame = (request[key] for key in ("task_id", "operation_id", "frame"))
    if (not isinstance(task_id, str) or not 0 < len(task_id) <= 160 or task_id != task_id.strip()
            or not task_id.isprintable() or not identifier(frame) or not isinstance(operation_id, str)
            or not 0 < len(operation_id) <= 240 or operation_id != operation_id.strip()
            or not operation_id.isprintable()):
        raise ValueError("invalid_binding")
    pose_stamp, grid_stamp = request["pose_stamp_s"], request["grid_stamp_s"]
    if not all(finite(stamp) and stamp >= 0 for stamp in (pose_stamp, grid_stamp)):
        raise ValueError("invalid_sample_stamp")
    raw_grid = request["grid"]
    if not isinstance(raw_grid, dict) or not isinstance(raw_grid.get("cells"), list):
        raise ValueError("invalid_grid")
    if raw_grid.get("frame") != frame:
        raise ValueError("frame_mismatch")
    try:
        grid = Grid({**raw_grid, "origin": [raw_grid[key] for key in ("origin_x", "origin_y", "origin_yaw")]})
        start, goal = position(request["start"], 2), position(request["goal"], 2)
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("invalid_grid") from exc
    result = {"schema_version": 1, "task_id": task_id, "operation_id": operation_id, "frame": frame,
              "source_stamp_s": min(pose_stamp, grid_stamp), "dimension": 2,
              "motion_authorized": False}

    def blocked(reason):
        return {**result, "points": [], "kind": "blocked", "reason": reason}

    distance = math.dist(start, goal)
    if distance < 1e-6:
        return blocked("at_target")
    target = goal if distance <= HORIZON_M else [a + (b-a) * HORIZON_M/distance for a, b in zip(start, goal)]
    origin, destination = grid.index(start), grid.index(target)
    safe, pad = clearance_mask(grid, clearance_m)
    for label, index in (("start", origin), ("target", destination)):
        if index < 0 or not safe[index]:
            return blocked(blocked_cell(grid, index, pad, label))
    first, last = sorted((origin, destination))
    same_row = first // grid.width == last // grid.width
    same_column = first % grid.width == last % grid.width
    stride = 1 if same_row else grid.width
    if (same_row or same_column) and all(safe[i] for i in range(first, last+1, stride)):
        points = [start, target]
    else:
        # Keep the search close to this view's horizon, including endpoint-cell rounding.
        radius = HORIZON_M + math.sqrt(2) * grid.resolution

        def adjacent(index):
            return (i for i in grid.neighbors(index) if safe[i] and math.dist(start, grid.center(i)) <= radius)

        try:
            centers = _shortest_path(origin, destination, adjacent, grid.center, math.dist)
        except NavigationError as exc:
            return blocked("local_path_too_long" if exc.code == "navigation.route_too_long" else "local_path_unreachable")
        points = bounded_points([start, *centers, target])
    return {**result, "points": points, "kind": "planned", "reason": "static_occupancy_preview"}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, clearance_m, **kwargs):
        self.clearance_m = clearance_m
        super().__init__(*args, **kwargs)

    def reply(self, status, payload):
        data = json.dumps(payload, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/evaluate":
            self.reply(404, {"error": "unknown_endpoint"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            self.reply(413 if length > MAX_BODY else 400, {"error": "invalid_body_length"})
            return
        self.connection.settimeout(3)
        try:
            result = evaluate(json.loads(self.rfile.read(length)), self.clearance_m)
        except (ValueError, OverflowError, RecursionError) as exc:
            self.reply(400, {"error": "bad_request" if isinstance(exc, json.JSONDecodeError) else str(exc)})
            return
        except TimeoutError:
            self.reply(408, {"error": "request_timeout"})
            return
        self.reply(200, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8181)
    parser.add_argument("--clearance", type=float, default=DEFAULT_CLEARANCE_M)
    args = parser.parse_args()
    if not finite(args.clearance) or args.clearance < 0:
        parser.error("--clearance must be nonnegative and finite")
    with ThreadingHTTPServer((args.bind, args.port), partial(Handler, clearance_m=args.clearance)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
