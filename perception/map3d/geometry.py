"""Bounded XYZ decoding and a short-lived surface map in one metric frame."""
from collections import OrderedDict
import math
import struct

MAX_BYTES = 32 * 1024 * 1024
SAMPLE_LIMIT = 4096
MAX_VOXELS = 8192
VOXEL_M = 0.1


def rotation(translation, quaternion):
    if not all(math.isfinite(v) for v in (*translation, *quaternion)):
        raise ValueError("transform_invalid")
    x, y, z, w = quaternion
    norm = math.sqrt(x*x + y*y + z*z + w*w)
    if abs(norm - 1) > .01:
        raise ValueError("transform_invalid")
    x, y, z, w = (v/norm for v in quaternion)
    return ((1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w), translation[0]),
            (2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w), translation[1]),
            (2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y), translation[2]))


IDENTITY = rotation((0, 0, 0), (0, 0, 0, 1))


def decode(message, matrix):
    width, height = message.width, message.height
    point_step, row_step = message.point_step, message.row_step
    raw = memoryview(message.data)
    if (point_step <= 0 or row_step < width * point_step or
            raw.nbytes != row_step * height or raw.nbytes > MAX_BYTES):
        raise ValueError("cloud_layout_invalid")
    axes = {}
    order = ">" if message.is_bigendian else "<"
    for field in message.fields:
        if field.name not in ("x", "y", "z"):
            continue
        fmt = {7:"f", 8:"d"}.get(field.datatype)
        if fmt is None or field.count != 1 or field.name in axes:
            raise ValueError("cloud_xyz_invalid")
        reader = struct.Struct(order + fmt)
        if field.offset < 0 or field.offset + reader.size > point_step:
            raise ValueError("cloud_xyz_invalid")
        axes[field.name] = (field.offset, reader.unpack_from)
    if len(axes) != 3:
        raise ValueError("cloud_xyz_missing")
    count = width * height
    points = []
    for i in range(0, count, max(1, math.ceil(count / SAMPLE_LIMIT))):
        row, col = divmod(i, width)
        offset = row * row_step + col * point_step
        x, y, z = (axes[k][1](raw, offset + axes[k][0])[0] for k in "xyz")
        if not all(math.isfinite(v) for v in (x, y, z)):
            continue
        point = [r[0]*x + r[1]*y + r[2]*z + r[3] for r in matrix]
        if all(math.isfinite(v) and abs(v) <= 1e6 for v in point):
            points.append(point)
    return points


class Surface:
    def __init__(self, limit=MAX_VOXELS):
        self.cells = OrderedDict()
        self.limit = limit
        self.last_ns = None
        self.evictions = 0

    def update(self, stamp_ns, points):
        if self.last_ns is not None and stamp_ns <= self.last_ns:
            return False
        self.last_ns = stamp_ns
        for point in points:
            key = tuple(math.floor(v/VOXEL_M) for v in point)
            self.cells[key] = [round(v, 3) for v in point]
            self.cells.move_to_end(key)
        while len(self.cells) > self.limit:
            self.cells.popitem(last=False)
            self.evictions += 1
        return True
