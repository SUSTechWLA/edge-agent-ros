"""Local pinhole information after eliminating the free pose of each board observation."""
import cv2
import numpy as np


def view_information(camera_points, k, distortion, size):
    points = np.asarray(camera_points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or len(points) < 4 or np.min(points[:,2]) <= .05:
        raise ValueError("invalid candidate board projection")
    _, jacobian = cv2.projectPoints(points, np.zeros(3), np.zeros(3), k, distortion)
    pose, singular, _ = np.linalg.svd(jacobian[:,:6], full_matrices=False)
    pose = pose[:,singular > 1e-8*singular[0]]
    width,height = size
    # Estimate information in units of one percent of the image dimension, with 1 px observation noise.
    intrinsic = jacobian[:,6:10] * (.01*np.array([width,height,width,height]))
    residual = intrinsic - pose @ (pose.T @ intrinsic)
    return residual.T @ residual


def accumulated_information(planes, k, distortion, size):
    information = np.eye(4)
    for plane in planes:
        points = plane.get("all_marker_corners_m")
        if points:
            information += view_information(points,k,distortion,size)
    return information


def information_gain(accumulated, points, k, distortion, size):
    candidate = view_information(points,k,distortion,size)
    return float(np.linalg.slogdet(accumulated+candidate)[1]-np.linalg.slogdet(accumulated)[1])
