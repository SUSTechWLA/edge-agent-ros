"""Report which required observation failed the survey runtime's freshness gates."""


def observation_issue(now, rewind, clock, pose, grid, scan):
    if rewind:
        return {"sensor":"clock", "reason":"clock_rewound"}
    if not clock:
        return {"sensor":"clock", "reason":"missing"}
    if now-clock[1] > 1.4:
        return {"sensor":"clock", "reason":"arrival_stale", "arrival_age_s":now-clock[1], "limit_s":1.4}
    for sensor, row, limit in (("pose",pose,1.2), ("grid",grid,3.0), ("scan",scan,1.2)):
        if not row:
            return {"sensor":sensor, "reason":"missing"}
        arrival_age = now-row[1]
        if arrival_age > limit:
            return {"sensor":sensor, "reason":"arrival_stale", "arrival_age_s":arrival_age, "limit_s":limit}
        stamp_age = clock[0]-row[0]
        if not -250_000_000 <= stamp_age <= limit*1e9:
            return {"sensor":sensor, "reason":"capture_time_outside_window", "capture_age_s":stamp_age/1e9,
                    "min_age_s":-.25, "max_age_s":limit}
    for sensor, row in (("pose",pose), ("grid",grid)):
        if row[2]["frame"] != "odom":
            return {"sensor":sensor, "reason":"frame_mismatch", "frame":row[2]["frame"], "expected_frame":"odom"}
    return None
