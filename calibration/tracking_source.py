"""Read-only ROS adapter from the installed target detector to image-space feedback."""
import hashlib
import json
import os
from pathlib import Path

import cv2
import numpy as np

from calibration.board_ros import BoardSource
from calibration.view_planner import target_outline
from perception.target_detection import recognize


def observe(frame, profile, recognizers):
    policy = profile["view_tracking"]
    solver = dict(profile["solver"], four_hole_board=profile.get("four_hole_board"))
    rgb, k = frame["rgb"], frame["k"]
    result = recognize(policy["target"], rgb, k, {"solver":solver}, recognizers)
    detector = result["detector"]
    provider = hashlib.sha256(json.dumps(detector, sort_keys=True).encode()).hexdigest()
    box = None
    if len(result["detections"]) == 1:
        detection = result["detections"][0]
        height, width = rgb.shape[:2]
        bounds = np.asarray(detection["bbox_xyxy"], dtype=float)
        if policy.get("outline_from_board"):
            plane = detection.get("features", {}).get("board_plane")
            if plane:
                outline = target_outline(plane, profile)
                distortion = np.asarray(solver.get("distortion_coeffs", []), dtype=float)
                uv = cv2.projectPoints(outline, np.zeros(3), np.zeros(3), np.asarray(k).reshape(3,3), distortion)[0].reshape(-1,2)
                bounds = np.r_[uv.min(axis=0), uv.max(axis=0)]
            else:
                bounds = None
        if bounds is not None:
            normal = bounds / [width,height,width,height]
            if np.isfinite(normal).all() and (0 <= normal[0] < normal[2] <= 1 and 0 <= normal[1] < normal[3] <= 1):
                box = normal.tolist()
    return dict(version=1, frame=frame["frames"]["camera"], target=policy["target"],
                stamp_ns=frame["stamp_ns"], provider=provider, box=box)


def main():
    from rclpy.qos import qos_profile_sensor_data
    from std_msgs.msg import String

    path = Path(os.environ["EDGE_CALIBRATION_PROFILE"])
    profile = json.loads(path.read_bytes())
    recognizers = path.parent / "recognizers"
    cv2.setNumThreads(1)
    with BoardSource(profile, preview_only=True, node_name="edge_target_track") as source:
        publisher = source.node.create_publisher(String, profile["view_tracking"]["topic"], qos_profile_sensor_data)
        while source.ctx.ok():
            try:
                frame = source.next_rgb_frame(.35)
                if frame:
                    row = observe(frame, profile, recognizers)
                    publisher.publish(String(data=json.dumps(row, allow_nan=False)))
            except (OSError, ValueError, cv2.error) as exc:
                print("target observation unavailable", type(exc).__name__, str(exc), flush=True)


if __name__ == "__main__":
    main()
