"""Forward-only differential-drive geometry in the rover's fixed starting frame."""
import math
import sys


def wrap_yaw(angle):
    """Signed shortest turn, with a deterministic positive turn at exactly pi."""
    wrapped = math.remainder(angle, math.tau)
    return math.pi if wrapped == -math.pi else wrapped


def plan_pose(arguments):
    if (not isinstance(arguments, dict) or set(arguments) != {"frame","x_m","y_m","yaw_rad"}
            or arguments["frame"] != "base_at_start"):
        raise ValueError("expected frame=base_at_start with x_m, y_m and yaw_rad")
    values = [arguments[k] for k in ("x_m","y_m","yaw_rad")]
    if any(type(v) not in (int,float) or not -sys.float_info.max <= v <= sys.float_info.max for v in values):
        raise ValueError("pose coordinates and yaw must be finite numbers")
    x, y, yaw = map(float, values)
    distance = math.hypot(x,y)
    if not math.isfinite(distance):
        raise ValueError("translation exceeds numeric range")
    target_yaw = wrap_yaw(yaw)
    heading = wrap_yaw(math.atan2(y,x)) if distance else 0.0
    segments = []
    if heading:
        segments.append({"kind":"pivot", "angle_rad":heading})
    if distance:
        segments.append({"kind":"drive_forward", "distance_m":distance})
    finish_turn = wrap_yaw(target_yaw - heading)
    if finish_turn:
        segments.append({"kind":"pivot", "angle_rad":finish_turn})
    return {
        "schema_version":1, "kind":"relative_pose_geometry", "frame":"base_at_start",
        "axes":"x_forward_y_left_yaw_ccw", "goal":{"x_m":x,"y_m":y,"yaw_rad":target_yaw},
        "segments":segments, "clearance_checked":False, "motion_authorized":False,
    }
