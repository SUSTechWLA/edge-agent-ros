"""Immutable read-only map layers and scene queries for SDD-057 archive providers."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import threading

from perception.rover_plan_field.rgb_scene import build_rgbd
from perception.rover_plan_field.scene import REF, build_scene
from task_recorder.global_map import read_bounded, read_map


class SceneError(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def require(condition, code, detail):
    if not condition:
        raise SceneError(code, detail)


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def reference(value):
    return digest(canonical(value))


def is_ref(value):
    return isinstance(value, str) and bool(REF.fullmatch(value))


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def bounded_bytes(root, name, limit):
    # File names come from the format adapter, never from a client's desired URL.
    path = root / name
    require(path.parent == root and path.resolve() == path and path.is_file(),
            "scene.artifact_missing", f"Missing archive artifact: {name}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    require(0 < len(raw) <= limit, "scene.artifact_size", f"Size out of bounds: {name}")
    return raw


def make_layer(metadata, archive_ref):
    # A new colour exposure elsewhere in the bundle must not change a LiDAR layer's identity.
    return {**metadata, "layer_ref": reference(metadata), "archive_ref": archive_ref}


class Archive:
    """Site-mounted two-sensor archive. Verify and memoize each representation independently."""
    def __init__(self, directory, manifest_ref=None):
        self.root = Path(directory).resolve()
        raw = bounded_bytes(self.root, "manifest.json", 65_536)
        self.archive_ref = digest(raw)
        require(manifest_ref is None or self.archive_ref == manifest_ref,
                "scene.manifest_mismatch", "Site-pinned archive manifest does not match")
        try:
            self.manifest = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise SceneError("scene.archive_invalid", "Unreadable Jetson archive manifest") from exc
        require(isinstance(self.manifest, dict) and self.manifest.get("schema_version") == 1
                and isinstance(self.manifest.get("files"), dict),
                "scene.archive_invalid", "Unsupported Jetson archive manifest")
        self._cache = {}
        self._lock = threading.Lock()

    def _blob(self, name, expected=None):
        recorded = self.manifest["files"].get(name)
        require(is_ref(recorded) and (expected is None or recorded == expected),
                "scene.artifact_mismatch", f"Reference is not bound to {name}")
        raw = bounded_bytes(self.root, name, 4_194_304)
        require(digest(raw) == recorded, "scene.artifact_mismatch", f"Digest mismatch: {name}")
        return raw

    def _occupancy(self):
        snapshot_ref = self.manifest["lidar_snapshot_ref"]
        raw = self._blob("occupancy-scene.json")
        saved = json.loads(raw)
        snapshot = json.loads(self._blob("lidar-snapshot.json", snapshot_ref))
        require(snapshot["scan_stamp_ns"] == self.manifest["lidar_stamp_ns"],
                "scene.time_mismatch", "LiDAR acquisition differs from its manifest")
        sources = snapshot["source_evidence"]["lidars"]
        clouds = {}
        refs = {"snapshot": snapshot_ref,
                "configuration": self.manifest["files"]["lidar-calibration.json"],
                "profile": self.manifest["files"]["chassis-profile.json"]}
        for source in sources:
            name = source["source_id"]
            require(name in ("mid360.front", "mid360.rear") and name not in refs,
                    "scene.source_mismatch", "Unexpected or repeated MID360 source")
            ref = source["artifact_ref"]
            clouds[ref] = self._blob(name + ".json", ref)
            refs[name] = ref
        require(len(clouds) == 2, "scene.source_mismatch", "Dual MID360 bundle requires both sources")
        calibration = json.loads(self._blob("lidar-calibration.json"))
        footprint = json.loads(self._blob("chassis-profile.json"))
        rebuilt = build_scene(snapshot, calibration, clouds.__getitem__, footprint=footprint, snapshot_ref=snapshot_ref)
        require(rebuilt == saved, "scene.derived_mismatch", "Recorded occupancy differs from the bound scans")
        stamps = [int(row["stamp_ns"]) for row in sources]
        desc = {"schema_version": 1, "kind": "occupancy", "representation": "body_occupancy_voxels",
                "frame_id": saved["frame_id"], "stamp_ns": saved["stamp_ns"], "clock_id": "unix",
                "acquisition": [str(min(stamps)), str(max(stamps))], "stamp_basis": "host_packet_receive",
                "space_ref": reference(["local_body_capture", snapshot_ref]),
                "calibration_ref": snapshot["calibration_ref"], "content_ref": digest(raw), "source_refs": refs,
                "quality": {"resolution_m": saved["voxel_size_m"], "error_bound_m": None,
                            "bounds_m": saved["bounds_m"], "outside_observations": "unknown",
                            "registration": "nominal_body_extrinsics", "coordinate_unit": "m"},
                "stats": {"voxel_count": len(saved["voxels"]), "observed_ground_count": len(saved["observed_ground"]),
                          "occupied_ground_count": len(saved["occupied_ground"])}}
        return make_layer(desc, self.archive_ref), raw

    def _texture(self):
        surface_ref = self.manifest["rgbd_scene_ref"]
        raw = self._blob("rgbd-scene.json", surface_ref)
        saved = json.loads(raw)
        evidence = saved["source_evidence"]
        stamp = saved["stamp_ns"]
        frame = saved["frame_id"]
        refs = {key: evidence[key]["artifact_ref"] for key in ("color", "depth", "calibration")}
        require(evidence["color"]["stamp_ns"] == evidence["depth"]["stamp_ns"] == stamp
                and self.manifest["rgbd_stamp_ns"] == stamp and evidence["clock_id"] == "unix",
                "scene.time_mismatch", "RGB and registered depth must share one acquisition time")
        require(all(evidence[name]["frame_id"] == frame for name in refs),
                "scene.frame_mismatch", "Colour, depth and intrinsics refer to different frames")
        rgb = self._blob("rgbd-color.png", refs["color"])
        depth = self._blob("rgbd-depth.png", refs["depth"])
        info = json.loads(self._blob("rgbd-calibration.json", refs["calibration"]))
        rebuilt = build_rgbd(rgb, depth, info, frame, stride=saved["stride_px"])
        require(all(saved.get(k) == value for k, value in rebuilt.items())
                and saved["texture_url"] == "/camera/" + refs["color"][7:] + ".png",
                "scene.derived_mismatch", "Surface geometry or texture differs from the bound RGB-D exposure")
        # The archived XYZ values were rounded to 0.1 mm. The integer sampling lattice
        # preserves the exact source texel even where reprojecting that XYZ would not.
        width, height, stride = rebuilt["image_width"], rebuilt["image_height"], rebuilt["stride_px"]
        uv = []
        for index, vertex in enumerate(rebuilt["vertices"]):
            row, col = divmod(index, rebuilt["grid_width"])
            uv.append(None if vertex is None else [(col * stride + .5) / width, (row * stride + .5) / height])
        saved["texture_mapping"] = {"schema_version": 1, "image_ref": refs["color"], "origin": "top_left",
                                    "coordinates": "normalized_pixel_centers", "uv": uv}
        mapped = canonical(saved)
        refs["surface"] = surface_ref
        points = [v for v in saved["vertices"] if v is not None]
        bounds = {axis: [min(v[i] for v in points), max(v[i] for v in points)] for i, axis in enumerate("xyz")}
        desc = {"schema_version": 1, "kind": "texture", "representation": "rgbd_textured_surface",
                "frame_id": frame, "stamp_ns": stamp, "clock_id": evidence["clock_id"],
                "acquisition": [stamp, stamp], "stamp_basis": "registered_rgbd_exposure",
                "space_ref": reference(["local_optical_capture", refs["calibration"], stamp, evidence["actor_ref"]]),
                "calibration_ref": refs["calibration"], "content_ref": digest(mapped), "source_refs": refs,
                "quality": {"resolution_m": None, "stride_px": saved["stride_px"], "error_bound_m": None,
                            "bounds_m": bounds, "outside_observations": "unknown", "registration": "camera_only",
                            "coordinate_unit": "m"},
                "stats": {"width": saved["image_width"], "height": saved["image_height"],
                          "valid_vertices": len(points), "triangles": len(saved["triangles"])}}
        return make_layer(desc, self.archive_ref), mapped

    def _layer(self, kind):
        builders = {"occupancy": self._occupancy, "texture": self._texture}
        require(isinstance(kind, str) and kind in builders, "scene.unsupported_layer", "Unknown archive layer")
        with self._lock:
            if kind not in self._cache:
                try:
                    self._cache[kind] = builders[kind]()
                except SceneError:
                    raise
                except (KeyError, OSError, ValueError, OverflowError, TypeError, IndexError) as exc:
                    raise SceneError("scene.archive_invalid", f"Invalid {kind} archive: {exc}") from exc
            return self._cache[kind]

    def describe(self, kind):
        return deepcopy(self._layer(kind)[0])

    def read(self, kind, layer_ref):
        layer, raw = self._layer(kind)
        require(layer_ref == layer["layer_ref"], "scene.layer_mismatch", "Layer revision is not installed")
        return {"layer": deepcopy(layer), "data": json.loads(raw)}

    def texture_png(self, layer_ref):
        layer = self.describe("texture")
        require(layer["layer_ref"] == layer_ref, "scene.layer_mismatch", "Texture revision is not installed")
        return self._blob("rgbd-color.png", layer["source_refs"]["color"])


class GridArchive:
    """Optional, separately mounted Nav2 map. Its original v1 manifest and cells remain unchanged."""
    def __init__(self, directory, manifest_ref):
        require(is_ref(manifest_ref), "scene.manifest_mismatch", "A content-addressed Nav2 manifest is required")
        root = Path(directory).resolve(strict=True)
        record = read_map(root, manifest_sha256=manifest_ref)
        data, meta = record["grid"], record["metadata"]
        manifest_raw = read_bounded(root, "manifest.json")
        require(digest(manifest_raw) == manifest_ref, "scene.manifest_mismatch", "2D map changed while loading")
        self.raw = read_bounded(root, "occupancy.json")
        require(digest(self.raw) == "sha256:" + json.loads(manifest_raw)["sha256"]["occupancy.json"],
                "scene.artifact_mismatch", "2D occupancy data changed while loading")
        stamp = str(meta["stamp_ns"])
        desc = {"schema_version": 1, "kind": "grid2d", "representation": "nav2_occupancy_grid",
                "frame_id": data["frame"], "stamp_ns": stamp,
                "clock_id": meta.get("map_identity", {}).get("clock_domain", meta.get("clock_domain")),
                "acquisition": [stamp, stamp], "stamp_basis": "map_checkpoint", "space_ref": manifest_ref,
                "calibration_ref": meta["calibration_id"], "content_ref": digest(self.raw),
                "source_refs": {"map_manifest": manifest_ref}, "stats": {"width": data["width"],
                "height": data["height"], "known_cells": record["known_cells"], "total_cells": len(data["cells"])},
                "quality": {"resolution_m": data["resolution"], "error_bound_m": None,
                            "outside_observations": "unknown", "registration": "stored_grid_frame",
                            "coordinate_unit": "m", "origin": data["origin"]}}
        self.layer = make_layer(desc, manifest_ref)

    def describe(self, kind):
        require(kind == "grid2d", "scene.unsupported_layer", "Only the bound 2D occupancy grid is available")
        return deepcopy(self.layer)

    def read(self, kind, layer_ref):
        layer = self.describe(kind)
        require(layer["layer_ref"] == layer_ref, "scene.layer_mismatch", "2D map revision differs")
        return {"layer": layer, "data": json.loads(self.raw)}


def validated_descriptor(layer):
    require(isinstance(layer, dict) and is_ref(layer.get("layer_ref")),
            "scene.layer_mismatch", "Missing layer revision")
    data = {k: v for k, v in layer.items() if k not in ("layer_ref", "archive_ref", "provider")}
    require(reference(data) == layer["layer_ref"], "scene.layer_mismatch", "Layer descriptor digest differs")
    require(type(data.get("schema_version")) is int and data["schema_version"] == 1
            and all(isinstance(data.get(k), str) and data[k]
            for k in ("kind", "frame_id", "stamp_ns")) and is_ref(data.get("content_ref"))
            and is_ref(data.get("space_ref")) and isinstance(data.get("quality"), dict),
            "scene.invalid_descriptor", "Invalid layer identity, frame or acquisition")
    acquisition = data.get("acquisition")
    require(isinstance(acquisition, list) and len(acquisition) == 2 and all(
            isinstance(t, str) and 0 < len(t) <= 19 and t.isascii() and t.isdecimal()
            for t in [*acquisition, data["stamp_ns"]])
            and 0 < int(acquisition[0]) <= int(data["stamp_ns"]) <= int(acquisition[1]) < 2**63,
            "scene.invalid_descriptor", "Invalid observation time interval")
    # Source archive is an audit locator; versioned content and required inputs are already in data.
    return deepcopy({k: v for k, v in layer.items() if k != "archive_ref"})


def freeze_scene(layers, *, frame_id=None, max_skew_ms=250, max_resolution_m=None, roi_m=None,
                 max_error_m=None):
    """Freeze compatible provider results without ever transforming or synthesizing geometry."""
    require(isinstance(layers, list) and 0 < len(layers) <= 16, "scene.invalid_query", "Expected 1 to 16 layers")
    require(frame_id is None or isinstance(frame_id, str) and 0 < len(frame_id) < 256,
            "scene.invalid_query", "Invalid target frame")
    require(number(max_skew_ms) and max_skew_ms >= 0, "scene.invalid_query", "Invalid time tolerance")
    for limit in (max_resolution_m, max_error_m):
        require(limit is None or number(limit) and limit > 0, "scene.invalid_query", "Invalid metric requirement")
    if roi_m is not None:
        require(isinstance(roi_m, dict) and set(roi_m) == set("xyz") and all(
            isinstance(span, list) and len(span) == 2 and all(number(n) for n in span) and span[0] <= span[1]
            for span in roi_m.values()), "scene.invalid_query", "ROI must specify x, y, z intervals in metres")
    selected = sorted((validated_descriptor(l) for l in layers), key=lambda l: l["kind"])
    require(len({l["kind"] for l in selected}) == len(selected), "scene.invalid_query", "Duplicate layer kind")
    frame = frame_id or selected[0]["frame_id"]
    reasons = []
    stamps = [int(l["stamp_ns"]) for l in selected]
    clocks = {l["clock_id"] for l in selected}
    time_span_ms = (max(stamps)-min(stamps))/1e6 if len(clocks) == 1 and None not in clocks else None
    acquisition_span_ms = None
    if time_span_ms is not None or len(selected) == 1:
        starts = (int(l["acquisition"][0]) for l in selected)
        ends = (int(l["acquisition"][1]) for l in selected)
        acquisition_span_ms = (max(ends)-min(starts))/1e6
    if len({l["space_ref"] for l in selected}) != 1 or any(l["frame_id"] != frame for l in selected):
        reasons.append({"code": "scene.missing_registration", "requested_frame": frame,
                        "available_frames": sorted({l["frame_id"] for l in selected})})
    if acquisition_span_ms is None or acquisition_span_ms > max_skew_ms:
        reasons.append({"code": "scene.time_skew" if acquisition_span_ms is not None else "scene.clock_mismatch",
                        "span_ms": acquisition_span_ms, "allowed_ms": max_skew_ms})
    for layer in selected:
        quality = layer["quality"]
        for key, maximum, code in (("resolution_m", max_resolution_m, "scene.resolution_unavailable"),
                                   ("error_bound_m", max_error_m, "scene.accuracy_unverified")):
            if maximum is not None and (not number(quality.get(key)) or quality[key] > maximum):
                reasons.append({"code": code, "kind": layer["kind"], "required_m": maximum,
                                "available_m": quality.get(key)})
        if roi_m is not None:
            bounds = quality.get("bounds_m")
            outside = bounds and any(roi_m[k][0] < bounds[k][0] or roi_m[k][1] > bounds[k][1] for k in roi_m)
            # The extent of a surface or a planar grid cannot certify that a 3D volume has been observed.
            reasons.append({"code": "scene.outside_coverage" if outside else "scene.coverage_unverified",
                            "kind": layer["kind"], "roi_m": roi_m})
    if reasons:
        return {"status": "refused", "scene_ref": None, "independent_layers": selected,
                "reasons": reasons, "time_span_ms": time_span_ms, "acquisition_span_ms": acquisition_span_ms,
                "motion_authorized": False}
    scene = {"schema_version": 1, "frame_id": frame, "layers": selected}
    return {"status": "ready", "scene_ref": reference(scene), "scene": scene,
            "time_span_ms": time_span_ms, "acquisition_span_ms": acquisition_span_ms, "motion_authorized": False}


def check_read(scene_ref, scene, kind, layer_ref):
    require(is_ref(scene_ref) and isinstance(scene, dict) and reference(scene) == scene_ref,
            "scene.snapshot_mismatch", "Scene reference differs from its immutable manifest")
    for layer in scene.get("layers", []):
        if layer.get("kind") == kind and layer.get("layer_ref") == layer_ref:
            return validated_descriptor(layer)
    raise SceneError("scene.layer_mismatch", "Requested layer does not belong to this scene")
