"""Independently score an RGB-LiDAR fusion result against the static Gazebo world.

Labels come from ray/box intersections and the SDF materials, independent of the
camera image, measured depth and projection code. No label enters the provider.
"""
from array import array
from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
from xml.etree import ElementTree as ET

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from perception.scene_layers.core import SceneError
from perception.scene_layers.lidar_rgb import FusionArchive, fuse_clouds


def floats(elem, key):
    return np.fromstring(elem.findtext(key), sep=" ")


def surfaces(world):
    boxes = {}
    for model in world.findall("model"):
        shape = model.find("link/visual")
        if shape is None:
            continue
        pose = floats(model, "pose")
        assert not np.any(pose[3:]), "Score supports only axis-aligned witness boxes"
        half = floats(shape, "geometry/box/size")/2
        colour = floats(shape, "material/diffuse")[:3]
        boxes[model.get("name")] = (pose[:3]-half, pose[:3]+half, colour)
    assert len(boxes) >= 4
    return boxes


def check_returns(inputs, data):
    """Use the original finite sensor returns as the population for every score."""
    expected = {(sensor, idx) for sensor, cloud in inputs.items()
                for idx in np.flatnonzero(np.isfinite(cloud).all(axis=1))}
    if not len(data["points"]) == len(data["colors"]) == len(data["contributors"]) == len(expected):
        raise ValueError("Fused XYZ, RGB and contributor counts must match all finite laser returns")
    observed = set()
    for row in data["contributors"]:
        if (not isinstance(row, (list, tuple)) or len(row) != 2 or
                not isinstance(row[0], str) or type(row[1]) is not int):
            raise ValueError("Invalid laser return identity")
        key = tuple(row)
        if key not in expected or key in observed:
            raise ValueError("Repeated or unknown laser return")
        observed.add(key)


def score(capture, data, world):
    boxes = surfaces(world)
    rig = floats(world.find("model[@name='rig']"), "pose")
    assert rig[3]==rig[4]==0
    co,si=math.cos(rig[5]),math.sin(rig[5])
    orient=np.array([[co,-si,0],[si,co,0],[0,0,1]])
    sensors = world.find("model[@name='rig']/link[@name='mount']")
    camera = sensors.find("sensor[@name='rgbd']")
    eye = rig[:3] + orient @ floats(camera, "pose")[:3]
    fov = float(camera.findtext("camera/horizontal_fov"))
    width, height = [int(camera.findtext("camera/image/"+k)) for k in ("width", "height")]
    inputs = {s["source_id"]: np.array(s["points"]) for s in capture["lidars"]}
    expected_sources = {s.get("name") for s in sensors.findall("sensor") if s.get("type") in ("lidar", "gpu_lidar")}
    if set(inputs) != expected_sources or len(inputs) != len(capture["lidars"]):
        raise ValueError("Laser sources in capture differ from the measurement rig")
    check_returns(inputs, data)
    xyz = []
    source_indices = data["contributors"]
    for sensor_id, index in source_indices:
        # Use the simulation mount definition rather than the camera or lidar matrices under test.
        x, y, z = inputs[sensor_id][index]
        pose = floats(sensors.find(f"sensor[@name='{sensor_id}']"), "pose")
        c, s = math.cos(pose[5]), math.sin(pose[5])
        xyz.append(rig[:3] + orient @ (pose[:3] + (x*c-y*s, x*s+y*c, z)))
    xyz = np.array(xyz)
    ray = xyz - eye
    length = np.linalg.norm(ray, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        inv_ray = 1. / ray
    names = list(boxes)
    surface_distance = []
    interior = []
    occluded = np.zeros(len(xyz), dtype=bool)
    solidly_hidden = np.zeros(len(xyz), dtype=bool)
    for low, high, _ in boxes.values():
        center, half = (low + high)/2, (high-low)/2
        q = np.abs(xyz - center) - half
        signed = np.linalg.norm(np.maximum(q, 0), axis=1) + np.minimum(q.max(axis=1), 0)
        surface_distance.append(np.abs(signed))
        # Exclude corners and seams with more than one face closer than 35 mm.
        interior.append(np.sort(q, axis=1)[:, -2] < -.035)
        t0, t1 = (low-eye)*inv_ray, (high-eye)*inv_ray
        entry = np.maximum(np.minimum(t0, t1).max(axis=1), 0)
        exit_ = np.maximum(t0, t1).min(axis=1)
        occluded |= (entry <= exit_) & ((1-entry)*length > .05)
        # A ray grazing an occluder's silhouette can land on either side of
        # the boundary after finite rasterization. Count definite occlusions
        # only when the ray penetrates a 1.5 cm inset box before the target.
        t0, t1 = (low+.015-eye)*inv_ray, (high-.015-eye)*inv_ray
        entry = np.maximum(np.minimum(t0, t1).max(axis=1), 0)
        exit_ = np.maximum(t0, t1).min(axis=1)
        solidly_hidden |= (entry <= exit_) & ((1-entry)*length > .05)
    dist = np.array(surface_distance)
    label = np.argmin(dist, axis=0)
    index = np.arange(len(xyz))
    closely_known = (dist[label, index] < .025) & np.array(interior)[label, index]
    rig_ray = ray @ orient
    in_fov = (rig_ray[:, 0] > 0) & (np.abs(rig_ray[:, 1]) < rig_ray[:, 0]*math.tan(fov/2)*.985)
    in_fov &= np.abs(rig_ray[:, 2]) < rig_ray[:, 0]*math.tan(fov/2)*height/width*.985
    colored = np.array([c is not None for c in data["colors"]])
    rgb = np.array([c if c is not None else [0, 0, 0] for c in data["colors"]], dtype=float)
    # Compare hue class (channel ratios), rather than assuming that lighting
    # and sRGB transfer reproduce the SDF linear albedo byte-for-byte.
    # Pure red/green/blue, cyan/yellow/magenta and the neutral floor are distinguishable.
    palette = np.array([color for _, _, color in boxes.values()])
    palette /= np.linalg.norm(palette, axis=1)[:, None]
    predicted = np.argmax(rgb @ palette.T, axis=1)
    known = closely_known & in_fov & ~occluded
    scored = known & colored
    hidden = closely_known & in_fov & solidly_hidden
    geom = np.linalg.norm(np.array(data["points"])-xyz, axis=1)
    return {"scored_visible_colored": int(scored.sum()), "eligible_visible": int(known.sum()),
            "correct_material": int((scored & (label == predicted)).sum()),
            "material_accuracy": float((label[scored] == predicted[scored]).mean()),
            "visible_interior_color_coverage": float(scored.sum()/known.sum()),
            "eligible_occluded": int(hidden.sum()), "colored_occluded": int((hidden & colored).sum()),
            "silhouette_occlusions_excluded": int((closely_known & in_fov & occluded & ~solidly_hidden).sum()),
            "map_coordinate_max_error_m": float(geom.max()),
            "surface_distance_p95_m": float(np.percentile(dist.min(axis=0),95)),
            "surface_distance_p99_m": float(np.percentile(dist.min(axis=0),99)),
            "by_material": {name: {"scored": int((scored & (label == k)).sum()),
                "correct": int((scored & (label == k) & (label == predicted)).sum())} for k, name in enumerate(names)}}


def main(folder, capture_ref, out):
    world_path = folder/"world.sdf"
    world = ET.parse(world_path).find("world")
    bundle = FusionArchive(folder, capture_ref)
    layer = bundle.describe("fusion")
    data = bundle.read("fusion", layer["layer_ref"])["data"]
    capture = json.loads((folder/"capture.json").read_bytes())
    calib = json.loads((folder/"calibration.json").read_bytes())
    assert calib["world_file_sha256"] == sha256(world_path.read_bytes()).hexdigest()
    rgb = (folder/"rgb.raw").read_bytes()
    depth = array("f")
    depth.frombytes((folder/"depth.f32").read_bytes())
    if sys.byteorder != "little":
        depth.byteswap()
    shifted = deepcopy(capture)
    shifted["camera"]["T_map_camera"][1][3] += .45
    faults = {}
    def refusal(key, source):
        try:
            fuse_clouds(source, calib, rgb, depth)
        except SceneError as exc:
            faults[key] = exc.code
        else:
            raise ValueError(f"{key} was accepted")
    refusal("undeclared_extrinsic_change", shifted)
    late = deepcopy(capture)
    late["lidars"][1]["stamp_ns"] += 200_000_000
    refusal("200ms_lidar_skew", late)
    bad_calib = deepcopy(calib)
    bad_calib["camera"]["T_map_camera"][1][3] += .45
    shifted_data = fuse_clouds(shifted, bad_calib, rgb, depth)
    baseline = score(capture, data, world)
    negative = score(capture, shifted_data, world)
    small_shift = deepcopy(capture)
    small_calib = deepcopy(calib)
    # A deliberate, trusted but inaccurate camera mount, pivoted about its own centre.
    t = np.array(small_shift["camera"]["T_map_camera"])
    angle = math.radians(2)
    c, s = math.cos(angle), math.sin(angle)
    t[:3,:3] = np.array([[c,-s,0],[s,c,0],[0,0,1]]) @ t[:3,:3]
    t[1,3] += .03
    small_calib["camera"]["T_map_camera"] = small_shift["camera"]["T_map_camera"] = t.tolist()
    small_error = score(capture, fuse_clouds(small_shift, small_calib, rgb, depth), world)
    inputs = {cloud["source_id"]: np.array(cloud["points"]) for cloud in capture["lidars"]}
    dropped = {key: data[key][:-1] for key in ("points", "colors", "contributors")}
    replaced = {key: [*data[key][:-1], data[key][0]] for key in dropped}
    provenance_negatives = {}
    for name, damaged in (("dropped_return", dropped), ("last_replaced_by_first", replaced)):
        try:
            check_returns(inputs, damaged)
        except ValueError as exc:
            provenance_negatives[name] = str(exc)
        else:
            raise ValueError(f"Unaccounted laser input accepted for {name}")
    accepted = (baseline["eligible_visible"] >= 4000 and baseline["eligible_occluded"] >= 100 and
                baseline["material_accuracy"] >= .98 and baseline["visible_interior_color_coverage"] >= .8 and
                baseline["colored_occluded"] <= .01*baseline["eligible_occluded"] and
                baseline["map_coordinate_max_error_m"] < .0001 and baseline["surface_distance_p95_m"] < .025 and
                baseline["material_accuracy"]-negative["material_accuracy"] >= .05 and
                faults == {"undeclared_extrinsic_change":"scene.missing_registration","200ms_lidar_skew":"scene.time_skew"})
    report = {"passed": bool(accepted), "source": "headless_gazebo_harmonic_ros2_jazzy", "capture_ref": capture_ref,
              "world_ref": "sha256:" + calib["world_file_sha256"], "static_world_ref": calib["static_world_ref"],
              "rig_ref": calib["rig_ref"], "T_world_rig": calib["T_world_rig"], "rig_pose_source": calib["rig_pose_source"],
              "provider_statistics": data["stats"],
              "finite_returns_by_source": {k: int(np.isfinite(points).all(axis=1).sum()) for k, points in inputs.items()},
              "rejected_provenance_negative_controls": provenance_negatives,
              "correct_calibration": baseline, "wrong_camera_y_plus_45cm": negative, "admission": faults,
              "wrong_camera_y_plus_3cm_yaw_plus_2deg": small_error,
              "thresholds": {"min_material_accuracy": .98, "min_interior_coverage": .8,
                             "max_occluded_leak_fraction": .01,"max_surface_p95_m": .025,
                             "min_accuracy_drop_in_negative_control": .05}}
    out.write_text(json.dumps(report, allow_nan=False, indent=2) + "\n")
    print(json.dumps({key: value for key,value in report.items() if key not in ("provider_statistics","thresholds")}))
    if not accepted:
        raise SystemExit(1)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
