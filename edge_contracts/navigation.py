"""Version 1 route artifact shared by route producers and the body-local admission controller."""
import hashlib
import math

from edge_contracts.descriptor import SHA, canonical


MAX_WAYPOINTS = 256
ROUTE_DIMENSIONS = {"ground": (2,), "terrain": (2, 3), "flight": (3,), "guidance": (2, 3)}


class NavigationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and value == value.strip() and value.isprintable()


def position(value, dimensions):
    if (not isinstance(value, list) or len(value) != dimensions
            or not all(type(v) in (float, int) and -1e7 <= v <= 1e7 for v in value)):
        raise NavigationError("navigation.invalid_position")
    return [float(v) for v in value]


def digest(value):
    try:
        encoded = canonical(value)
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError) as exc:
        raise NavigationError("navigation.invalid_artifact") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def map_reference(record):
    """Bind site, map revision, coordinates, representation and every input cell or corridor."""
    if (not isinstance(record, dict) or set(record) != {"site_id", "map_id", "revision", "frame_id", "kind", "geometry"}
            or not all(identifier(record[k]) for k in ("site_id", "map_id", "revision", "frame_id", "kind"))
            or not isinstance(record["geometry"], dict)):
        raise NavigationError("navigation.invalid_map")
    return digest(record)


def route_reference(route):
    return digest({k: v for k, v in route.items() if k != "route_ref"})


def validate_route(route, *, actor_ref, map_ref, frame_id, localization_epoch, profile_ref):
    """Verify the immutable route and its independently supplied current body binding."""
    keys = {"schema_version", "route_ref", "actor_ref", "destination_ref", "map_ref", "frame_id",
            "localization_epoch", "localization_stamp_ns", "clock_id", "profile_ref", "profile_digest",
            "route_type", "algorithm_ref", "waypoints", "length_m"}
    if not isinstance(route, dict) or set(route) != keys or type(route["schema_version"]) is not int or route["schema_version"] != 1:
        raise NavigationError("navigation.invalid_route")
    binding = dict(actor_ref=actor_ref, map_ref=map_ref, frame_id=frame_id,
                   localization_epoch=localization_epoch, profile_ref=profile_ref)
    if any(not identifier(value) or value != route[key] for key, value in binding.items()):
        raise NavigationError("navigation.route_binding_mismatch")
    stamp = route["localization_stamp_ns"]
    if (not all(isinstance(route[k], str) and SHA.fullmatch(route[k]) for k in ("route_ref", "map_ref", "profile_digest"))
            or not all(identifier(route[k]) for k in ("destination_ref", "clock_id", "algorithm_ref", "route_type"))
            or not isinstance(stamp, str) or not 1 <= len(stamp) <= 19 or not stamp.isascii() or not stamp.isdecimal()
            or (len(stamp) > 1 and stamp.startswith("0")) or int(stamp) >= 2**63):
        raise NavigationError("navigation.invalid_route")
    coordinates = route["waypoints"]
    if (not isinstance(coordinates, list) or not 1 <= len(coordinates) <= MAX_WAYPOINTS
            or not isinstance(coordinates[0], list) or len(coordinates[0]) not in ROUTE_DIMENSIONS.get(route["route_type"], ())):
        raise NavigationError("navigation.invalid_route_geometry")
    dimensions = len(coordinates[0])
    points = [position(p, dimensions) for p in coordinates]
    length = math.fsum(math.dist(a, b) for a, b in zip(points, points[1:]))
    if (type(route["length_m"]) not in (int, float) or not math.isfinite(route["length_m"])
            or not math.isclose(length, route["length_m"], rel_tol=1e-12, abs_tol=1e-12)
            or route_reference(route) != route["route_ref"]):
        raise NavigationError("navigation.route_integrity")
