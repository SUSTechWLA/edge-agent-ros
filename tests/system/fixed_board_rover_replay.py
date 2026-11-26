"""Isolated four-hole geometry replay with a fixed board and a planar moving rover."""
import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from calibration import circle_target
from four_hole_scene import HOLES, K, PROFILE, render


def rotation_vector(vector):
    return cv2.Rodrigues(np.asarray(vector, dtype=float))[0]


BOARD_CENTER = np.array([2.79, 0.08, .53])
BODY_FROM_OPTICAL = np.array([[0., 0., 1.], [-1., 0., 0.], [0., -1., 0.]])
WORLD_FROM_BOARD = BODY_FROM_OPTICAL @ rotation_vector((.04, -.06, .02))
CAMERA_IN_BODY = np.array([.16, 0., .34])
CAMERA_FROM_LIDAR = rotation_vector((.05, .18, -.10))
LIDAR_IN_CAMERA = np.array([-.12, .05, .02])
PROFILE_FRONT = dict(PROFILE, lidar_frames={"front": "lidar_front_optical"})

# These are independent stationary samples, not a commanded trajectory.
POSES = (
    (0.06, 0.02, 0.00),
    (0.17, 0.24, 0.12),
    (0.30, -0.24, -0.12),
    (0.28, 0.13, 0.14),
    (0.05, -0.22, -0.15),
)


def scene_points(seed, lidar_in_world):
    """Sample fixed world surfaces. The depth samples exclude the four through holes."""
    rng = np.random.default_rng(seed)
    samples = rng.uniform([-.7, -.5], [.7, .5], (12500, 2))
    for hole in HOLES:
        samples = samples[np.linalg.norm(samples-hole, axis=1) > .12]
    board = np.column_stack((samples, rng.normal(0, .0012, len(samples))))
    world_board = board @ WORLD_FROM_BOARD.T + BOARD_CENTER
    world_background = np.column_stack((
        rng.normal(3.10, .001, 4000),
        rng.uniform(-.9, 1.0, 4000),
        rng.uniform(.02, 1.28, 4000),
    ))
    # Clip background returns whose rays pass through solid board material.
    rays = world_background - lidar_in_world
    normal = WORLD_FROM_BOARD[:, 2]
    hit_fraction = ((BOARD_CENTER - lidar_in_world) @ normal) / (rays @ normal)
    hit_points = lidar_in_world + hit_fraction[:, None] * rays
    hit_uv = (hit_points - BOARD_CENTER) @ WORLD_FROM_BOARD[:, :2]
    through_hole = (np.linalg.norm(hit_uv[:, None] - HOLES, axis=2) < .12).any(axis=1)
    occluded = ((hit_fraction > 0) & (hit_fraction < 1)
                & (np.abs(hit_uv) <= [.7, .5]).all(axis=1) & ~through_hole)
    return np.concatenate((world_board, world_background[~occluded]))


def observe(ident, pose, seed):
    x, y, yaw = pose
    world_from_body = rotation_vector((0., 0., yaw))
    world_from_camera = world_from_body @ BODY_FROM_OPTICAL
    camera_in_world = np.array([x, y, 0.]) + world_from_body @ CAMERA_IN_BODY
    camera_from_board = world_from_camera.T @ WORLD_FROM_BOARD
    board_in_camera = world_from_camera.T @ (BOARD_CENTER - camera_in_world)
    rgb = render(cv2.Rodrigues(camera_from_board)[0].ravel(), board_in_camera)
    detection, = circle_target.recognize("four_hole_board", rgb, K,
                                         {"solver": PROFILE_FRONT}, {})
    features = detection["features"]
    if "board_plane" not in features:
        raise ValueError(f"fixture camera pose {ident}: {features['condition']}")
    lidar_in_world = camera_in_world + world_from_camera @ LIDAR_IN_CAMERA
    points_in_camera = (scene_points(seed, lidar_in_world) - camera_in_world) @ world_from_camera
    points_in_lidar = (points_in_camera - LIDAR_IN_CAMERA) @ CAMERA_FROM_LIDAR
    lidar_plane = circle_target.observe_lidar(points_in_lidar, PROFILE_FRONT)
    if lidar_plane is None:
        raise ValueError(f"fixture LiDAR pose {ident} failed independent hole extraction")
    return {"id": ident, "camera": features["board_plane"], "lidars": {"front": lidar_plane}}


def errors(transform):
    x, y, z, w = transform["rotation_xyzw"]
    skew = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
    fitted = np.eye(3) + 2*w*skew + 2*skew@skew
    trace = np.trace(fitted.T @ CAMERA_FROM_LIDAR)
    return {
        "translation_m": float(np.linalg.norm(transform["translation_m"] - LIDAR_IN_CAMERA)),
        "rotation_deg": float(np.degrees(np.arccos(np.clip((trace - 1.) / 2., -1., 1.)))),
    }


def run():
    observations = [observe(f"pose-{i+1}", pose, 829+i) for i, pose in enumerate(POSES)]
    training, held_out = observations[:3], observations[3:]
    candidate = circle_target.fit_extrinsics(training, PROFILE_FRONT)
    verified = circle_target.verify_extrinsics(candidate, held_out, PROFILE_FRONT)
    fit_ids, held_out_ids = verified["fit_ids"], verified["validation_ids"]
    if (verified["status"] != "VERIFIED" or fit_ids != [o["id"] for o in training]
            or held_out_ids != [o["id"] for o in held_out] or set(fit_ids) & set(held_out_ids)):
        raise ValueError("calibration verification did not prove independent held-out poses")
    transform = verified["transforms"]["front"]
    truth_errors = errors(transform)
    if truth_errors["translation_m"] > .015 or truth_errors["rotation_deg"] > .75:
        raise ValueError(f"solver exceeds fixture truth-error limit: {truth_errors}")

    # Keep range and heading fixed; changing only the lateral offset is degenerate.
    degenerate = [observe(f"lateral-{i+1}", (.17, y, 0.), 897+i)
                  for i, y in enumerate((-.18, 0., .18))]
    try:
        circle_target.fit_extrinsics(degenerate, PROFILE_FRONT)
    except ValueError as exc:
        if "repeated observation or board pose" not in str(exc):
            raise
        negative_reason = str(exc)
    else:
        raise ValueError("lateral-only sequence was accepted as an independent fit")

    return {
        "scope": "synthetic RGB and metric point cloud replay; planar rigid-body pose transforms",
        "world_board_center_m": BOARD_CENTER.tolist(),
        "world_from_board_rotation": WORLD_FROM_BOARD.tolist(),
        "pose_columns": ["x_forward_m", "y_left_m", "yaw_rad"],
        "robot_poses": dict(zip((o["id"] for o in observations), POSES)),
        "fit_ids": fit_ids,
        "holdout_ids": held_out_ids,
        "independent_truth": {
            "camera_from_lidar_rotation": CAMERA_FROM_LIDAR.tolist(),
            "lidar_in_camera_m": LIDAR_IN_CAMERA.tolist(),
        },
        "status": verified["status"],
        "validation": transform["validation"],
        "truth_error": truth_errors,
        "negative_control": {"lateral_offsets_m": [-.18, 0., .18], "outcome": "refused",
                             "reason": negative_reason},
        "solver_sha256": hashlib.sha256(Path(circle_target.__file__).read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    options = parser.parse_args()
    text = json.dumps(run(), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    options.out.write_text(text, encoding="utf-8")
    print(text)
