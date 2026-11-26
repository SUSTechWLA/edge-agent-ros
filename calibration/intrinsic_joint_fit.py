"""Refine pinhole intrinsics and a rigid LiDAR mount using training marker and hole observations."""
import cv2
import numpy as np

from calibration import circle_target
from calibration.targets import rotation_xyzw
from edge_contracts.spatial_facts import rotation


def refine(images, object_points, paired, images_ids, k, distortion, result, limits):
    pixel_sigma, hole_sigma = (limits[key] for key in ("pixel_sigma_px","hole_sigma_m"))
    if not np.isfinite([pixel_sigma,hole_sigma]).all() or min(pixel_sigma,hole_sigma)<=0:
        raise ValueError("invalid calibration observation weights")
    pose_seed = []
    for image in images:
        ok, r, t = cv2.solvePnP(object_points,image,k,distortion)
        if not ok:
            raise ValueError("initial_board_pose_unavailable")
        pose_seed.extend(np.r_[r.ravel(),t.ravel()])
    spacing = np.array(limits["hole_spacing_m"])
    board_holes = np.column_stack((circle_target.SIGNS * spacing/2,np.zeros(4)))
    role, = result["transforms"]
    mount = result["transforms"][role]
    mount_matrix = np.array(rotation(mount["translation_m"],mount["rotation_xyzw"]))
    r = mount_matrix[:,:3]
    t = mount_matrix[:,3]
    observed, _ = circle_target._paired(paired,role,r,t,spacing)
    slot_by_id = {sample_id:slot for slot,sample_id in enumerate(images_ids)}
    measurements = dict(zip((slot_by_id[row["id"]] for row in paired),observed.reshape(-1,4,3)))
    initial = np.r_[k[0,0],k[1,1],k[0,2],k[1,2],cv2.Rodrigues(r)[0].ravel(),t,pose_seed]
    image = np.asarray(images)
    scales = np.ones(len(initial))
    scales[:4] = 100.

    def unpack(parameters):
        fx,fy,cx,cy = parameters[:4]
        camera = np.array([[fx,0,cx],[0,fy,cy],[0,0,1.]])
        mount_r = cv2.Rodrigues(parameters[4:7])[0]
        mount_t = parameters[7:10]
        board_poses = parameters[10:].reshape(-1,6)
        return camera,mount_r,mount_t,board_poses

    def residual(parameters):
        camera,mount_r,mount_t,board_poses = unpack(parameters)
        marker_error = []
        hole_error = []
        for slot, (board_pose, observed_pixels) in enumerate(zip(board_poses,image)):
            predicted, _ = cv2.projectPoints(object_points,board_pose[:3],board_pose[3:],camera,distortion)
            marker_error.extend((predicted[:,0,:]-observed_pixels).ravel()/pixel_sigma)
            if slot in measurements:
                expected = board_holes @ cv2.Rodrigues(board_pose[:3])[0].T + board_pose[3:]
                actual = measurements[slot] @ mount_r.T + mount_t
                hole_error.extend((expected-actual).ravel()/hole_sigma)
        return np.r_[marker_error,hole_error]

    parameters = initial.copy()
    error = residual(parameters)
    damping = .001
    perturbations = np.diag(scales * 1e-5)
    for _ in range(20):
        jacobian = np.stack([(residual(parameters+step)-residual(parameters-step))/(2e-5)
                             for step in perturbations],axis=1)
        hessian = jacobian.T @ jacobian
        regularizer = np.diag(np.maximum(hessian.diagonal(),1))
        delta = np.linalg.solve(hessian+damping*regularizer,-jacobian.T @ error)*scales
        proposal = parameters+delta
        new_error = residual(proposal)
        if new_error @ new_error < error @ error:
            parameters,error = proposal,new_error
            damping = max(damping/3,1e-7)
            if np.linalg.norm(delta/scales)<1e-6:
                break
        else:
            damping = min(damping*4,1e5)
    camera,mount_r,mount_t,_ = unpack(parameters)
    marker_errors = error[:image.size].reshape(-1,2)*pixel_sigma
    rms = float(np.sqrt(np.mean(np.sum(marker_errors**2,axis=1))))
    return camera,rms,mount_r,mount_t


def update_extrinsics(result, observations, profile, mount_r, mount_t):
    role, = result["transforms"]
    mount = result["transforms"][role]
    _,_,spacing,_ = circle_target.dimensions(profile)
    metrics = circle_target._residuals(observations,role,mount_r,mount_t,spacing)
    mount.update(translation_m=mount_t.tolist(),rotation_xyzw=rotation_xyzw(mount_r),fit=metrics,
                 initializer=mount["initializer"]+"+joint_metric_refinement")
    result.update(fit_sha256=circle_target.board_solver._signature(observations),
                  fit_poses=[dict(id=row["id"],camera=row["camera"]) for row in observations])
