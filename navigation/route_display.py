"""Smooth an observed grid route for display using the planner's own cell admissions."""
import math
from navigation.grid_corridor import covered, segment_admitted


def _toward(start, end, distance):
    scale = distance / math.dist(start, end)
    return [a + (b - a) * scale for a, b in zip(start, end)]


def rounded_route(points, grid, admitted, *, retain=()):
    """Return lines and tangent quadratics in the original frame for the viewer."""
    raw = []
    for point in points:
        if not raw or math.dist(raw[-1], point) > 1e-7:
            raw.append(point)
    if len(raw) < 2:
        return None
    kept = {tuple(p) for p in retain}
    anchors = [raw[0]]
    stop = 0
    for limit in (i for i in range(1, len(raw)) if i == len(raw) - 1 or tuple(raw[i]) in kept):
        while stop < limit:
            index = next((i for i in range(limit, stop, -1) if segment_admitted(raw[stop], raw[i], grid, admitted)), None)
            if index is None:
                return None
            anchors.append(raw[index])
            stop = index
    segments = []
    for before, corner, after in zip(anchors, anchors[1:], anchors[2:]):
        a = [p - q for p, q in zip(corner, before)]
        b = [p - q for p, q in zip(after, corner)]
        if tuple(corner) in kept or abs(a[0] * b[1] - a[1] * b[0]) < 1e-10:
            segments.append({"end": corner})
            continue
        trim = min(.35, .4 * math.hypot(*a), .4 * math.hypot(*b))
        while trim >= grid.resolution / 10:
            entry, leave = _toward(corner, before, trim), _toward(corner, after, trim)
            # A quadratic lies inside its three-point control hull. Its
            # grid-aligned bounding box is a conservative, continuous test.
            if covered([grid.local(p) for p in (entry, corner, leave)], grid, admitted):
                segments.extend(({"end": entry}, {"control": corner, "end": leave}))
                break
            trim /= 2
        else:
            segments.append({"end": corner})
    segments.append({"end": anchors[-1]})
    return {"frame": grid.frame, "start": anchors[0], "segments": segments}
