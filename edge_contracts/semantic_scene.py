"""A bounded, evidence-addressed projection shared by scene providers and model clients."""
import math
import re


DIGEST = re.compile(r"[0-9a-f]{64}")
KINDS = {"object", "sensor", "place", "region", "viewpoint"}
RELATIONS = {"observed_by", "approach_to", "inside", "adjacent"}


def _label(value, size=96):
    return isinstance(value, str) and 0 < len(value) <= size and value.isprintable()


def _vector(value, dimensions):
    return (isinstance(value, (list, tuple)) and len(value) in dimensions and
            all(type(n) in (int, float) and math.isfinite(n) and abs(n) <= 1e7 for n in value))


def compact_scene(value):
    """Only return the documented schema. Raw points, images and provider prose stay in receipts."""
    if not isinstance(value, dict) or type(value.get("version")) != int or value["version"] != 1:
        raise ValueError("scene_version")
    scope, frame, source = (value.get(k) for k in ("scope", "frame", "source_ref"))
    stamp = value.get("stamp_ns")
    if (scope not in ("sensor_snapshot", "map_revision") or not _label(frame)
            or not isinstance(source, str) or not DIGEST.fullmatch(source)
            or not isinstance(stamp, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", stamp)):
        raise ValueError("scene_identity")
    ref = value.get("map_ref")
    if scope == "map_revision":
        if (not isinstance(ref, dict) or not _label(ref.get("id")) or ref.get("frame") != frame
                or ref.get("revision") != source or ref.get("digest") != source):
            raise ValueError("scene_map_binding")
        ref = {k:ref[k] for k in ("id", "revision", "frame")}
        ref["digest"] = source
    elif ref is not None:
        raise ValueError("scene_local_frame")
    nodes, edges = value.get("nodes"), value.get("edges")
    if not isinstance(nodes, list) or len(nodes) > 12 or not isinstance(edges, list) or len(edges) > 16:
        raise ValueError("scene_budget")
    clean, ids = [], set()
    for row in nodes:
        if (not isinstance(row, dict) or not _label(row.get("id")) or row["id"] in ids
                or not _label(row.get("label")) or row.get("kind") not in KINDS
                or row.get("state") not in ("observed", "not_detected", "confirmed", "proposed", "unknown")):
            raise ValueError("scene_entity")
        node = {k:row[k] for k in ("id", "label", "kind", "state")}
        for key, dims in (("position_m", (2, 3)), ("plane_normal", (3,))):
            if key in row:
                if not _vector(row[key], dims):
                    raise ValueError("scene_geometry")
                if key == "plane_normal" and abs(math.hypot(*row[key])-1) > .01:
                    raise ValueError("scene_plane_normal")
                node[key] = [round(v, 5) for v in row[key]]
        if "yaw_rad" in row:
            if not _vector([row["yaw_rad"]], (1,)) or abs(row["yaw_rad"]) > math.pi + 1e-5:
                raise ValueError("scene_heading")
            node["yaw_rad"] = round(row["yaw_rad"], 6)
        clean.append(node)
        ids.add(row["id"])
    links = []
    for link in edges:
        if (not isinstance(link, (list, tuple)) or len(link) != 3 or
                not all(isinstance(s, str) for s in link) or link[0] not in ids
                or link[2] not in ids or link[1] not in RELATIONS):
            raise ValueError("scene_relation")
        links.append(list(link))
    omitted = value.get("omitted", 0)
    if type(omitted) != int or not 0 <= omitted <= 1_000_000:
        raise ValueError("scene_omitted")
    scene = dict(version=1, scope=scope, frame=frame, source_ref=source, stamp_ns=stamp,
                 map_ref=ref, nodes=clean, edges=links, omitted=omitted)
    if "detector" in value:
        det = value["detector"]
        if (not isinstance(det, dict) or not _label(det.get("id")) or not _label(det.get("version"))
                or not isinstance(det.get("manifest_sha256"), str) or not DIGEST.fullmatch(det["manifest_sha256"])):
            raise ValueError("scene_detector")
        scene["detector"] = {k:det[k] for k in ("id", "version", "manifest_sha256")}
    return scene


def target_scene(*, target, frame, stamp_ns, archive, plane, detector, lidar_roles, seen):
    """One identified target in its observed optical frame; this snapshot has no map transform."""
    obj = dict(id="target", label=target, kind="object", state="observed",
               position_m=plane["center_m"], plane_normal=plane["normal"])
    nodes = [obj, dict(id="camera", label=frame, kind="sensor", state="observed")]
    edges = [["target", "observed_by", "camera"]]
    for role in sorted(lidar_roles)[:10]:
        ident = "lidar." + role
        nodes.append(dict(id=ident, label=lidar_roles[role], kind="sensor",
                          state="observed" if role in seen else "not_detected"))
        if role in seen:
            edges.append(["target", "observed_by", ident])
    return compact_scene(dict(version=1, scope="sensor_snapshot", frame=frame, source_ref=archive,
                              stamp_ns=str(stamp_ns), nodes=nodes, edges=edges, detector=detector,
                              omitted=max(0, len(lidar_roles)-10)))
