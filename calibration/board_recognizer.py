"""Checkerboard implementation of the target detection port."""
import math
import numpy as np

from calibration.board_solver import detect_board


def recognize(target, rgb, k, context, settings):
    plane = detect_board(rgb, k, context["solver"])
    if plane is None:
        return []
    corners = np.asarray(plane["corners_px"])
    # The bounding box encloses the actual measured inner-corner region.
    lo, hi = corners.min(axis=0), corners.max(axis=0)
    box = [max(0, math.floor(lo[0])), max(0, math.floor(lo[1])),
           min(rgb.shape[1], math.ceil(hi[0])+1), min(rgb.shape[0], math.ceil(hi[1])+1)]
    return [dict(label=target, bbox_xyxy=box, features={"checkerboard_plane":plane})]
