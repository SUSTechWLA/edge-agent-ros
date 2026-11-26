"""Estimate a pinhole model and check it on unused views and marker corners."""
import math

import cv2
import numpy as np

from calibration import board_solver, circle_target
from calibration.view_coverage import image_cells


MIN_FIT, MIN_HOLDOUT = 7, 3


def sample(rgb, k, cloud, profile, paired_priors=()):
    marks = circle_target.marker_corners(rgb)
    if set(marks) != set(circle_target.IDS):
        return None
    size = list(rgb.shape[1::-1])
    model = circle_target._camera_plane(marks, k, profile)
    if not model:
        return None
    points = np.concatenate([marks[i] for i in circle_target.IDS])
    corners = model["all_marker_corners_m"]
    axis = np.subtract(corners[1],corners[0])
    n = model["normal"]
    # These angles use the current stream model only to assess collection diversity.
    tilt = [math.degrees(math.atan2(n[1],n[2])), math.degrees(math.atan2(axis[1],axis[0]))]
    diverse = board_solver.distinct_pose({"camera":model},[{"camera":view} for view in paired_priors])
    lidar = circle_target.observe_lidar(cloud, profile) if cloud is not None and diverse else None
    return dict(points=points.tolist(),size=size,tilt_deg=tilt, camera_pose=model,
                cells=sorted(image_cells(points,size)),lidar=lidar)


def pixels(row):
    result = np.asarray(row["points"], dtype=np.float64)
    w,h = row["size"]
    if (result.shape!=(16,2) or not np.isfinite(result).all()
            or np.any(result<0) or np.any(result>=[w,h])):
        raise ValueError("invalid_camera_corners")
    return result


def prepare(rows):
    if not rows or len(rows)>24 or len({r["id"] for r in rows})!=len(rows):
        raise ValueError("invalid_sample_identities")
    size = tuple(rows[0]["size"])
    if any(tuple(row["size"])!=size for row in rows):
        raise ValueError("camera_resolution_changed")
    return size, [pixels(row) for row in rows]


def fit(rows, profile, joint):
    size, images = prepare(rows)
    if len(rows)<MIN_FIT:
        raise ValueError("need_more_training_views")
    _,_,_,object_points,_ = circle_target._camera_geometry(np.eye(3),profile)
    model = [object_points.astype(np.float32)]*len(images)
    flags = cv2.CALIB_ZERO_TANGENT_DIST|cv2.CALIB_FIX_K1|cv2.CALIB_FIX_K2|cv2.CALIB_FIX_K3
    rms,k,d,_,_ = cv2.calibrateCamera(model,[p.astype(np.float32) for p in images],size,None,None,flags=flags)
    if not (.35*size[0] < min(k[0,0],k[1,1]) <= max(k[0,0],k[1,1]) < 2*size[0]
            and .1*size[0] < k[0,2] < .9*size[0] and .1*size[1] < k[1,2] < .9*size[1] and rms<1.5):
        raise ValueError("unreliable_camera_fit")
    candidate = dict(k=k.tolist(),distortion=d.ravel().tolist(),size=list(size),fit_ids=[r["id"] for r in rows],
                     train_rms_px=rms,lens_model="ideal_pinhole")
    if joint:
        observations = paired(rows,k,profile)
        candidate["extrinsics"] = circle_target.fit_extrinsics(observations,profile)
        if limits := profile.get("joint_metric_refinement"):
            from calibration import intrinsic_joint_fit
            _,_,spacing,_ = circle_target.dimensions(profile)
            refined,rms,mount_r,mount_t = intrinsic_joint_fit.refine(
                images,object_points,observations,candidate["fit_ids"],k,d,candidate["extrinsics"],
                dict(limits,hole_spacing_m=spacing))
            if not (np.isfinite(refined).all() and .35*size[0] < min(refined[0,0],refined[1,1]) <=
                    max(refined[0,0],refined[1,1]) < 2*size[0] and .1*size[0] < refined[0,2] < .9*size[0]
                    and .1*size[1] < refined[1,2] < .9*size[1] and 0.<=rms<1.5):
                raise ValueError("unreliable_joint_camera_refinement")
            fitted = paired(rows,refined,profile)
            if [o["id"] for o in fitted] != [o["id"] for o in observations]:
                raise ValueError("paired_training_geometry_changed")
            intrinsic_joint_fit.update_extrinsics(candidate["extrinsics"],fitted,profile,mount_r,mount_t)
            candidate.update(k=refined.tolist(),train_rms_px=rms,refinement="joint_metric_four_hole")
    return candidate


def paired(rows, k, profile):
    result = []
    for row in rows:
        if row["lidar"] is None:
            continue
        marks = dict(zip(circle_target.IDS,pixels(row).reshape(4,4,2)))
        model = circle_target._camera_plane(marks,k,profile)
        if model:
            result.append(dict(camera=model,lidars={"front":row["lidar"]},id=row["id"]))
    return result


def verify(candidate, rows, profile):
    size, images = prepare(rows)
    ids = [r["id"] for r in rows]
    if len(rows)<MIN_HOLDOUT or set(ids)&set(candidate["fit_ids"]) or list(size)!=candidate["size"]:
        raise ValueError("invalid_independent_camera_holdout")
    k = np.asarray(candidate["k"])
    d = np.asarray(candidate["distortion"])
    _,_,_,points,_ = circle_target._camera_geometry(k,profile)
    errors, by_view = [], []
    for observed in images:
        error = []
        for marker in range(4):
            included = np.repeat(np.arange(4)!=marker,4)
            ok,r,t = cv2.solvePnP(points[included],observed[included],k,d)
            if not ok:
                raise ValueError("holdout_pose_unobservable")
            prediction = cv2.projectPoints(points[~included],r,t,k,d)[0][:,0,:]
            error.extend(np.linalg.norm(prediction-observed[~included],axis=1).tolist())
        errors.extend(error)
        by_view.append(float(np.sqrt(np.mean(np.square(error)))))
    result = dict(validation_ids=ids,holdout_rms_px=float(np.sqrt(np.mean(np.square(errors)))),
                  holdout_max_px=max(errors),holdout_rms_by_view_px=by_view,
                  predicted_corners=len(errors))
    result["intrinsics_verified"] = result["holdout_rms_px"]<=1.5 and result["holdout_max_px"]<=3.
    if "extrinsics" in candidate:
        extrinsics = circle_target.verify_extrinsics(candidate["extrinsics"],paired(rows,k,profile),profile)
        result.update(extrinsics_verified=extrinsics.get("status")=="VERIFIED",extrinsics=extrinsics)
    return result
