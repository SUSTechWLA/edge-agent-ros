"""Ground place candidates from multiple registered observations and a matching free-space map."""

from collections import deque
import hashlib
import math
import re
from statistics import median
import unicodedata

from edge_contracts.occupancy import Grid, clearance_mask, finite
from exploration.survey_strategy import xy


DIGEST = re.compile(r"[a-f0-9]{64}")


def identity(value, limit=120):
    return isinstance(value, str) and 0 < len(value) <= limit and value == value.strip() and value.isprintable()


def _yaw_pose(value):
    return isinstance(value, (list, tuple)) and len(value) == 3 and all(finite(v) for v in value)


def _norm(value):
    return unicodedata.normalize("NFKC", value).casefold()


def resolve_place(body):
    """Resolve a requested stop against the semantic layer of one immutable map."""
    query, ref, markers = body["query"], body["map_ref"], body["semantic"]
    if (not identity(query) or not isinstance(ref, dict) or not identity(ref.get("id"))
            or not identity(ref.get("frame")) or not isinstance(ref.get("revision"), str)
            or not DIGEST.fullmatch(ref["revision"]) or ref.get("digest") != ref["revision"]
            or not isinstance(markers, list) or len(markers) > 128):
        raise ValueError("invalid_place_query")
    def key(label):
        return "".join(_norm(label).split())

    text = key(query)
    environment = None
    place = text
    for prefix, kind in (("室内","indoor"), ("室外","outdoor"), ("过渡区","transition")):
        if text.startswith(prefix):
            environment, place = kind, text[len(prefix):].removeprefix("的")
            break
    matches = []
    ids = set()
    for marker in markers:
        if not isinstance(marker, dict) or not identity(marker.get("entity_id")):
            raise ValueError("invalid_place_catalog")
        aliases = marker.get("aliases", [])
        if (marker["entity_id"] in ids or not isinstance(aliases, list) or len(aliases) > 64
                or not all(identity(label) for label in [marker.get("name"), *aliases])
                or marker.get("state") not in ("confirmed","proposed")):
            raise ValueError("invalid_place_catalog")
        ids.add(marker["entity_id"])
        labels = {key(label) for label in [marker["name"], *aliases]}
        if text in labels or environment and place in labels:
            if environment is None or marker.get("environment") == environment:
                matches.append(marker)

    candidates = [{k:m.get(k) for k in ("entity_id","name","state","environment","zone","zone_label","source_kind")}
                  for m in matches]
    result = dict(query=query, map_ref=ref, candidates=candidates)
    if not matches:
        return dict(result, state="unknown")
    if len(matches) > 1:
        return dict(result, state="ambiguous")
    marker, = matches
    if marker["state"] != "confirmed":
        return dict(result, state="unconfirmed")
    if marker.get("frame", ref["frame"]) != ref["frame"] or not _yaw_pose(marker.get("entrance_pose")):
        return dict(result, state="unavailable")
    # Camera-derived markers keep the actual observation source in their evidence.
    destination = {k:v for k,v in candidates[0].items() if k != "state"}
    if not destination["source_kind"] and marker.get("observations"):
        destination["source_kind"] = marker["observations"][0].get("source_kind")
    return dict(result, **destination, state="resolved", entrance_pose=marker["entrance_pose"])


def _observation(item, frame):
    return (isinstance(item, dict) and item.get("frame") == frame and identity(item.get("source_id"), 200)
            and identity(item.get("label"), 64) and identity(item.get("source_kind"), 64)
            and isinstance(item.get("evidence_ref"), str) and DIGEST.fullmatch(item["evidence_ref"])
            and finite(item.get("stamp_s")) and item["stamp_s"] > 0
            and finite(item.get("confidence")) and 0 <= item["confidence"] <= 1
            and xy(item.get("point")) and _yaw_pose(item.get("pose")) and math.dist(item["point"], item["pose"][:2]) <= 12)


def assess_semantics(body):
    grid = Grid(body["grid"])
    map_id, frame, source = body["map_id"], body["frame"], body["observations"]
    radius = body.get("footprint_radius", .28)
    pose, bounds = body.get("pose"), body.get("bounds")
    if (not identity(map_id) or frame != grid.frame or not isinstance(source, list) or len(source) > 128
            or not all(_observation(item, frame) for item in source)
            or not finite(radius) or not 0 <= radius <= 5
            or pose is not None and not xy(pose)):
        raise ValueError("invalid_semantic_observations")
    if bounds is not None and (not isinstance(bounds, list) or len(bounds) != 4 or not all(finite(v) for v in bounds)
                               or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
        raise ValueError("invalid_semantic_bounds")
    safe, _ = clearance_mask(grid, radius)
    groups = {}
    seen = set()
    for item in sorted(source, key=lambda o: (o["stamp_s"], o["source_id"], o["evidence_ref"])):
        signature = (item["source_id"], item["stamp_s"])
        if signature in seen:
            continue
        seen.add(signature)
        label = _norm(item["label"])
        clusters = groups.setdefault(label, [])
        cluster = next((group for group in clusters if math.dist(group[0]["point"], item["point"]) <= .45), None)
        if cluster is None:
            clusters.append([item])
        else:
            cluster.append(item)
    candidates = []
    for name, clusters in groups.items():
        for group in clusters:
            anchor = [median(o["point"][axis] for o in group) for axis in range(2)]
            first = group[0]
            seed = "/".join((map_id, name, *(str(round(v/.5)) for v in first["point"])))
            entity_id = "poi-" + hashlib.sha256(seed.encode()).hexdigest()[:16]
            entrance = _entrance(grid, safe, anchor, pose, group, bounds, radius)
            verified = _multiple_views(group)
            reason = ("ambiguous_location" if len(clusters) != 1 else "need_alternate_view" if not verified
                      else "no_verified_entrance" if entrance is None else "multi_view_and_reachable")
            candidates.append({"entity_id":entity_id,"name":first["label"],"anchor":anchor,
                               "entrance_pose":entrance,"state":"confirmed" if reason == "multi_view_and_reachable" else "proposed",
                               "reason":reason,"observations":group})
    return {"status":"ok","map_id":map_id,"frame":frame,"candidates":candidates}


def _multiple_views(group):
    for j, first in enumerate(group):
        if first["confidence"] < .75:
            continue
        for second in group[j+1:]:
            if (second["confidence"] >= .75 and second["evidence_ref"] != first["evidence_ref"]
                    and second["stamp_s"]-first["stamp_s"] >= .12
                    and math.dist(first["pose"][:2], second["pose"][:2]) >= .18
                    and math.dist(first["point"], second["point"]) <= .30):
                return True
    return False


def is_evidenced_marker(item, grid):
    """Recheck that a published auto-mark has registered evidence and a free stop cell."""
    observations = item.get("observations")
    if (not isinstance(observations, list) or not 2 <= len(observations) <= 128
            or not all(_observation(x, grid.frame) and _norm(x["label"]) == _norm(item["name"])
                       and math.dist(x["point"], item["anchor"]) <= .55 for x in observations)):
        return False
    cell = grid.index(item["entrance_pose"][:2])
    return cell >= 0 and grid.cells[cell] == 0 and _multiple_views(sorted(observations, key=lambda x:x["stamp_s"]))


def _entrance(grid, safe, anchor, pose, observations, bounds, radius):
    if pose is None:
        return None
    start = grid.index(pose)
    if start < 0 or not safe[start]:
        return None

    def permitted(point):
        if bounds is None:
            return True
        x0, y0, x1, y1 = bounds
        return x0+radius <= point[0] <= x1-radius and y0+radius <= point[1] <= y1-radius

    if not permitted(pose):
        return None
    # Each measured observer pose determines the side of an actual wall. Set a
    # prospective stop on that side rather than targeting the measured sign itself.
    goals = []
    for o in observations:
        ox, oy = o["pose"][:2]
        length = math.dist([ox, oy], anchor)
        if not .42 <= length <= 8:
            continue
        for stand_off in (max(radius+.3, .75), max(radius+.5, .95), max(radius+.7, 1.15), length):
            index = grid.index([anchor[0]+(ox-anchor[0])*stand_off/length, anchor[1]+(oy-anchor[1])*stand_off/length])
            if index < 0 or not safe[index]:
                continue
            point = grid.center(index)
            if permitted(point) and math.dist(point, anchor) >= max(radius+.18, .5) and index not in goals:
                goals.append(index)
    if not goals:
        return None
    nearest = set(goals)
    seen = {start}
    available = set()
    queue = deque([start])
    while queue and nearest:
        index = queue.popleft()
        if index in nearest:
            available.add(index)
            nearest.remove(index)
        for candidate in grid.neighbors(index):
            if candidate not in seen and safe[candidate] and permitted(grid.center(candidate)):
                seen.add(candidate)
                queue.append(candidate)
    first = next((i for i in goals if i in available), None)
    if first is None:
        return None
    x, y = grid.center(first)
    return [x, y, math.atan2(anchor[1]-y, anchor[0]-x)]
