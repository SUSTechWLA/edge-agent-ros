"""Site-bounded rover maneuver geometry. Inputs are acquired in odom and base_link."""
import math


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def pose(values, size):
    return isinstance(values, (list, tuple)) and len(values) == size and all(map(finite, values))


def relative_intent(config, odom, stamp_ns, report):
    if not (pose(odom, 3) and type(stamp_ns) is int and stamp_ns > 0 and
            report.get("schema_version") == 1 and report.get("valid") is True and
            report.get("swept_coverage") is True and
            report.get("source_id") == config["report_source"] and
            report.get("calibration_ref") == config["report_calibration"] and
            report.get("ttc_footprint_ref") == config["profile_id"] and
            report.get("frame_id") == "base_link" and report.get("clock_id") == "ros" and
            type(report.get("stamp_ns")) is int and abs(stamp_ns-report["stamp_ns"]) <= 250_000_000 and
            isinstance(report.get("report_ref"), str) and report["report_ref"]):
        raise ValueError("navigation.observation_unavailable")
    near = report.get("front_min_m")
    if near is None:
        raise ValueError("navigation.front_clearance_unconfirmed")
    if not finite(near) or near <= 0:
        raise ValueError("navigation.observation_invalid")
    target = None
    if near <= config["activation_range_m"]:
        # The measured front surface defines a finite progress objective. The
        # Gateway remains responsible for visibility and clearance throughout.
        distance = min(config["max_forward_m"], near + config["clearance_beyond_surface_m"])
        if distance <= near:
            raise ValueError("navigation.maneuver_budget_insufficient")
        x, y, yaw = odom
        target = [x+distance*math.cos(yaw), y+distance*math.sin(yaw)]
        if any(abs(v) > 4 for v in target):
            raise ValueError("navigation.goal_outside_core_bounds")
    return {"odom_initial":list(odom), "odom_stamp_ns":stamp_ns, "odom_goal":target,
            "report_ref":report["report_ref"], "report_stamp_ns":report["stamp_ns"],
            "report_source":report["source_id"],"report_calibration":report["calibration_ref"],
            "footprint_profile":report["ttc_footprint_ref"],
            "front_min_m":near,"swept_coverage":True, "disposition":"pass_front_surface" if target else "clear_no_motion"}


def world_goal(intent, world_origin):
    if intent["odom_goal"] is None:
        return list(world_origin[:2])
    x, y, yaw = intent["odom_initial"]
    dx, dy = intent["odom_goal"][0]-x, intent["odom_goal"][1]-y
    correction = world_origin[2]-yaw
    return [world_origin[0]+dx*math.cos(correction)-dy*math.sin(correction),
            world_origin[1]+dx*math.sin(correction)+dy*math.cos(correction)]
