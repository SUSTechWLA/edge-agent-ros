"""Verify accepted capture positions and image regions from raw Gazebo and RGB archives."""
import argparse
import gzip
import json
import math
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from calibration.circle_target import marker_corners
from calibration.view_coverage import image_cells


def audit(run, lateral_limit):
    summary = json.loads((run/"summary.json").read_bytes())
    with gzip.open(run/"motion/trajectory.jsonl.gz","rt",encoding="utf-8") as stream:
        world = [row for line in stream if (row := json.loads(line))[1]=="world"]
    if not world:
        raise ValueError("no independent Gazebo pose stream")
    placement = json.loads((run/"operator-placement.json").read_bytes())["world_pose"]["board"]
    _,board_y,_ = placement["position_m"]
    if placement["rotation_xyzw"] != [0,0,0,1]:
        raise ValueError("expected the fixed board parallel to the Gazebo Y axis")
    world_model = ET.parse(run/"fixture-truth/world.sdf")
    mount = world_model.findtext(".//model[@name='lingao']/link/sensor[@name='d435_color']/pose")
    mount_x,mount_y = map(float,mount.split()[:2])
    cells, poses = set(), []
    for accepted in summary["capture"]["captures"]:
        manifest = json.loads((run/"captures"/(accepted["manifest"]+".json")).read_bytes())
        selected = str(accepted["observation_id"])
        stamp = int(selected)
        sample = min(world,key=lambda row:abs(row[2]-stamp))
        skew_ms = abs(sample[2]-stamp)/1e6
        if skew_ms > 100:
            raise ValueError("no independent pose at accepted observation")
        _, y, yaw = sample[3]
        lateral = y - board_y
        eye = lateral + mount_x*math.sin(yaw) + mount_y*math.cos(yaw)
        poses.append(dict(stamp_ns=selected,x_y_yaw=sample[3],base_lateral_m=lateral,optical_lateral_m=eye,
                          skew_ms=skew_ms))
        for frame in manifest["frames"]:
            with np.load(run/"captures"/(frame["sha256"]+".npz"),allow_pickle=False) as archive:
                rgb = archive["rgb"]
                corners = marker_corners(rgb)
                if len(corners) != 4:
                    raise ValueError("accepted RGB frame does not contain all four markers")
                cells |= image_cells(np.concatenate(list(corners.values())),rgb.shape[1::-1])
    left = max(r["optical_lateral_m"] for r in poses)
    right = min(r["optical_lateral_m"] for r in poses)
    base_left = max(r["base_lateral_m"] for r in poses)
    base_right = min(r["base_lateral_m"] for r in poses)
    return dict(task_id=summary["task_id"],optical_left_m=left,optical_right_m=-right,
                optical_span_m=left-right,base_left_m=base_left,base_right_m=-base_right,
                base_span_m=base_left-base_right,rgb_cells=sorted(cells),poses=poses,
                cross_track_pass=left>=lateral_limit and right<=-lateral_limit and base_left>0>base_right,
                yaw_span_deg=math.degrees(max(r["x_y_yaw"][2] for r in poses)-min(r["x_y_yaw"][2] for r in poses)))


if __name__ == "__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("run",type=Path)
    cli.add_argument("--lateral-limit",type=float,default=.14)
    args=cli.parse_args()
    print(json.dumps(audit(args.run,args.lateral_limit),indent=2,allow_nan=False))
