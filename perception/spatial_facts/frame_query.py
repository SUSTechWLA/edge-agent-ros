"""Bounded numeric queries on one calibrated, published RGB-D frame."""
import math
import re
import struct

from edge_contracts.spatial_facts import require
from perception.spatial_facts.visual_grounding import calibrated_inputs


DECIMAL = r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
CHANNELS = {"depth", "brightness", "red", "green", "blue"}
REDUCTIONS = {"mean", "median", "min", "max", "coverage"}


def parse(program):
    error = "query.invalid_program"
    require(isinstance(program, str) and program.isascii() and 0 < len(program) <= 128, error)
    parts = [p.strip() for p in program.split("|")]
    require(2 <= len(parts) <= 4 and parts[0] in CHANNELS, error)
    crop = (0., 0., 1., 1.)
    rows = cols = 1
    middle = parts[1:-1]
    if middle and middle[0].startswith("crop"):
        pattern = r"crop\(\s*" + r"\s*,\s*".join(["(" + DECIMAL + ")"] * 4) + r"\s*\)"
        match = re.fullmatch(pattern, middle.pop(0))
        require(match, error)
        crop = tuple(map(float, match.groups()))
        require(0 <= crop[0] < crop[2] <= 1 and 0 <= crop[1] < crop[3] <= 1, error)
    if middle:
        match = re.fullmatch(r"grid\(\s*([1-8])\s*,\s*([1-8])\s*\)", middle.pop(0))
        require(match, error)
        rows, cols = map(int, match.groups())
        require(rows * cols <= 32, error)
    require(not middle, error)
    stat = parts[-1]
    q = None
    if stat not in REDUCTIONS:
        match = re.fullmatch(r"quantile\(\s*(" + DECIMAL + r")\s*\)", stat)
        require(match, error)
        q = float(match[1])
        require(0 <= q <= 1, error)
    return parts[0], crop, rows, cols, stat, q


def channel_samples(raster, bounds, channel):
    raw, _, _, stride = raster
    x0, y0, x1, y1 = bounds
    channel_index = {"red": 0, "green": 1, "blue": 2}.get(channel)
    for y in range(y0, y1):
        start = y * stride + 1
        if channel == "depth":
            segment = raw[start + 2*x0:start + 2*x1]
            for value, in struct.iter_unpack(">H", segment):
                if 0 < value < 65535:
                    yield value
        elif channel == "brightness":
            segment = raw[start + 3*x0:start + 3*x1]
            for r, g, b in struct.iter_unpack("BBB", segment):
                yield (299*r + 587*g + 114*b) / 1000
        else:
            yield from raw[start + 3*x0 + channel_index:start + 3*x1:3]


def reduce_samples(values, stat, q, pixels, scale):
    count = len(values)
    if stat == "coverage":
        return 100 * count / pixels
    if not count:
        return None
    if stat == "min":
        return min(values) * scale
    if stat == "max":
        return max(values) * scale
    if stat == "mean":
        return sum(values) * scale / count
    values.sort()
    index = (.5 if stat == "median" else q) * (count - 1)
    low = math.floor(index)
    return (values[low] + (index - low) * (values[math.ceil(index)] - values[low])) * scale


def execute(program, batch, artifacts, profile, *, now_ns):
    channel, crop, rows, cols, stat, q = parse(program)
    rgb, depth, _, binding = calibrated_inputs(batch, artifacts, profile, now_ns)
    raster = depth if channel == "depth" else rgb
    _, w, h, _ = raster
    x0, y0 = math.floor(crop[0]*w), math.floor(crop[1]*h)
    x1, y1 = math.ceil(crop[2]*w), math.ceil(crop[3]*h)
    require(x1 - x0 >= cols and y1 - y0 >= rows, "query.empty_region")
    regions = []
    scale = binding["depth_scale_m"] if channel == "depth" else 1.
    unit = "%" if stat == "coverage" else ("m" if channel == "depth" else "0..255")
    for row in range(rows):
        for col in range(cols):
            bounds = [x0 + col*(x1-x0)//cols, y0 + row*(y1-y0)//rows,
                      x0 + (col+1)*(x1-x0)//cols, y0 + (row+1)*(y1-y0)//rows]
            pixels = (bounds[2]-bounds[0]) * (bounds[3]-bounds[1])
            values = list(channel_samples(raster, bounds, channel))
            value = reduce_samples(values, stat, q, pixels, scale)
            regions.append({"bounds_xyxy": bounds, "value": value, "valid_pixels": len(values), "pixels": pixels})
    numeric = [r["value"] for r in regions if r["value"] is not None]
    require(numeric, "query.no_valid_pixels")
    imin = [i for i, r in enumerate(regions) if r["value"] == min(numeric)]
    imax = [i for i, r in enumerate(regions) if r["value"] == max(numeric)]
    measures = "、".join("无有效值" if r["value"] is None else f"{r['value']:.3f} {unit}" for r in regions)
    summary = f"{rows} 行 {cols} 列，按从上到下、从左到右依次为 {measures}。"
    if len(regions) > 1:
        summary += "最小值在第 " + "、".join(str(i+1) for i in imin) + " 区。"
        summary += "最大值在第 " + "、".join(str(i+1) for i in imax) + " 区。"
    return {**binding, "schema_version": 1, "program": program, "statistic": stat, "channel": channel,
            "unit": unit, "grid": [rows, cols], "regions": regions, "minimum_indices": imin,
            "maximum_indices": imax, "summary": summary}
