"""Read-only spatial calibration with four through holes and FAST-Calib's ArUco layout.

All metric lengths come from the operator's target profile. LiDAR hole centers
are independently measured in a plane; neither camera pose nor nominal TF is
used to find that plane or to label its rotationally symmetric constellation.
"""
from itertools import combinations, permutations

import cv2
import numpy as np

from calibration import board_solver
from calibration.board_metrics import four_hole_lengths
from calibration.targets import rotation_xyzw


IDS = (1, 2, 4, 3)  # clockwise from the upper left, in the printed board's frame
SIGNS = np.array([[-1., -1.], [1., -1.], [1., 1.], [-1., 1.]])
PAIRS = np.array(list(combinations(range(4), 2)))
ORDERS = np.array(list(permutations(range(4))))
METHOD = "aruco_four_hole_rigid_v1"


def dimensions(profile):
    """Read full center-to-center spacings, black marker side, and hole diameter (m)."""
    side, width, height, dx, dy, diameter = four_hole_lengths(profile.get("four_hole_board"))
    return side, np.array([width, height]), np.array([dx, dy]), diameter


def marker_corners(rgb):
    """Decode all target markers in full-resolution RGB pixel coordinates."""
    image = np.asarray(rgb)
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8 or not image.size:
        raise ValueError("invalid RGB image")
    if not hasattr(cv2, "aruco"):
        raise ValueError("opencv_aruco_unavailable")
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    a = cv2.aruco
    dictionary = a.getPredefinedDictionary(a.DICT_6X6_250)
    params = a.DetectorParameters() if hasattr(a, "ArucoDetector") else a.DetectorParameters_create()
    params.cornerRefinementMethod = a.CORNER_REFINE_SUBPIX
    if hasattr(a, "ArucoDetector"):
        points, ids, _ = a.ArucoDetector(dictionary, params).detectMarkers(gray)
    else:
        points, ids, _ = a.detectMarkers(gray, dictionary, parameters=params)
    markers = {}
    for ident, corners in zip(() if ids is None else ids.ravel(), points):
        if ident in IDS:
            if ident in markers:
                return {}  # Multiple copies cannot be assigned to the same physical board.
            markers[int(ident)] = corners.reshape(4, 2).astype(float)
    return markers


def _camera_geometry(k, profile):
    side, spacing, hole_spacing, _ = dimensions(profile)
    K = np.asarray(k, dtype=float).reshape(3, 3)
    if (not np.isfinite(K).all() or min(K[0, 0], K[1, 1]) <= 0 or
            not np.allclose(K[2], [0., 0., 1.])):
        raise ValueError("invalid camera intrinsics")
    xy = np.concatenate([p*spacing/2 + SIGNS*side/2 for p in SIGNS])
    model = np.column_stack((xy, np.zeros(len(xy))))
    distortion = np.asarray(profile.get("distortion_coeffs", []), dtype=float)
    return side, hole_spacing, K, model, distortion


def _camera_plane(markers, k, profile):
    _, hole_spacing, K, model, distortion = _camera_geometry(k, profile)
    pixels = np.concatenate([markers[i] for i in IDS])
    ok, axis, t = cv2.solvePnP(model, pixels, K, distortion)
    if not ok:
        return None
    r = cv2.Rodrigues(axis)[0]
    fitted = cv2.projectPoints(model, axis, t, K, distortion)[0].reshape(-1, 2)
    residual = np.linalg.norm(fitted-pixels, axis=1)
    if np.max(residual) > 2.5 or (model @ r.T + t.ravel())[:, 2].min() <= 0:
        return None
    holes = (SIGNS * hole_spacing/2) @ r[:, :2].T + t.ravel()
    return board_solver._plane(r[:, 2], t.ravel(), holes_m=holes.tolist(), marker_ids=list(IDS),
                               all_marker_corners_m=(model@r.T+t.ravel()).tolist(),marker_modules_per_side=8,
                               corners_px=pixels.tolist(), corner_count=len(pixels),
                               reprojection_rms_px=float(np.sqrt(np.mean(residual**2))))


def _planning_plane(markers, k, profile):
    """Locate a partial board for choosing the next viewpoint. Predicted corners are not observations."""
    ids=[i for i in IDS if i in markers]
    if not 2 <= len(ids) < 4:
        return None
    side, hole_spacing, K, all_points, distortion = _camera_geometry(k,profile)
    squares=np.stack([markers[i] for i in ids])
    if squares.shape!=(len(ids),4,2) or not np.isfinite(squares).all():
        return None
    lengths=np.linalg.norm(squares-np.roll(squares,1,axis=1),axis=2)
    centers=squares.mean(axis=1)
    selected=all_points.reshape(4,4,3)[[IDS.index(i) for i in ids]]
    metric_centers=selected.mean(axis=1)
    if (lengths.min()<5. or
            max(np.linalg.norm(a-b) for a,b in combinations(metric_centers,2)) < 2.5*side or
            max(np.linalg.norm(a-b) for a,b in combinations(centers,2)) < 2.*np.median(lengths)):
        return None
    model,pixels=selected.reshape(-1,3), squares.reshape(-1,2)
    # IPPE returns both coplanar solutions. Reject unresolved mirror poses that disagree on the board geometry.
    ok,axes,translations,_=cv2.solvePnPGeneric(model,pixels,K,distortion,flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        return None
    candidates=[]
    for axis,t in zip(axes,translations):
        r=cv2.Rodrigues(axis)[0]
        xyz=all_points@r.T+t.ravel()
        fitted=cv2.projectPoints(model,axis,t,K,distortion)[0].reshape(-1,2)
        residual=np.linalg.norm(fitted-pixels,axis=1)
        rms=float(np.sqrt(np.mean(residual**2)))
        if (not np.isfinite(xyz).all() or not np.isfinite(residual).all() or xyz[:,2].min()<=0
                or r[:,2]@t.ravel() <= .25*np.linalg.norm(t) or np.max(residual)>2.5 or rms>1.3):
            continue
        candidates.append((rms,xyz,r,t.ravel()))
    if not candidates:
        return None
    candidates.sort(key=lambda row:row[0])
    rms,xyz,r,t=candidates[0]
    if any(err<=rms+.6 and np.linalg.norm(other-xyz,axis=1).max()>.08 for err,other,_,_ in candidates[1:]):
        return None
    predicted=(SIGNS*hole_spacing/2)@r[:,:2].T+t
    return board_solver._plane(r[:,2],t,usage="view_planning_only",marker_ids=ids,
                               corners_px=pixels.tolist(),corner_count=len(pixels),reprojection_rms_px=rms,
                               all_marker_corners_m=xyz.tolist(),marker_modules_per_side=8,
                               projective_hole_centers_m=predicted.tolist())


def recognize(target, rgb, k, context, settings):
    marks = marker_corners(rgb)
    if not marks:
        return []
    pixels = np.concatenate(list(marks.values()))
    h, w = rgb.shape[:2]
    lo = np.maximum(0, np.floor(pixels.min(axis=0))).astype(int)
    hi = np.minimum([w, h], np.ceil(pixels.max(axis=0))+1).astype(int)
    features = dict(marker_ids=sorted(marks), marker_corners_px={str(i):p.tolist() for i,p in marks.items()})
    profile = context["solver"]
    if len(marks) < 4:
        features["condition"] = "partial_markers"
        if profile.get("four_hole_board"):
            planning=_planning_plane(marks,k,profile)
            if planning is not None:
                features["planning_plane"]=planning
    elif not profile.get("four_hole_board"):
        features["condition"] = "board_geometry_required"
    else:
        plane = _camera_plane(marks, k, profile)
        if plane is None:
            features["condition"] = "marker_geometry_mismatch"
        else:
            features.update(board_plane=plane, condition="board_visible")
    return [dict(label=target, bbox_xyxy=np.concatenate((lo, hi)).tolist(), features=features)]


def _rim(points, seed, radius):
    """Refine a distance-map maximum using independently occupied rim sectors."""
    center = seed.copy()
    for _ in range(3):
        vectors = points-center
        distances = np.linalg.norm(vectors, axis=1)
        if np.any(distances < .78*radius):
            return None
        sel = distances < 1.23*radius
        local, dist = vectors[sel], distances[sel]
        bins = (np.arctan2(local[:, 1], local[:, 0]) * (12/np.pi)).astype(float)
        bins = np.floor(bins).astype(int) % 24
        indices = np.argsort(dist)
        _, first = np.unique(bins[indices], return_index=True)
        if len(first) < 19:
            return None
        edges = local[indices[first]]
        coeff = np.linalg.lstsq(np.column_stack((2*edges, np.ones(len(edges)))),
                                (edges**2).sum(axis=1), rcond=None)[0]
        shift = coeff[:2]
        center = center + shift
        if np.linalg.norm(center-seed) > .21*radius:
            return None
    edge_radius = np.linalg.norm(edges-shift, axis=1)
    if abs(np.median(edge_radius)-radius) > .12*radius or np.std(edge_radius) > .08*radius:
        return None
    return center


def _hole_sets(points, spacing, radius):
    """Find measured voids with circular rims; board outline is intentionally not assumed."""
    pixel = radius/21
    origin = points.min(axis=0)-2*pixel
    uv = np.rint((points-origin)/pixel).astype(np.int32)
    width, height = uv.max(axis=0)+3
    if int(width)*int(height) > 4_000_000:
        return []
    mask = np.zeros((height, width), np.uint8)
    cv2.fillConvexPoly(mask, cv2.convexHull(uv), 1)
    mask[uv[:, 1], uv[:, 0]] = 0
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    peaks = (distance >= cv2.dilate(distance, np.ones((15, 15), np.uint8)))
    peaks &= (distance >= .8*radius/pixel) & (distance <= 1.23*radius/pixel)
    n, _, _, centroids = cv2.connectedComponentsWithStats(peaks.astype(np.uint8))
    if n > 512:  # Ambiguous or unobserved region.
        return []
    centers = []
    for uv0 in centroids[1:]:
        seed = uv0*pixel+origin
        near = points[np.all(abs(points-seed) <= 1.5*radius, axis=1)]
        center = _rim(near, seed, radius)
        if center is not None and all(np.linalg.norm(center-c) > .5*radius for c in centers):
            centers.append(center)
    if len(centers) > 16:
        return []
    expected = np.sort([*spacing, *spacing, np.linalg.norm(spacing), np.linalg.norm(spacing)])
    matches = []
    for group in combinations(centers, 4):
        xy = np.array(group)
        measured = np.sort(np.linalg.norm(xy[PAIRS[:, 0]]-xy[PAIRS[:, 1]], axis=1))
        if np.all(abs(measured-expected) <= np.minimum(.015, .04*expected)):
            matches.append(xy)
    return matches


def observe_lidar(xyz, profile):
    _, _, spacing, diameter = dimensions(profile)
    cloud = np.asarray(xyz, dtype=float)
    if cloud.ndim != 2 or cloud.shape[1] != 3:
        raise ValueError("expected Nx3 sensor-frame XYZ")
    low, high = profile.get("capture_range_m", [.4, 3.])
    cloud = cloud[np.isfinite(cloud).all(axis=1)]
    ranges = np.linalg.norm(cloud, axis=1)
    cloud = cloud[(ranges >= low) & (ranges <= high)]
    rng = np.random.default_rng(1183)
    if len(cloud) > 150000:
        cloud = cloud[rng.choice(len(cloud), 150000, replace=False)]
    boards = []
    for _ in range(6):
        if len(cloud) < 120:
            break
        probe = cloud[rng.choice(len(cloud), min(len(cloud), 2500), replace=False)]
        triplets = probe[rng.integers(len(probe), size=(180, 3))]
        cross = np.cross(triplets[:, 1]-triplets[:, 0], triplets[:, 2]-triplets[:, 0])
        good = np.linalg.norm(cross, axis=1) > .001
        if not good.any():
            break
        normals = cross[good]/np.linalg.norm(cross[good], axis=1)[:, None]
        origins = triplets[good, 0]
        scores = abs(probe @ normals.T - (origins*normals).sum(axis=1)) < .009
        best = np.argmax(scores.sum(axis=0))
        near = abs((cloud-origins[best]) @ normals[best]) < .009
        patch = cloud[near]
        if len(patch) < 120:
            break
        center = patch.mean(axis=0)
        basis = np.linalg.svd(patch-center, full_matrices=False)[2]
        near = abs((cloud-center) @ basis[2]) < .009
        patch, cloud = cloud[near], cloud[~near]
        if len(patch) < 120:
            continue
        center = patch.mean(axis=0)
        coords = (patch-center) @ basis.T
        rms = float(np.sqrt(np.mean(coords[:, 2]**2)))
        if rms > .006:
            continue
        for xy in _hole_sets(coords[:, :2], spacing, diameter/2):
            holes = xy @ basis[:2] + center
            boards.append(board_solver._plane(basis[2], holes.mean(axis=0), holes_m=holes.tolist(),
                                              point_count=len(patch), plane_rms_m=rms))
        if len(boards) > 1:
            return None
    return boards[0] if len(boards) == 1 else None


ANCHOR_MIN_RATIO = .025


def _rigid(source, target):
    src, dst = np.asarray(source), np.asarray(target)
    u, s, vt = np.linalg.svd((src-src.mean(axis=0)).T @ (dst-dst.mean(axis=0)))
    if s[0] <= 1e-8 or s[1] < ANCHOR_MIN_RATIO*s[0]:
        raise ValueError("insufficient spread of target poses")
    r = vt.T @ np.diag([1., 1., np.sign(np.linalg.det(vt.T @ u.T))]) @ u.T
    return r, dst.mean(axis=0)-r@src.mean(axis=0)


def _anchors(planes):
    centers = np.array([p["center_m"] for p in planes])
    normals = np.array([board_solver._normal(p) for p in planes])
    return np.concatenate((centers, centers + .30*normals))


def score_view_geometry(camera_planes):
    """Under any rigid sensor transform the noiseless anchor singular values agree."""
    anchors = _anchors(camera_planes)
    centered = anchors - anchors.mean(axis=0)
    singular = np.linalg.svd(centered.T @ centered,compute_uv=False)
    ratio = float(singular[1] / singular[0]) if singular[0] > 1e-8 else 0.
    return {"metric":"anchor_ratio","value":ratio,"preferred_minimum":ANCHOR_MIN_RATIO}


def _holes(plane, spacing):
    holes = np.asarray(plane.get("holes_m"), dtype=float)
    if holes.shape != (4, 3) or not np.isfinite(holes).all():
        raise ValueError("invalid hole centers")
    expected = np.sort([*spacing, *spacing, np.linalg.norm(spacing), np.linalg.norm(spacing)])
    lengths = np.sort(np.linalg.norm(holes[PAIRS[:, 0]]-holes[PAIRS[:, 1]], axis=1))
    if (not np.allclose(holes.mean(axis=0), plane["center_m"], atol=1e-4) or
            np.max(abs(holes@board_solver._normal(plane)-plane["offset_m"])) > .009 or
            np.max(abs(lengths-expected)) > .015):
        raise ValueError("inconsistent hole constellation")
    return holes


def _paired(obs, role, r, t, spacing):
    matched, expected = [], []
    for o in obs:
        source = _holes(o["lidars"][role], spacing)
        target = _holes(o["camera"], spacing)
        residuals = np.linalg.norm((source@r.T+t)[ORDERS]-target, axis=2)
        order = np.argmin(np.mean(residuals**2, axis=1))
        if max(residuals[order]) > min(.09, min(spacing)*.4):
            raise ValueError("hole correspondence unresolved")
        matched.extend(source[ORDERS[order]])
        expected.extend(target)
    return np.array(matched), np.array(expected)


def _residuals(obs, role, r, t, spacing):
    metrics = board_solver._errors(obs, role, r, t)
    src, dst = _paired(obs, role, r, t, spacing)
    residual = np.linalg.norm(src@r.T+t-dst, axis=1)
    if max(residual) > .025:
        raise ValueError("independent hole center residual exceeds 25 mm")
    return dict(metrics, max_hole_error_m=float(max(residual)), rms_hole_error_m=float(np.sqrt(np.mean(residual**2))))


def _constellation_fit(observations, role, spacing):
    # Four unlabeled holes admit discrete symmetries. Retain an assignment only if
    # that same physical transform explains all training views and their signed planes.
    a = _holes(observations[0]["lidars"][role],spacing)
    b = _holes(observations[0]["camera"],spacing)
    solutions = {}
    for order in ORDERS:
        try:
            r,t = _rigid(a[order],b)
            src,dst = _paired(observations,role,r,t,spacing)
            key = src.tobytes()
            if key in solutions:
                continue
            r,t = _rigid(src,dst)
            metrics = _residuals(observations,role,r,t,spacing)
        except ValueError:
            continue
        solutions[key] = r,t,metrics
        if len(solutions) > 1:
            raise ValueError("ambiguous multi-view four-hole correspondence")
    if not solutions:
        raise ValueError("unresolved multi-view four-hole correspondence")
    return next(iter(solutions.values()))


def fit_extrinsics(observations, profile):
    _, _, spacing, _ = dimensions(profile)
    board_solver._check(observations, profile, 3)
    camera = _anchors([o["camera"] for o in observations])
    transforms = {}
    for role, frame in profile["lidar_frames"].items():
        lidar = _anchors([o["lidars"][role] for o in observations])
        try:
            r,t = _rigid(lidar,camera)
            r,t = _rigid(*_paired(observations,role,r,t,spacing))
            metrics = _residuals(observations,role,r,t,spacing)
            initializer = "plane_anchors"
        except ValueError:
            r,t,metrics = _constellation_fit(observations,role,spacing)
            initializer = "unique_training_hole_correspondence"
        transforms[role] = dict(parent_frame=profile["camera_frame"], child_frame=frame,
                                translation_m=t.tolist(), rotation_xyzw=rotation_xyzw(r), fit=metrics,
                                initializer=initializer)
    return dict(method=METHOD, transforms=transforms, profile_sha256=board_solver._signature(profile),
                fit_sha256=board_solver._signature(observations),
                fit_poses=[dict(id=o["id"], camera=o["camera"]) for o in observations])


def verify_extrinsics(candidate, validation_obs, profile):
    if candidate.get("method") != METHOD or len(candidate["fit_poses"]) < 3:
        raise ValueError("invalid four-hole candidate")
    _, _, spacing, _ = dimensions(profile)
    result = board_solver.verify_extrinsics(candidate, validation_obs, profile)
    for role, tf in result["transforms"].items():
        x, y, z, w = tf["rotation_xyzw"]
        skew = np.array([[0,-z,y], [z,0,-x], [-y,x,0]])
        rotation = np.eye(3)+2*w*skew+2*skew@skew
        tf["validation"] = _residuals(validation_obs, role, rotation, np.array(tf["translation_m"]), spacing)
    return dict(result, max_allowed_hole_error_m=.025)
