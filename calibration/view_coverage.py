"""Board-relative baselines and observed image coverage for active calibration."""
import math


def lateral_m(plane, camera_position=(0.,0.,0.)):
    marker = plane.get("all_marker_corners_m")
    if not marker or len(marker) < 2:
        return None
    right = [b-a for a,b in zip(marker[0],marker[1])]
    length = math.hypot(*right)
    if length <= .01:
        return None
    # Camera is to the left of the board centre when this projection is positive.
    return sum((c-p)*axis/length for c,p,axis in zip(plane["center_m"],camera_position,right))


def image_cells(points, size):
    if not size or len(size) != 2 or min(size) <= 0:
        return set()
    w,h = size
    return {f"{('top','middle','bottom')[int(y*3/h)]}_{('left','center','right')[int(x*3/w)]}"
            for x,y in points if 0 <= x < w and 0 <= y < h}


def coverage(samples):
    offsets, cells = [], set()
    for observation in samples:
        plane = observation["camera"]
        lateral = lateral_m(plane)
        if lateral is not None and math.isfinite(lateral):
            offsets.append(lateral)
        cells |= image_cells(plane.get("corners_px",[]), plane.get("image_size_px"))
    return dict(left_m=max([0.,*offsets]), right_m=-min([0.,*offsets]), cells=sorted(cells))


def ready(report, limit):
    return min(report["left_m"],report["right_m"]) >= limit
