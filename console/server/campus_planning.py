"""Read-only projection of task-bound ROS observations into the DSH local preview service."""
import http.client
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from edge_contracts.occupancy import finite
from edge_contracts.service_directory import PluginUnavailable, ServiceSnapshot


SERVICE = "planning.local_preview"
MAX_SKEW_S = 1.0


def point(value, dimension=2):
    return isinstance(value, list) and len(value) == dimension and all(finite(v) for v in value)


def placement(event, scene):
    route = event.get("data")
    if (not isinstance(route, dict) or not scene or route.get("scene") != scene.get("id")
            or route.get("revision") != scene.get("revision") or route.get("digest") != scene.get("digest")
            or route.get("frame") != scene.get("frame")):
        return None
    return route


def request_preview(observer, task_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,159}", task_id):
        raise ValueError("expected a task identifier")
    state = observer.snapshot()
    op = state.get("active_operation")
    if not isinstance(op, str):
        return {"status": "waiting", "code": "planning.no_active_waypoint"}
    owner, separator, leg = op.rpartition("/waypoint/")
    if not separator or not re.fullmatch(r"[1-9][0-9]{0,5}", leg):
        return {"status": "waiting", "code": "planning.no_active_waypoint"}
    sources = state["sources"]
    pose = sources.get("pose", {})
    grid = sources.get("map", {})
    scene = sources.get("semantics", {}).get("data")
    events = observer.history.page(None, None, 8, {"task_id": task_id, "kind": "scene.route_planned"})["events"]
    route = next((placement(event, scene) for event in events if event.get("operation_id") == owner), None)
    if not route:
        return {"status": "waiting", "code": "planning.current_task_route_missing"}
    waypoints = route.get("waypoints")
    index = int(leg)-1
    if not isinstance(waypoints, list) or len(waypoints) <= index or not point(waypoints[index]):
        return {"status": "blocked", "code": "planning.waypoint_invalid"}
    frame = route["frame"]
    position, cells = pose.get("data"), grid.get("data")
    pose_stamp, grid_stamp = pose.get("stamp_s"), grid.get("stamp_s")
    if (not pose.get("fresh") or not grid.get("fresh") or not position or not cells
            or position.get("frame") != frame or cells.get("frame") != frame
            or not finite(pose_stamp) or not finite(grid_stamp) or abs(pose_stamp-grid_stamp) > MAX_SKEW_S):
        return {"status": "waiting", "code": "planning.observation_unsynchronized"}
    start = [position.get("x"),position.get("y")]
    if not point(start):
        return {"status": "blocked", "code": "planning.pose_invalid"}
    body = {"schema_version": 1, "task_id": task_id, "operation_id": op, "frame": frame, "grid": cells,
            "start": start, "goal": waypoints[index], "pose_stamp_s": pose_stamp, "grid_stamp_s": grid_stamp}
    directory = Path(os.getenv("EDGE_PLUGIN_DIR", "/etc/edge/plugins")) / "runtime"
    try:
        pin = ServiceSnapshot(directory).pin(SERVICE)
        result = evaluate(pin, body)
        ServiceSnapshot(directory).pin(SERVICE, expected_binding=pin["binding"])
    except PluginUnavailable as error:
        return {"status": "unavailable", "code": error.code}
    except (OSError, http.client.HTTPException, ValueError):
        return {"status": "unavailable", "code": "planning.provider_unavailable"}
    latest = observer.snapshot()
    current_sources = latest["sources"]
    if (latest["session_id"] != state["session_id"] or latest.get("active_operation") != op
            or not current_sources.get("pose", {}).get("fresh") or not current_sources.get("map", {}).get("fresh")
            or not placement({"data": route}, current_sources.get("semantics", {}).get("data"))):
        return {"status": "waiting", "code": "planning.operation_changed"}
    return {"status": result["kind"], "code": result.get("reason"), "task_id": task_id, "operation_id": op,
            "points": result["points"], "dimension": result["dimension"], "source_stamp_s": result["source_stamp_s"],
            "scene_id": route["scene"], "scene_revision": route["revision"], "frame": frame, "binding": pin["binding"],
            "motion_authorized": False}


def evaluate(pin, body):
    target = urlsplit(pin["endpoint"])
    connect = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    connection = connect(target.hostname,target.port,timeout=0.75)
    try:
        connection.request("POST",target.path or "/",body=json.dumps(body,allow_nan=False),
                           headers={"Content-Type":"application/json"})
        reply = connection.getresponse()
        payload = reply.read(65537)
        if reply.status != 200 or len(payload) > 65536:
            raise ValueError("preview rejected")
        result = json.loads(payload)
    finally:
        connection.close()
    if not isinstance(result, dict):
        raise ValueError("invalid preview envelope")
    points, dimension = result.get("points"), result.get("dimension")
    if (any(result.get(key) != body[key] for key in ("schema_version","task_id","operation_id","frame"))
            or result.get("motion_authorized") is not False or result.get("kind") not in ("planned","blocked")
            or dimension not in (2,3) or not isinstance(points,list) or len(points)>256
            or any(not point(p,dimension) for p in points)
            or (len(points)<2 if result["kind"]=="planned" else bool(points))
            or not finite(result.get("source_stamp_s"))
            or not math.isclose(result["source_stamp_s"],min(body["pose_stamp_s"],body["grid_stamp_s"]),rel_tol=0,abs_tol=1e-6)):
        raise ValueError("preview is inconsistent with the current observation")
    return result
