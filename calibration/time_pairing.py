"""Evaluate a single RGB/LiDAR association against the station's clock evidence."""


def _integer(value):
    return type(value) is int


def assess_pair(clock_status, request, maximum_error_ns):
    """Return an association decision. All time quantities are integer nanoseconds.

    A radius bounds the distance from the header time to any contributing
    acquisition instant. A whole LiDAR sweep can have a much larger radius
    than a single point with its own timestamp.
    """
    if not _integer(maximum_error_ns) or maximum_error_ns <= 0:
        raise ValueError("invalid_station_pairing_budget")
    keys = ("robot_id", "environment", "profile_sha256", "spatial_revision", "clock_epoch", "revision")
    names = ("lidar_stamp_ns", "camera_stamp_ns", "lidar_radius_ns", "camera_radius_ns", "budget_ns")
    if (not isinstance(request, dict) or not isinstance(request.get("source"), str)
            or not all(_integer(request.get(k)) and request[k] >= 0 for k in names)
            or not request["budget_ns"]):
        raise ValueError("invalid_pair_request")
    if clock_status.get("state") != "VERIFIED":
        return {"accepted": False, "reason": "clock_unverified"}
    for key in keys:
        if clock_status.get(key) in (None, "") or clock_status[key] != request.get(key):
            return {"accepted": False, "reason": "clock_binding_mismatch", "field": key}
    source = clock_status.get("sources", {}).get(request["source"])
    if not source:
        return {"accepted": False, "reason": "source_not_calibrated"}
    offset, uncertainty = source.get("offset_ns"), source.get("uncertainty_ns")
    window = source.get("valid_raw_ns")
    if (not _integer(offset) or not _integer(uncertainty) or uncertainty < 0
            or not isinstance(window, (list, tuple)) or len(window) != 2
            or not all(_integer(t) for t in window) or window[0] > window[1]):
        return {"accepted": False, "reason": "invalid_clock_evidence"}
    lidar, camera = request["lidar_stamp_ns"], request["camera_stamp_ns"]
    lr, cr = request["lidar_radius_ns"], request["camera_radius_ns"]
    if (lidar - lr < window[0] or lidar + lr > window[1]
            or camera - offset - cr < window[0] or camera - offset + cr > window[1]):
        return {"accepted": False, "reason": "outside_observed_clock_interval"}
    corrected = lidar + offset
    error = abs(corrected - camera) + uncertainty + lr + cr
    budget = min(request["budget_ns"], maximum_error_ns)
    return {"accepted": error <= budget, "reason": "within_budget" if error <= budget else "time_error_exceeds_budget",
            "source": request["source"], "corrected_lidar_ns": corrected,
            "temporal_error_bound_ns": error, "budget_ns": budget, "revision": clock_status["revision"]}
