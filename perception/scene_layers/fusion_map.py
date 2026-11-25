"""Freeze registered static survey windows in a common frame, retaining each measured return."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import tempfile
import time
from xml.etree import ElementTree

from perception.map3d.geometry import rotation

from .core import bounded_bytes, canonical, digest, is_ref, make_layer, reference, require
from .lidar_rgb import FusionArchive, _transform


def _station(root, name):
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name),
            "scene.invalid_station", "Expected a local survey station name")
    directory = root/name
    require(directory.resolve() == directory and directory.is_dir(), "scene.invalid_station", "Unknown survey station")
    return directory


class RegisteredMap(FusionArchive):
    """A map checkpoint. Observation timestamps from separate simulation runs are never subtracted."""
    def __init__(self, directory, manifest_ref, *, manifest_bytes=None):
        root = Path(directory).resolve()
        raw = bounded_bytes(root, "map.json", 65536) if manifest_bytes is None else manifest_bytes
        require(is_ref(manifest_ref) and digest(raw) == manifest_ref, "scene.manifest_mismatch", "Map manifest differs")
        manifest = json.loads(raw)
        entries = manifest.get("stations")
        require(manifest.get("schema_version") == 1 and isinstance(entries, list) and 1 <= len(entries) <= 8
                and all(isinstance(e, dict) and is_ref(e.get("capture_ref")) for e in entries),
                "scene.invalid_station", "Expected one to eight pinned survey windows")
        require(len({e["capture_ref"] for e in entries}) == len(entries), "scene.invalid_station", "Repeated capture")
        stamp = manifest.get("checkpoint_unix_ns")
        require(type(stamp) is int and 0 < stamp < 2**63, "scene.time_mismatch", "Missing map checkpoint time")
        world_ref, rig_ref = manifest.get("static_world_ref"), manifest.get("rig_ref")
        require(is_ref(world_ref) and is_ref(rig_ref), "scene.missing_registration", "Missing fixed world or rig identity")
        points, colors, contributors, windows = [], [], [], []
        sources, rejected, refs = {}, Counter(), {}
        for entry in entries:
            name = entry.get("id")
            folder = _station(root, name)
            require(name not in refs, "scene.invalid_station", "Repeated survey station")
            part = FusionArchive(folder, entry["capture_ref"])
            descriptor = part.describe("fusion")
            data = part.read("fusion", descriptor["layer_ref"])["data"]
            cal = bounded_bytes(folder, "calibration.json", 65536)
            require(digest(cal) == descriptor["calibration_ref"], "scene.artifact_mismatch", "Station calibration changed")
            pose = json.loads(cal)
            require(descriptor["frame_id"] == manifest["frame_id"] and pose.get("static_world_ref") == world_ref
                    and pose.get("rig_ref") == rig_ref and pose.get("rig_pose_source") == "gazebo_pose_info"
                    and data.get("calibration_source") == "gazebo_sdf", "scene.missing_registration",
                    "Station requires matching Gazebo world, frame, sensor rig and an evidenced pose")
            transform = _transform(pose.get("T_world_rig"))
            world_raw = bounded_bytes(folder, "world.sdf", 512*1024)
            require(digest(world_raw)[7:] == pose.get("world_file_sha256"),
                    "scene.artifact_mismatch", "World file differs from station calibration")
            world = ElementTree.fromstring(world_raw).find("world")
            rig = world.find("model[@name='rig']") if world is not None else None
            require(rig is not None and rig.findtext("static") in ("true", "1") and rig.find("pose") is not None,
                    "scene.missing_registration", "Station requires a fixed measurement rig")
            world.remove(rig)
            rig.remove(rig.find("pose"))
            require(digest(ElementTree.tostring(world)) == world_ref and digest(ElementTree.tostring(rig)) == rig_ref,
                    "scene.missing_registration", "Station geometry differs from the common static world or rig")
            witness_raw = bounded_bytes(folder, "rig-pose.json", 65536)
            require(digest(witness_raw) == entry.get("pose_ref"), "scene.artifact_mismatch", "Station pose witness changed")
            witness = json.loads(witness_raw)
            require(witness.get("topic") == f"/world/{world.get('name')}/pose/info" and witness["rig"]["name"] == "rig",
                    "scene.missing_registration", "Pose witness belongs to a different world or body")
            q, p = witness["rig"]["orientation"], witness["rig"]["position"]
            observed = rotation([p.get(k, 0.) for k in "xyz"], [q.get(k, 0.) for k in "xyzw"])
            require(all(abs(x-y) < 1e-6 for row, other in zip(observed, transform) for x,y in zip(row, other)),
                    "scene.missing_registration", "Registered rig pose differs from the simulator witness")
            start = len(points)
            require(start + len(data["points"]) <= 60000, "scene.capture_size", "Map exceeds the point budget")
            # Single-window projections have already applied the station transforms into this world.
            points.extend(data["points"])
            colors.extend(data["colors"])
            contributors.extend(data["contributors"])
            counts = data["stats"]
            for key, value in counts["sources"].items():
                sources.setdefault(key, Counter()).update(value)
            rejected.update(counts["rejections"])
            refs[name] = entry["capture_ref"]
            windows.append({"id": name, "start": start, "count": len(data["points"]), "colored": counts["colored"],
                            "frame_id": descriptor["frame_id"], "clock_id": data["clock_id"],
                            "acquisition": data["acquisition"], "capture_ref": entry["capture_ref"],
                            "calibration_ref": descriptor["calibration_ref"], "pose_ref": entry["pose_ref"],
                            "pose_source": pose["rig_pose_source"], "clock_epoch": name, "T_world_rig": pose["T_world_rig"]})
        colored = sum(w["colored"] for w in windows)
        stats = {"points": len(points), "colored": colored, "uncolored": len(points)-colored,
                 "sources": sources, "rejections": rejected, "station_count": len(windows)}
        source_refs = {"stations": refs, "static_world": world_ref, "rig": rig_ref, "manifest": manifest_ref}
        self.raw = canonical({"schema_version": 1, "frame_id": manifest["frame_id"], "stamp_ns": str(stamp),
                              "clock_id": "unix", "stamp_basis": "map_checkpoint", "points": points, "colors": colors,
                              "contributors": contributors, "windows": windows, "stats": stats, "source_refs": source_refs,
                              "calibration_source": "gazebo_sdf", "motion_authorized": False})
        require(len(self.raw) <= 5*1024*1024, "scene.capture_size", "Map exceeds the DSH reply budget")
        self.layer = make_layer({"schema_version": 1, "kind": "fusion", "representation": "registered_lidar_rgb_map",
                                 "space_ref": world_ref, "frame_id": manifest["frame_id"], "clock_id": "unix",
                                 "stamp_basis": "map_checkpoint", "stamp_ns": str(stamp), "acquisition": [str(stamp)]*2,
                                 "source_refs": source_refs, "calibration_ref": reference([w["calibration_ref"] for w in windows]),
                                 "content_ref": digest(self.raw), "stats": stats,
                                 "quality": {"registration": "gazebo_sdf", "coordinate_unit": "m", "temporal_scope": "static_survey",
                                             "outside_observations": "unknown", "resolution_m": None, "error_bound_m": None}}, manifest_ref)


def open_fusion(directory, manifest_ref):
    if (Path(directory)/"map.json").is_file():
        return RegisteredMap(directory, manifest_ref)
    return FusionArchive(directory, manifest_ref)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("stations", nargs="+")
    args = parser.parse_args()
    root = args.directory.resolve()
    entries = []
    for name in args.stations:
        directory = _station(root, name)
        entry = {"id": name, "capture_ref": digest(bounded_bytes(directory, "capture.json", 12*1024*1024))}
        entry["pose_ref"] = digest(bounded_bytes(directory, "rig-pose.json", 65536))
        entries.append(entry)
    cal = json.loads(bounded_bytes(_station(root, args.stations[0]), "calibration.json", 65536))
    path = root/"map.json"
    manifest = {"schema_version": 1, "stations": entries, "static_world_ref": cal["static_world_ref"],
                "rig_ref": cal["rig_ref"], "frame_id": cal["frame_id"]}
    if path.exists():
        held = json.loads(bounded_bytes(root, "map.json", 65536))
        require(manifest == {k:v for k,v in held.items() if k != "checkpoint_unix_ns"},
                "scene.manifest_mismatch", "Use a new output directory for a different survey")
    else:
        manifest["checkpoint_unix_ns"] = time.time_ns()
        candidate = canonical(manifest)
        RegisteredMap(root, digest(candidate), manifest_bytes=candidate)
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as tmp:
            tmp.write(candidate)
        try:
            os.link(tmp.name, path)
        finally:
            Path(tmp.name).unlink()
    ref = digest(bounded_bytes(root, "map.json", 65536))
    RegisteredMap(root, ref)
    print(ref)


if __name__ == "__main__":
    main()
