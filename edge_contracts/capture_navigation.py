"""Source-bound spatial facts for active observations. Angles use ROS body axes."""
import math
import re


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def vector(value, length):
    return isinstance(value, list) and len(value) == length and all(map(number, value))


def pose_at(stamp, row):
    if (not isinstance(row, dict) or row.get("frame") != "boot_odom"
            or not re.fullmatch(r"[a-f0-9]{32}:[1-9][0-9]*", str(row.get("epoch")))
            or type(row.get("stamp_ns")) is not int or abs(stamp-row["stamp_ns"]) > 250_000_000
            or not vector(row.get("position_m"), 3) or not vector(row.get("orientation_xyzw"), 4)
            or abs(math.hypot(*row["orientation_xyzw"])-1) > .001
            or not number(row.get("yaw_rad")) or abs(row["yaw_rad"]) > math.pi):
        raise ValueError("missing_time_matched_boot_pose")
    return {key:row[key] for key in ("frame","epoch","stamp_ns","position_m","orientation_xyzw","yaw_rad")}


def scan_at(stamp, row):
    """Keep the local sweep bounded. Null means a valid no-return beam."""
    if (not isinstance(row, dict) or type(row.get("stamp_ns")) is not int
            or abs(stamp-row["stamp_ns"]) > 300_000_000 or row.get("frame") != "fixture_clearance"
            or not number(row.get("range_m")) or not 0 < row["range_m"] <= 30
            or not number(row.get("angle_min")) or not number(row.get("angle_step"))):
        raise ValueError("missing_time_matched_perimeter")
    ranges = row.get("ranges_m")
    step = row["angle_step"]
    if (not isinstance(ranges,list) or not 180 <= len(ranges) <= 1440
            or not 0 < step < .04 or abs(step*len(ranges)-math.tau) > .05
            or not all(r is None or number(r) and 0 < r <= row["range_m"] for r in ranges)):
        raise ValueError("unknown_or_incomplete_perimeter")
    return row


def clearance(scan, xy):
    """Distance from observed returns to the swept centre line, in metres."""
    x, y = xy
    length2 = x*x + y*y
    nearest = max(0., scan["range_m"]-math.sqrt(length2))
    for i, r in enumerate(scan["ranges_m"]):
        if r is None:
            continue
        angle = scan["angle_min"] + i*scan["angle_step"]
        ox,oy = r*math.cos(angle),r*math.sin(angle)
        t = min(1., max(0., (ox*x+oy*y)/length2)) if length2 else 0.
        # Allow a beam interval for uncertainty at each observed return.
        nearest = min(nearest, math.hypot(ox-t*x,oy-t*y)-r*scan["angle_step"])
    return max(0., nearest)


def sectors(scan):
    result = dict(front=scan["range_m"], left=scan["range_m"], rear=scan["range_m"], right=scan["range_m"])
    names = tuple(result)
    for i,r in enumerate(scan["ranges_m"]):
        if r is not None:
            idx = round((scan["angle_min"] + i*scan["angle_step"])/(math.pi/2)) % 4
            result[names[idx]] = min(result[names[idx]], r)
    return {name:round(r,2) for name,r in result.items()}


def world_position(pose, local):
    x,y,z = local
    c,s = math.cos(pose["yaw_rad"]),math.sin(pose["yaw_rad"])
    bx,by,bz = pose["position_m"]
    return [bx+c*x-s*y, by+s*x+c*y, bz+z]


def compact_context(row):
    """Project only bounded, typed facts to the model, without any raw scan."""
    if not isinstance(row,dict) or type(row.get("stamp_ns")) is not int or row["stamp_ns"] <= 0:
        raise ValueError("missing_capture_navigation")
    pose = pose_at(row["stamp_ns"], row.get("robot"))
    target = row.get("target")
    area = row.get("nearest_observed_m")
    if (not isinstance(target,dict) or not vector(target.get("position_m"),3)
            or target.get("bearing") not in ("front","front_left","front_right","rear")
            or not number(target.get("range_m")) or target["range_m"] < 0
            or not isinstance(area,dict) or set(area) != {"front","left","rear","right"}
            or not all(number(v) and 0 < v <= 30 for v in area.values())):
        raise ValueError("invalid_capture_navigation")
    return dict(frame=pose["frame"], origin_epoch=pose["epoch"], stamp_ns=str(row["stamp_ns"]),
                robot_xyz_m=[round(v,3) for v in pose["position_m"]],
                robot_orientation_xyzw=[round(v,5) for v in pose["orientation_xyzw"]],
                robot_heading_deg=round(math.degrees(pose["yaw_rad"]),1),
                target_xyz_m=[round(v,3) for v in target["position_m"]], target_bearing=target["bearing"],
                target_range_m=round(target["range_m"],2), nearest_observed_m=area)
