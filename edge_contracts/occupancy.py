"""Publish one immutable occupancy map and its provenance at the end of a bounded operation."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class Grid:
    """The same ROS row-major layout and origin convention as the map viewer."""

    def __init__(self, grid):
        self.width, self.height = grid["width"], grid["height"]
        self.resolution, self.cells = grid["resolution"], grid["cells"]
        self.frame = grid["frame"]
        if (type(self.width) is not int or type(self.height) is not int
                or not 1 <= self.width <= 4096 or not 1 <= self.height <= 4096
                or self.width*self.height > 262144 or not finite(self.resolution)
                or not 0.001 <= self.resolution <= 1000 or not isinstance(self.frame, str)
                or not self.frame.strip() or len(self.frame) > 200):
            raise ValueError("invalid_grid_geometry")
        self.ox, self.oy, self.yaw = grid["origin"]
        if not all(finite(v) for v in (self.ox, self.oy, self.yaw)):
            raise ValueError("invalid_grid_origin")
        height_offset = grid.get("origin_z", 0)
        if not finite(height_offset) or abs(height_offset) > 1e-6:
            raise ValueError("nonplanar_grid_origin")
        self.c, self.s = math.cos(self.yaw), math.sin(self.yaw)
        if (len(self.cells) != self.width*self.height
                or any(type(v) is not int or not -1 <= v <= 100 for v in self.cells)):
            raise ValueError("invalid_grid_values")

    def local(self, point):
        dx, dy = point[0]-self.ox, point[1]-self.oy
        return ((self.c*dx+self.s*dy)/self.resolution, (-self.s*dx+self.c*dy)/self.resolution)

    def index(self, point):
        u, v = self.local(point)
        x, y = math.floor(u), math.floor(v)
        return y*self.width+x if 0 <= x < self.width and 0 <= y < self.height else -1

    def center(self, index):
        x = (index % self.width+.5)*self.resolution
        y = (index // self.width+.5)*self.resolution
        return [self.ox+self.c*x-self.s*y, self.oy+self.s*x+self.c*y]

    def neighbors(self, i):
        x = i % self.width
        if x+1 < self.width:
            yield i+1
        if i+self.width < len(self.cells):
            yield i+self.width
        if x:
            yield i-1
        if i >= self.width:
            yield i-self.width


def _json(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":"))+"\n").encode("utf-8")


def _sync_directory(directory):
    if os.name == "posix":
        fd = os.open(directory, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def save_map(destination, grid, *, metadata):
    """Write Nav2 trinary PGM/YAML, original cells and a manifest with SHA256.

    Replaying an operation with the identical payload returns its verified manifest.
    A destination conflict or a damaged original raises ValueError. No file is replaced.
    """
    g = Grid(grid)
    for key in ("robot_id","calibration_id","operation_id","state","reason"):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ValueError("missing_map_metadata:"+key)
    if type(metadata.get("stamp_ns")) is not int or metadata["stamp_ns"] <= 0:
        raise ValueError("invalid_map_stamp")
    data = {"frame":g.frame, "origin":[g.ox,g.oy,g.yaw], "width":g.width, "height":g.height,
            "resolution":g.resolution, "cells":list(g.cells)}
    # PGM row zero is the top row. ROS cell zero is in the bottom row.
    pixels = bytearray()
    for y in reversed(range(g.height)):
        pixels.extend(205 if v<0 else (254 if v==0 else 0)
                      for v in g.cells[y*g.width:(y+1)*g.width])
    files = {"occupancy.json":_json(data),
             "map.pgm":f"P5\n{g.width} {g.height}\n255\n".encode("ascii")+pixels,
             "map.yaml":("image: map.pgm\nmode: trinary\n"
                         f"resolution: {g.resolution}\norigin: {json.dumps(data['origin'])}\n"
                         "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n").encode("ascii")}
    manifest = {"schema_version":1, "frame":g.frame, "origin":data["origin"],
                "resolution":g.resolution, "width":g.width, "height":g.height,
                "known_cells":sum(v>=0 for v in g.cells), "total_cells":len(g.cells),
                "metadata":metadata, "sha256":{k:hashlib.sha256(v).hexdigest() for k,v in files.items()}}
    files["manifest.json"] = _json(manifest)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    def verify_existing():
        for name, expected in files.items():
            path = destination/name
            if not path.is_file() or path.read_bytes() != expected:
                raise ValueError("map_artifact_conflict")
        return manifest

    if destination.exists():
        return verify_existing()
    with tempfile.TemporaryDirectory(prefix=".map-stage-", dir=destination.parent) as tmp:
        # Keep the TemporaryDirectory itself in place for portable cleanup on Windows.
        stage = Path(tmp)/"map"
        stage.mkdir()
        for name, value in files.items():
            with (stage/name).open("wb") as out:
                out.write(value)
                out.flush()
                os.fsync(out.fileno())
        _sync_directory(stage)
        try:
            stage.rename(destination)
        except OSError:
            if not destination.is_dir():
                raise
            return verify_existing()
        _sync_directory(destination.parent)
    return manifest
