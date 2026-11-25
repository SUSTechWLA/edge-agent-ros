"""Estimate camera_optical <- lidar from synchronized observations of a free board."""
import hashlib
import json
import math

import cv2
import numpy as np

from calibration.targets import rotation_xyzw


def _dimensions(profile):
    cols, rows = profile["inner_corners"]
    square = profile["square_m"]
    margin = profile["margin_m"]
    if (any(type(i) is not int or not 3 <= i <= 32 for i in (cols, rows))
            or not .005 <= square <= .3 or not 0 <= margin <= .3):
        raise ValueError("invalid metric checkerboard profile")
    return cols, rows, square, margin


def _plane(normal, center, **fields):
    normal = normal / np.linalg.norm(normal)
    distance = float(normal @ center)
    if distance < 0:
        normal, distance = -normal, -distance
    return dict(normal=normal.tolist(), offset_m=distance, center_m=center.tolist(), **fields)


def detect_board(rgb, k, profile):
    """Use registered camera intrinsics and metric checker corners, never a board pose."""
    cols, rows, square, _ = _dimensions(profile)
    image = np.asarray(rgb)
    K = np.asarray(k, dtype=float).reshape(3, 3)
    if (image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3 or
            not np.isfinite(K).all() or min(K[0, 0], K[1, 1]) <= 0 or
            not np.allclose(K[2], [0, 0, 1])):
        raise ValueError("invalid RGB or camera intrinsics")
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    # A blank frame is common while waiting for the operator.
    if int(gray.max()) - int(gray.min()) < 30:
        return None
    found, corners = cv2.findChessboardCornersSB(gray, (cols, rows))
    if not found:
        return None
    model = np.zeros((cols * rows, 3), dtype=float)
    model[:, :2] = np.indices((rows, cols)).reshape(2, -1)[::-1].T * square
    observed = corners.reshape(-1, 2).astype(float)
    distortion = np.asarray(profile.get("distortion_coeffs", []), dtype=float)
    ok, axis, shift = cv2.solvePnP(model, observed, K, distortion)
    if not ok:
        return None
    rot = cv2.Rodrigues(axis)[0]
    coords = model @ rot.T + shift.ravel()
    projected = cv2.projectPoints(model, axis, shift, K, distortion)[0].reshape(-1, 2)
    rms = float(np.sqrt(np.mean(np.sum((observed - projected)**2, axis=1))))
    if rms > .75 or np.any(coords[:, 2] <= 0):
        return None
    return _plane(rot[:, 2], coords.mean(axis=0), reprojection_rms_px=rms,
                  corners_px=observed.tolist(), corner_count=len(observed))


def observe_lidar(xyz, profile):
    """Return a unique board-sized planar patch within the station's capture range."""
    cols, rows, square, margin = _dimensions(profile)
    target = np.sort([(cols + 1) * square + 2 * margin, (rows + 1) * square + 2 * margin])
    cloud = np.asarray(xyz, dtype=float)
    if cloud.ndim != 2 or cloud.shape[1] != 3:
        raise ValueError("expected Nx3 sensor-frame XYZ")
    cloud = cloud[np.isfinite(cloud).all(axis=1)]
    ranges = np.linalg.norm(cloud, axis=1)
    low, high = profile.get("capture_range_m", [.4, 3.])
    cloud = cloud[(ranges >= low) & (ranges <= high)]
    rng = np.random.default_rng(591)
    if len(cloud) > 5000:
        cloud = cloud[rng.choice(len(cloud), 5000, replace=False)]
    candidates = []
    for _ in range(5):
        if len(cloud) < 25:
            break
        # Score plane hypotheses together; check finite board extent after refitting.
        triplets = cloud[rng.integers(0, len(cloud), size=(150, 3))]
        normals = np.cross(triplets[:, 1]-triplets[:, 0], triplets[:, 2]-triplets[:, 0])
        lengths = np.linalg.norm(normals, axis=1)
        good = lengths > .0001
        if not good.any():
            break
        normals = normals[good] / lengths[good, None]
        offsets = np.sum(normals * triplets[good, 0], axis=1)
        distances = np.abs(np.einsum("ij,kj->ik", cloud, normals) - offsets)
        best = np.argmax(np.count_nonzero(distances < .008, axis=0))
        near = distances[:, best] < .008
        patch = cloud[near]
        if len(patch) < 25:
            break
        center = patch.mean(axis=0)
        _, _, basis = np.linalg.svd(patch - center, full_matrices=False)
        normal = basis[2]
        near = np.abs((cloud - center) @ normal) < .009
        patch, cloud = cloud[near], cloud[~near]
        local = np.ascontiguousarray((patch - center) @ basis[:2].T, dtype=np.float32)
        sides = np.sort(cv2.minAreaRect(local)[1])
        if np.any(sides < .78 * target) or np.any(sides > 1.10 * target):
            continue
        center = patch.mean(axis=0)
        err = (patch-center) @ normal
        rms = float(np.sqrt(np.mean(err**2)))
        if rms <= .006:
            candidates.append(_plane(normal, center, point_count=len(patch), plane_rms_m=rms,
                                     sides_m=sides.tolist()))
    return candidates[0] if len(candidates) == 1 else None


def _normal(plane):
    n = np.asarray(plane["normal"], dtype=float)
    if n.shape != (3,) or not np.isfinite(n).all() or abs(np.linalg.norm(n)-1) > 1e-5:
        raise ValueError("invalid board normal")
    if not math.isfinite(plane["offset_m"]) or plane["offset_m"] <= 0:
        raise ValueError("invalid plane offset")
    center = np.asarray(plane["center_m"], dtype=float)
    if (center.shape != (3,) or not np.isfinite(center).all() or
            abs(float(n @ center)-plane["offset_m"]) > 1e-4):
        raise ValueError("invalid board centroid")
    return n


def distinct_pose(obs, prior_obs):
    """Parallel shifts along a single plane do not add independent constraints."""
    board = obs["camera"]
    n = _normal(board)
    for previous in prior_obs:
        other = previous["camera"]
        if n @ _normal(other) > math.cos(math.radians(6)) and abs(board["offset_m"]-other["offset_m"]) < .10:
            return False
    return True


def _check(observations, profile, minimum, prior=()):
    if len(observations) < minimum:
        raise ValueError("insufficient independent board poses")
    roles = profile["lidar_frames"]
    frames = [profile["camera_frame"], *roles.values()]
    if not roles or len(set(frames)) != len(frames) or not all(isinstance(f, str) and f for f in frames):
        raise ValueError("invalid sensor frame identities")
    seen = list(prior)
    ids = {o["id"] for o in seen}
    for obs in observations:
        if not isinstance(obs["id"], str) or not obs["id"] or obs["id"] in ids or not distinct_pose(obs, seen):
            raise ValueError("repeated observation or board pose")
        if set(obs["lidars"]) != set(roles):
            raise ValueError("missing paired sensor plane")
        for role in roles:
            _normal(obs["lidars"][role])
        ids.add(obs["id"])
        seen.append(obs)


def _errors(obs, role, rotation, translation):
    angles, distances = [], []
    for sample in obs:
        camera, lidar = sample["camera"], sample["lidars"][role]
        n = _normal(camera)
        cosine = n @ rotation @ _normal(lidar)
        angles.append(math.degrees(math.acos(float(np.clip(cosine, -1., 1.)))))
        point = rotation @ np.asarray(lidar["center_m"]) + translation
        distances.append(abs(float(n @ point - camera["offset_m"])))
    if max(angles) > 2. or max(distances) > .025:
        raise ValueError(f"{role}: board residual exceeds limit ({max(angles):.3f} deg, {max(distances):.4f} m)")
    return dict(count=len(obs), max_angle_deg=max(angles), max_plane_distance_m=max(distances),
                rms_plane_distance_m=float(np.sqrt(np.mean(np.square(distances)))))


def _signature(data):
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def fit_extrinsics(observations, profile):
    """Solve normal alignment then plane-offset least squares in the camera frame."""
    _check(observations, profile, 5)
    a = np.array([o["camera"]["normal"] for o in observations])
    singular = np.linalg.svd(a, compute_uv=False)
    if singular[-1]/singular[0] < .10:
        raise ValueError("insufficient independent plane normals for translation")
    transforms = {}
    for role, frame in profile["lidar_frames"].items():
        b = np.array([o["lidars"][role]["normal"] for o in observations])
        u, s, vt = np.linalg.svd(b.T @ a)
        if s[-1]/s[0] < .01:
            raise ValueError("degenerate lidar plane normals")
        correction = np.diag([1., 1., np.sign(np.linalg.det(vt.T @ u.T))])
        rotation = vt.T @ correction @ u.T
        rhs = np.array([o["camera"]["offset_m"]-o["lidars"][role]["offset_m"] for o in observations])
        translation = np.linalg.lstsq(a, rhs, rcond=None)[0]
        metrics = _errors(observations, role, rotation, translation)
        transforms[role] = dict(parent_frame=profile["camera_frame"], child_frame=frame,
                                translation_m=translation.tolist(), rotation_xyzw=rotation_xyzw(rotation),
                                fit=metrics)
    return dict(method="checkerboard_multi_plane_v1", transforms=transforms, profile_sha256=_signature(profile),
                fit_sha256=_signature(observations), singular_values=singular.tolist(),
                fit_poses=[dict(id=o["id"], camera=o["camera"]) for o in observations])


def verify_extrinsics(candidate, validation_obs, profile):
    """Independently evaluate held-out poses, preserving the fitted transform."""
    if candidate["profile_sha256"] != _signature(profile) or set(candidate["transforms"]) != set(profile["lidar_frames"]):
        raise ValueError("candidate sensor profile changed")
    _check(validation_obs, profile, 2, candidate["fit_poses"])
    verified = {}
    for role, item in candidate["transforms"].items():
        q = np.asarray(item["rotation_xyzw"], dtype=float)
        t = np.asarray(item["translation_m"], dtype=float)
        if (q.shape != (4,) or not np.isfinite(q).all() or abs(np.linalg.norm(q)-1.) > 1e-6 or
                t.shape != (3,) or not np.isfinite(t).all() or item["parent_frame"] != profile["camera_frame"]
                or item["child_frame"] != profile["lidar_frames"][role]):
            raise ValueError("invalid candidate transform")
        x, y, z, w = q
        skew = np.array([[0,-z,y],[z,0,-x],[-y,x,0]])
        rotation = np.eye(3) + 2*w*skew + 2*skew@skew
        verified[role] = dict(item, validation=_errors(validation_obs, role, rotation, t))
    return dict(status="VERIFIED", method=candidate["method"], transforms=verified,
                fit_sha256=candidate["fit_sha256"], validation_sha256=_signature(validation_obs),
                fit_ids=[o["id"] for o in candidate["fit_poses"]], validation_ids=[o["id"] for o in validation_obs],
                profile_sha256=candidate["profile_sha256"], max_allowed_angle_deg=2.,
                max_allowed_plane_distance_m=.025)
