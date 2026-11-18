"""Validate, synchronize and sample aligned RGB-D without depending on ROS or a camera model."""
from collections import deque
from dataclasses import dataclass, field
from itertools import product
import json
import math
from pathlib import Path
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "camera"))
from frames import NAME, TOPIC, chunk, dimensions, raw_frame, unique

KINDS = ("color", "depth", "info")
MAX_PIXELS = 1920 * 1080
MAX_BYTES = MAX_PIXELS * 4 + 8192


def configuration(raw):
    if len(raw) > 8192:
        raise ValueError("config_size")
    spec = json.loads(raw, object_pairs_hook=unique)
    keys = {"id", "color_topic", "aligned_depth_topic", "info_topic", "alignment"}
    if not isinstance(spec, dict) or set(spec) != keys:
        raise ValueError("config_fields")
    if not isinstance(spec["id"], str) or not NAME.fullmatch(spec["id"]):
        raise ValueError("config_id")
    topics = [spec[k] for k in ("color_topic", "aligned_depth_topic", "info_topic")]
    if any(not isinstance(t, str) or len(t) > 200 or not TOPIC.fullmatch(t) for t in topics) or len(set(topics)) != 3:
        raise ValueError("config_topics")
    if spec["alignment"] not in ("gazebo_shared_optical_projection", "driver_aligned_to_color"):
        raise ValueError("alignment_undeclared")
    return spec


def validate(kind, msg):
    dimensions(msg.width, msg.height)
    if msg.width * msg.height > MAX_PIXELS:
        raise ValueError("frame_too_large")
    frame = msg.header.frame_id
    if not frame or len(frame) > 160 or not frame.isascii() or not frame.isprintable():
        raise ValueError("invalid_frame")
    if kind == "info":
        k = msg.k
        if (len(k) != 9 or not all(math.isfinite(v) for v in k) or k[0] <= 0 or k[4] <= 0 or
                not 0 <= k[2] < msg.width or not 0 <= k[5] < msg.height or abs(k[8] - 1) > 1e-6):
            raise ValueError("invalid_intrinsics")
        return
    widths = {"rgb8": 3, "bgr8": 3, "mono8": 1} if kind == "color" else {"16uc1": 2, "32fc1": 4}
    step = widths.get(msg.encoding.lower())
    if not step:
        raise ValueError(kind + "_encoding")
    if msg.step < msg.width * step or msg.step * msg.height != len(msg.data) or len(msg.data) > MAX_BYTES:
        raise ValueError(kind + "_layout")
    if msg.is_bigendian not in (0, 1):
        raise ValueError(kind + "_endian")


def stamp(msg):
    return msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec


def depth_reader(msg):
    mm = msg.encoding.upper() == "16UC1"
    unpack = struct.Struct((">" if msg.is_bigendian else "<") + ("H" if mm else "f"))
    def read(u, v):
        value = unpack.unpack_from(msg.data, v * msg.step + u * unpack.size)[0]
        if math.isfinite(value) and value > 0:
            return value / 1000.0 if mm else value
        return None
    return read


def at_color(msg, u, v):
    encoding = msg.encoding.lower()
    if encoding == "mono8":
        return [msg.data[v * msg.step + u]] * 3
    offset = v * msg.step + u * 3
    rgb = list(msg.data[offset:offset + 3])
    return rgb if encoding == "rgb8" else rgb[::-1]


RAMP = ((250, 200, 66), (223, 92, 58), (90, 87, 166), (36, 146, 166), (33, 63, 98))


def depth_color(z):
    if z is None:
        return b"\x00\x00\x00"
    value = min(4.0, z * 4 / 6)
    i = min(int(value), 3)
    return bytes(round(a + (b - a) * (value - i)) for a, b in zip(RAMP[i], RAMP[i + 1]))


PALETTE = [depth_color(i * 6 / 255) for i in range(256)]


def depth_png(msg):
    scale = max(1, math.ceil(msg.width / 640), math.ceil(msg.height / 480))
    width, height = math.ceil(msg.width / scale), math.ceil(msg.height / scale)
    read = depth_reader(msg)
    rows = bytearray()
    for v in range(0, msg.height, scale):
        rows.append(0)
        for u in range(0, msg.width, scale):
            z = read(u, v)
            rows.extend(b"\0\0\0" if z is None else PALETTE[round(min(z, 6) * 255 / 6)])
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 2)) + chunk(b"IEND", b"")


@dataclass
class Pair:
    id: str
    messages: tuple
    arrival: float
    png: dict = field(default_factory=dict)

    def details(self):
        color, depth, info = self.messages
        stamps = [stamp(m) for m in self.messages]
        cal = {n: float(info.k[i]) for n, i in (("fx", 0), ("fy", 4), ("cx", 2), ("cy", 5))}
        return {"pair_id": self.id, "width": color.width, "height": color.height,
                "frame": color.header.frame_id, "depth_encoding": depth.encoding, "intrinsics": cal,
                "color_stamp_ns": stamps[0], "depth_stamp_ns": stamps[1], "info_stamp_ns": stamps[2],
                "stamp_ns_text": dict(zip(KINDS, map(str, stamps))),
                "skew_ms": (max(stamps) - min(stamps)) / 1e6}

    def sample(self, u, v):
        color, depth, _ = self.messages
        if not (0 <= u < color.width and 0 <= v < color.height):
            return 400, {"error": "pixel_out_of_bounds"}
        z = depth_reader(depth)(u, v)
        out = {"u": u, "v": v, "pair_id": self.id, "rgb": at_color(color, u, v)}
        return (200, {**out, "depth_m": z}) if z is not None else (422, {**out, "error": "no_depth_return"})

    def preview(self, kind):
        if kind not in self.png:
            self.png[kind] = raw_frame(self.messages[0])[0] if kind == "color" else depth_png(self.messages[1])
        return self.png[kind]


class Synchronizer:
    """Finite three-stream queues; a client may pin one of the most recent published pairs."""
    def __init__(self, instance, ttl=2.0, skew_ms=20):
        self.instance, self.ttl, self.skew_ns = instance, ttl, int(skew_ms * 1e6)
        self.seq, self.last_ros = 0, 0
        self.buffers = {kind: deque(maxlen=3) for kind in KINDS}
        self.history = deque(maxlen=4)
        self.last = {}
        self.status, self.reason = "waiting", "waiting_for_streams"

    def clear(self, state, reason):
        self.status, self.reason = state, reason
        self.history.clear()
        for values in self.buffers.values():
            values.clear()

    def refresh(self, now, ros):
        if self.last_ros and ros < self.last_ros:
            self.clear("waiting", "clock_reset")
            self.last.clear()
        self.last_ros = ros
        for buf in self.buffers.values():
            while buf and (now - buf[0][2] > self.ttl or ros - buf[0][0] > self.ttl * 1e9):
                buf.popleft()
        while self.history:
            p = self.history[-1]
            if now - p.arrival <= self.ttl and ros - min(stamp(m) for m in p.messages) <= self.ttl * 1e9:
                break
            self.history.pop()
        if self.status == "online" and not self.history:
            self.status, self.reason = "stale", "stream_timeout"

    def accept(self, kind, msg, now, ros):
        self.refresh(now, ros)
        t = stamp(msg)
        try:
            if ros <= 0 or t <= 0:
                raise ValueError("clock_unavailable")
            if ros - t > self.ttl * 1e9 or t - ros > 500_000_000:
                raise ValueError("stamp_outside_clock")
            if t <= self.last.get(kind, 0):
                return
            validate(kind, msg)
        except ValueError as exc:
            self.clear("invalid", str(exc))
            return
        self.last[kind] = t
        self.buffers[kind].append((t, msg, now))
        if any(not buf for buf in self.buffers.values()):
            return
        choices = product(*(self.buffers[k] for k in KINDS))
        stamps_msgs = min(choices, key=lambda trio: max(x[0] for x in trio) - min(x[0] for x in trio))
        stamps, messages, arrivals = zip(*stamps_msgs)
        if max(stamps) - min(stamps) > self.skew_ns:
            # Preserve unmatched newer arrivals for the next delivery or recovery.
            for buf in self.buffers.values():
                while buf and buf[0][0] < max(stamps) - self.skew_ns:
                    buf.popleft()
            self.history.clear()
            self.status, self.reason = "invalid", "timestamp_skew"
            return
        if len({m.header.frame_id for m in messages}) != 1:
            self.clear("invalid", "frame_mismatch")
            return
        if len({(m.width, m.height) for m in messages}) != 1:
            self.clear("invalid", "size_mismatch")
            return
        for buf, matched in zip(self.buffers.values(), stamps):
            while buf and buf[0][0] <= matched:
                buf.popleft()
        # Publish at most 5 Hz while still checking every incoming triple for mismatches.
        if self.history and now - self.history[0].arrival < .2:
            return
        self.seq += 1
        self.history.appendleft(Pair(f"{self.instance}-{self.seq}", messages, min(arrivals)))
        self.status, self.reason = "online", "aligned"

    def find(self, pair_id):
        if not self.history:
            return 503, {"error": "pair_unavailable", "status": self.status, "reason": self.reason}
        if pair_id is None:
            return 200, self.history[0]
        for pair in self.history:
            if pair.id == pair_id:
                return 200, pair
        return 409, {"error": "pair_expired"}
