"""Measure constant acquisition-stamp offsets from independent moving-plane tracks."""
import math
from dataclasses import dataclass

import numpy as np


# Station commissioning limits. Each candidate uses exactly the same measurements.
SEARCH_MS = 250
MAX_GAP_S = .35
MAX_UNCERTAINTY_MS = 40
MAX_HOLDOUT_DIFFERENCE_MS = 25
_LAGS_MS = np.arange(-SEARCH_MS, SEARCH_MS + 1)
_LAGS_S = _LAGS_MS / 1000


@dataclass
class _Window:
    optimum: int
    width_ms: int
    errors: np.ndarray
    camera_times: np.ndarray
    camera_distances: np.ndarray
    camera_normals: np.ndarray
    times: np.ndarray
    distances: np.ndarray
    normals: np.ndarray


def _track(rows, role, transform=None):
    if not isinstance(rows, list) or not 24 <= len(rows) <= 4096:
        raise ValueError(f"{role}: need 24 to 4096 independent board observations")
    if any(not isinstance(row, dict) or not isinstance(row.get("plane"), dict)
           or not {"normal", "offset_m"} <= row["plane"].keys() for row in rows):
        raise ValueError(f"{role}: missing measured board plane")
    stamps = [row.get("stamp_ns") for row in rows]
    if any(type(s) is not int or not 0 <= s < 2**63 for s in stamps):
        raise ValueError(f"{role}: invalid acquisition stamp")
    stamps = np.array(stamps, dtype=np.int64)
    if np.any(np.diff(stamps) <= 0):
        raise ValueError(f"{role}: duplicate or regressing acquisition stamp")
    # A single model describes one continuous recording, never a gap across clock sessions.
    if np.any(np.diff(stamps) > 1_000_000_000):
        raise ValueError(f"{role}: discontinuous acquisition window")
    normals = np.array([row["plane"]["normal"] for row in rows], dtype=float)
    distances = np.array([row["plane"]["offset_m"] for row in rows], dtype=float)
    if (normals.shape != (len(rows), 3) or distances.shape != (len(rows),)
            or not np.isfinite(normals).all() or not np.isfinite(distances).all()
            or (distances <= 0).any()
            or np.any(np.abs(np.linalg.norm(normals, axis=1) - 1.) > 1e-5)):
        raise ValueError(f"{role}: invalid measured board plane")
    if transform is not None:
        q = np.asarray(transform["rotation_xyzw"], dtype=float)
        translation = np.asarray(transform["translation_m"], dtype=float)
        if (q.shape != (4,) or not np.isfinite(q).all() or abs(np.linalg.norm(q) - 1.) > 1e-5
                or translation.shape != (3,) or not np.isfinite(translation).all()):
            raise ValueError(f"{role}: invalid spatial transform")
        # Plane n.p=d transforms as n'=Rn and d'=d+n'.t.
        v = q[:3]
        cross = np.cross(v, normals)
        normals = normals + 2 * (q[3] * cross + np.cross(v, cross))
        distances += normals @ translation
        normals[distances < 0] *= -1
        distances = np.abs(distances)
    return stamps, distances, normals


def _excited(values, role):
    spread = np.percentile(values, 95) - np.percentile(values, 5)
    rising_swing = np.max(values - np.minimum.accumulate(values))
    falling_swing = np.max(np.maximum.accumulate(values) - values)
    if spread < .14 or min(rising_swing, falling_swing) < .12:
        raise ValueError(f"{role}: insufficient motion excitation; move board towards and away")


def _search(cam_t, cam_d, src_t, src_d, role):
    """Score every shift on a common cohort without crossing interpolation gaps."""
    limit = SEARCH_MS / 1000
    selected = (src_t - limit >= cam_t[0]) & (src_t + limit <= cam_t[-1])
    for i in np.flatnonzero(np.diff(cam_t) > MAX_GAP_S):
        selected &= (src_t + limit <= cam_t[i]) | (src_t - limit >= cam_t[i + 1])
    t, d = src_t[selected], src_d[selected]
    if len(t) < 10 or t[-1] - t[0] < 1.5:
        raise ValueError(f"{role}: insufficient contiguous clock overlap")
    _excited(d, role)
    _excited(cam_d, role)
    predict = np.interp(t[None, :] + _LAGS_S[:, None], cam_t, cam_d)
    errors = np.sqrt(np.mean((predict - d) ** 2, axis=1))
    optimum = int(np.argmin(errors))
    if optimum in (0, len(errors)-1):
        raise ValueError(f"{role}: offset at search limit")
    # Include all indistinguishable minima, including aliases outside the closest valley.
    # 2 mm is a measurement floor; residual variation can widen this empirical band.
    floor = max(.002, float(errors[optimum]) * .5)
    plausible = np.flatnonzero(errors**2 <= errors[optimum]**2 + floor**2)
    width_ms = int(np.max(np.abs(plausible - optimum))) + 1
    return optimum, width_ms, errors, selected


def _estimate(camera, source, role):
    cam_stamp, cam_d, cam_n = camera
    stamp, src_d, src_n = source
    origin = int(cam_stamp[0])
    cam_t = (cam_stamp - origin) / 1e9
    src_t = (stamp - origin) / 1e9
    start, end = max(cam_t[0], src_t[0]), min(cam_t[-1], src_t[-1])
    if end - start < 5.:
        raise ValueError(f"{role}: need at least five seconds within common clock epoch")
    split = (start + end) / 2
    slices = []
    for cam_part, src_part in ((cam_t < split, src_t < split), (cam_t >= split, src_t >= split)):
        if not cam_part.any() or not src_part.any():
            raise ValueError(f"{role}: missing independent clock window")
        t, d = cam_t[cam_part], cam_d[cam_part]
        s, measured = src_t[src_part], src_d[src_part]
        optimum, width_ms, errors, used = _search(t, d, s, measured, role)
        slices.append(_Window(optimum, width_ms, errors, t, d, cam_n[cam_part],
                              s[used], measured[used], src_n[src_part][used]))

    fit, check = slices
    fit_index, check_index = fit.optimum, check.optimum
    difference = abs(fit_index - check_index)
    if difference > MAX_HOLDOUT_DIFFERENCE_MS:
        raise ValueError(f"{role}: held-out offset inconsistent; clock or motion changed")
    period_ms = max(float(np.median(np.diff(cam_t))), float(np.median(np.diff(src_t)))) * 1000
    uncertainty_ms = math.ceil(max(fit.width_ms, check.width_ms + difference, period_ms / 4))
    if uncertainty_ms > MAX_UNCERTAINTY_MS:
        raise ValueError(f"{role}: ambiguous or insufficiently resolved clock offset")
    offset_ns = int(_LAGS_MS[fit_index]) * 1_000_000
    rms = [float(section.errors[fit_index]) for section in slices]
    if max(rms) > .025:
        raise ValueError(f"{role}: fitted or held-out plane distance exceeds 25 mm")
    for w in slices:
        paired = w.times + offset_ns / 1e9
        dist_error = np.abs(np.interp(paired, w.camera_times, w.camera_distances) - w.distances)
        if np.max(dist_error) > .075:
            raise ValueError(f"{role}: local plane mismatch within clock window")
        # Check that the moving surfaces match, even for a spurious range correlation.
        ref = np.column_stack([np.interp(paired, w.camera_times, n) for n in w.camera_normals.T])
        cosine = np.sum(ref * w.normals, axis=1) / np.linalg.norm(ref, axis=1)
        if not np.all(cosine >= math.cos(math.radians(4.))):
            raise ValueError(f"{role}: moving plane normals disagree")
    uncertainty_ns = uncertainty_ms * 1_000_000
    valid = [max(int(stamp[0]), int(cam_stamp[0]) - offset_ns + uncertainty_ns),
             min(int(stamp[-1]), int(cam_stamp[-1]) - offset_ns - uncertainty_ns)]
    return dict(offset_ns=offset_ns, uncertainty_ns=uncertainty_ns, valid_raw_ns=valid,
                fit_count=len(fit.times), holdout_count=len(check.times),
                holdout_offset_ns=int(_LAGS_MS[check_index]) * 1_000_000,
                fit_rmse_m=rms[0], holdout_rmse_m=rms[1],
                median_period_ms=round(period_ms, 3))


def estimate_offsets(traces, transforms):
    """Compare original acquisition stamps; all LiDARs must pass independently.

    transforms must come from the caller's verified camera <- LiDAR spatial revision.
    Each row contains stamp_ns and the unique board plane in that sensor's frame.
    """
    if not transforms or "camera" in transforms or set(traces) != {"camera", *transforms}:
        raise ValueError("expected one independent trace per spatially calibrated sensor")
    parents = {value["parent_frame"] for value in transforms.values()}
    children = {value["child_frame"] for value in transforms.values()}
    if (len(parents) != 1 or len(children) != len(transforms) or parents & children
            or not all(isinstance(f, str) and f for f in parents | children)):
        raise ValueError("inconsistent spatial frame identities")
    camera = _track(traces["camera"], "camera")
    sources = {role: _estimate(camera, _track(traces[role], role, tf), role)
               for role, tf in transforms.items()}
    return dict(status="VERIFIED", method="moving_plane_constant_offset_v1",
                reference="camera", reference_frame=next(iter(parents)),
                offset_convention="corrected_lidar_ns = raw_lidar_ns + offset_ns",
                search_limit_ns=SEARCH_MS * 1_000_000,
                uncertainty_kind="measurement ambiguity, held-out agreement and cadence floor",
                sources=sources)
