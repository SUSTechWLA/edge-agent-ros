"""Read-only observation of the requested initial board placement on a commissioned rover."""
import math

import numpy as np

from calibration import checks
from calibration.board_clip import stationary
from edge_contracts.spatial_facts import rotation


GUIDANCE = "请将标定板竖直放在车头正前方，板底落地，四个黑码正对相机。让前雷达看全四孔，保持板和车静止。"


def placement_sample(profile, inspect, observation):
    """Classify one measured RGB and LiDAR pair. Ground contact is estimated in the surveyed base frame."""
    gate=profile["front_placement"]
    role=gate["lidar_role"]
    camera=profile["camera"]
    if (inspect.get("camera_frame")!=camera["frame"] or profile["solver"]["camera_frame"]!=camera["frame"]
            or not inspect.get("camera_pose_observed") or inspect.get("camera_detections")!=1
            or set(inspect.get("camera_marker_ids",[]))!={1,2,3,4} or not isinstance(observation,dict)):
        return "front_markers_required",None
    metric=inspect.get("lidar_measurements",{}).get(role,{})
    lidar=observation.get("lidars",{}).get(role) or {}
    if metric.get("condition")!="board_observed" or metric.get("hole_count")!=4 or len(lidar.get("holes_m",[]))!=4:
        return "front_four_holes_required",None
    plane=observation.get("camera") or {}
    xyz=np.asarray(plane.get("all_marker_corners_m"),dtype=float)
    center=np.asarray(plane.get("center_m"),dtype=float)
    normal=np.asarray(plane.get("normal"),dtype=float)
    if (xyz.shape!=(16,3) or center.shape!=(3,) or normal.shape!=(3,) or
            not all(np.isfinite(p).all() for p in (xyz,center,normal)) or not np.all(xyz[:,2]>0)
            or not math.isclose(float(np.linalg.norm(normal)),1.,abs_tol=.01)):
        return "measured_board_geometry_required",None
    try:
        checks.extrinsic(profile,"rgbd",camera)
    except checks.Invalid:
        return "surveyed_camera_mount_required",None
    mount=profile["extrinsics"]["rgbd"]
    transform=np.asarray(rotation(mount["translation_m"],mount["rotation_xyzw"]))
    r,t=transform[:,:3],transform[:,3]
    target=r@center+t
    # Corner order is clockwise from upper left. The configured mounting distance refers to this base frame.
    centers=xyz.reshape(4,4,3).mean(axis=1)
    down=centers[2:].mean(axis=0)-centers[:2].mean(axis=0)
    if np.linalg.norm(down)<.01:
        return "measured_board_geometry_required",None
    down/=np.linalg.norm(down)
    height=profile["board_asset"]["outline_m"][1]
    bottom=(r@(center+down*height/2)+t)[2]
    facing=math.degrees(math.acos(float(np.clip((r@normal)[0],-1.,1.))))
    camera_stamp=int(inspect["camera_stamp_ns"])
    lidar_stamp=int(metric["stamp_ns"])
    sha=inspect.get("archive_sha256")
    if (not isinstance(sha,str) or len(sha)!=64 or camera_stamp<=0 or lidar_stamp<=0
            or abs(camera_stamp-lidar_stamp)>profile["max_skew_ns"]):
        return "paired_stamps_required",None
    if (not gate["forward_range_m"][0]<=target[0]<=gate["forward_range_m"][1] or
            abs(target[1])>gate["max_lateral_m"] or facing>gate["max_facing_deg"] or
            abs(bottom-gate["ground_z_base_m"])>gate["max_bottom_height_error_m"] or
            (r@down)[2]>-math.cos(math.radians(gate["max_facing_deg"]))):
        return "place_board_front_and_grounded",None
    return "hold_stationary",dict(camera_stamp_ns=camera_stamp,lidar_stamp_ns=lidar_stamp,
            archive_sha256=sha,camera=plane,lidar=lidar,base_frame=profile["base_frame"],center_base_m=target.tolist(),
            estimated_bottom_z_base_m=float(bottom),facing_error_deg=facing)


def same_placement(first, second, limits):
    return (first is not None and second is not None and first["archive_sha256"]!=second["archive_sha256"]
            and 0<second["camera_stamp_ns"]-first["camera_stamp_ns"]<=limits["max_gap_s"]*1e9
            and 0<second["lidar_stamp_ns"]-first["lidar_stamp_ns"]<=limits["max_gap_s"]*1e9
            and stationary(first["camera"],second["camera"],limits) and stationary(first["lidar"],second["lidar"],limits))
