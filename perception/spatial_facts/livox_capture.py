"""Passively copy approved MID-360 ingress packets into bounded per-sensor point previews.

Linux AF_PACKET/SOCK_DGRAM delivers IP datagrams without binding the driver's UDP port.
The kernel filter is installed before reception starts; no NIC mode or device settings change.
"""
import argparse
from collections import Counter
from contextlib import ExitStack
import ctypes
import hashlib
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import socket
import struct
import time
import zlib

from edge_contracts.spatial_facts import identifier
from perception.spatial_facts.artifact_ring import ArtifactRing


LIVOX_HEADER = struct.Struct("<BHHHHBBB12xIQ")
CARTESIAN_MM = struct.Struct("<iiiBB")
IPV4 = 0x0800
SOURCE_PORT, DEST_PORT = 56300, 56301
SO_ATTACH_FILTER, SO_LOCK_FILTER = 26, 44
SO_TIMESTAMPNS = 35  # Linux 64-bit ABIs (Jetson aarch64, x86_64); struct timespec
TIMESPEC = struct.Struct("@ll")
WINDOW_NS = 200_000_000
MAX_POINTS = 1200
KEEP_ARTIFACTS = 300  # 60s at 5 Hz, per physical source


class BpfInstruction(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]


class BpfProgram(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("insns", ctypes.POINTER(BpfInstruction))]


def attach_ingress_filter(rx, dest, sources):
    # Classic BPF on a cooked IPv4 socket. Only ordinary unfragmented IPv4/UDP with
    # the approved destination, exact ports and one of the approved source IPs passes.
    # Every failed condition immediately returns zero bytes from the packet.
    ld_byte, ld_half, ld_word, jeq, jset, ret = 0x30, 0x28, 0x20, 0x15, 0x45, 0x06
    code = []
    for load, at, value in [(ld_byte, 0, 0x45), (ld_byte, 9, 17),
                            (ld_word, 16, int(dest)), (ld_half, 20, SOURCE_PORT), (ld_half, 22, DEST_PORT)]:
        code.extend([(load, 0, 0, at), (jeq, 1, 0, value), (ret, 0, 0, 0)])
    code.extend([(ld_half, 0, 0, 6), (jset, 0, 1, 0x3fff), (ret, 0, 0, 0), (ld_word, 0, 0, 12)])
    for source in sources:
        code.extend([(jeq, 0, 1, int(source)), (ret, 0, 0, 2048)])
    code.append((ret, 0, 0, 0))
    instructions = (BpfInstruction * len(code))(*(BpfInstruction(*op) for op in code))
    program = BpfProgram(len(code), instructions)
    rx.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, bytes(program))
    rx.setsockopt(socket.SOL_SOCKET, SO_LOCK_FILTER, 1)


def open_receiver(interface, dest, sources):
    if TIMESPEC.size != 16:
        raise RuntimeError("this receiver requires a Linux 64-bit timespec")
    rx = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, 0)
    try:
        attach_ingress_filter(rx, dest, sources)
        rx.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
        rx.settimeout(.2)
        rx.bind((interface, IPV4))
    except BaseException:
        rx.close()
        raise
    return rx


def release_process_capabilities():
    """After opening the filtered FD, relinquish all capabilities in this Linux process."""
    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int32)]

    class Data(ctypes.Structure):
        _fields_ = [(field, ctypes.c_uint32) for field in ("effective", "permitted", "inheritable")]

    libc = ctypes.CDLL(None, use_errno=True)
    # no_new_privs survives exec. Version 3 uses two words per capability set.
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.capset(ctypes.byref(Header(0x20080522, 0)), (Data * 2)()) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def receive(rx, destination, sources):
    ip, aux, flags, address = rx.recvmsg(2048, socket.CMSG_SPACE(TIMESPEC.size))
    if address[2] != socket.PACKET_HOST or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(ip) < 28:
        return None
    src, dst, udp_len = struct.unpack_from("!HHH", ip, 20)
    source = ip[12:16]
    if (source not in sources or ip[16:20] != destination or src != SOURCE_PORT or dst != DEST_PORT
            or udp_len < 8 or len(ip) != 20 + udp_len or struct.unpack_from("!H", ip, 2)[0] != len(ip)):
        return None
    for level, option, buf in aux:
        if level == socket.SOL_SOCKET and option == SO_TIMESTAMPNS and len(buf) == TIMESPEC.size:
            sec, nano = TIMESPEC.unpack(buf)
            if sec > 0 and 0 <= nano < 10**9:
                return source, sec * 10**9 + nano, ip[28:]
    return None


class PreviewWindow:
    def __init__(self, source_id, frame_id):
        self.source_id, self.frame_id = source_id, frame_id
        self.first = self.last = self.first_device = self.last_device = None
        self.clock_type = None
        self.packets = self.decoded = self.valid = 0
        self.checksums = Counter()
        self.points = []

    def add(self, payload, received):
        if len(payload) < LIVOX_HEADER.size:
            return "invalid_size"
        version, length, _, count, _, _, kind, clock, crc, stamp = LIVOX_HEADER.unpack_from(payload)
        if version or kind != 1 or not 0 < count <= 96 or clock not in (0, 1, 2):
            return "unsupported_packet"
        if length != len(payload) or length != LIVOX_HEADER.size + count * CARTESIAN_MM.size:
            return "invalid_size"
        if crc and zlib.crc32(payload[28:]) != crc:
            return "invalid_crc"
        if self.first is None:
            self.first, self.first_device, self.clock_type = received, stamp, clock
        elif self.clock_type != clock or stamp < self.last_device or received < self.last:
            return "time_discontinuity"
        self.last, self.last_device = received, stamp
        self.checksums["verified" if crc else "device_field_zero"] += 1
        self.packets += 1
        # Spread samples across the entire window and packet rather than taking the
        # first N consecutive points. Keep raw per-source ranging and reflectivity.
        if (self.packets - 1) % 5:
            return None
        self.decoded += 1
        phase = self.decoded % 11
        for index, (x, y, z, intensity, _) in enumerate(CARTESIAN_MM.iter_unpack(payload[LIVOX_HEADER.size:])):
            if x or y or z:
                self.valid += 1
                if index % 11 == phase and len(self.points) < MAX_POINTS:
                    self.points.append([x / 1000, y / 1000, z / 1000, intensity])
        return None

    def payload(self):
        return {"schema_version": 1, "type": "livox.mid360.preview",
                "source_id": self.source_id, "frame_id": self.frame_id,
                "stamp_basis": "host_packet_receive", "first_receive_ns": str(self.first),
                "last_receive_ns": str(self.last), "device_time_type": self.clock_type,
                "first_device_stamp_ns": str(self.first_device), "last_device_stamp_ns": str(self.last_device),
                "packet_count": self.packets, "decoded_packet_count": self.decoded, "crc_counts": dict(self.checksums),
                "valid_point_count": self.valid, "sampled_point_count": len(self.points),
                "point_fields": ["x_m", "y_m", "z_m", "reflectivity"], "points": self.points}


def publish(ring, actor, window):
    data = json.dumps(window.payload(), separators=(",", ":"), allow_nan=False).encode()
    reference = hashlib.sha256(data).hexdigest()
    sample = {"kind": "artifact", "actor_ref": actor, "source_id": window.source_id,
              "frame_id": window.frame_id, "clock_id": "unix", "stamp_ns": window.last,
              "artifact_ref": "sha256:" + reference, "calibration_ref": "livox.cartesian-mm.device-frame",
              "media_type": "application/json"}
    ring.publish(sample, data)


def capture(interface, dest, actor, sources, output, seconds):
    import fcntl

    output.mkdir(parents=True, exist_ok=True)
    expiry = time.monotonic() + seconds if seconds is not None else float("inf")
    windows = {ip.packed: PreviewWindow(sid, frame) for ip, sid, frame in sources}
    errors = Counter()
    emitted = Counter()
    with ExitStack() as stack:
        rings = {}
        for ip, sid, _ in sources:
            directory = output / sid
            directory.mkdir(exist_ok=True)
            if directory.resolve(strict=True) != directory.absolute():
                raise ValueError("expected a private source directory")
            lock = stack.enter_context((directory / "writer.lock").open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rings[ip.packed] = ArtifactRing(directory, actor, sid, media_type="application/json", suffix=".json",
                                            retain=KEEP_ARTIFACTS, max_bytes=65536)
        rx = stack.enter_context(open_receiver(interface, dest, [ip for ip, _, _ in sources]))
        release_process_capabilities()
        print(json.dumps({"stage": "ready", "interface": interface, "sources": sorted(sid for _, sid, _ in sources),
                          "window_ms": WINDOW_NS // 1000000, "retained_per_source": KEEP_ARTIFACTS,
                          "capture": "kernel_filtered_receive", "process_capabilities": "released"}), flush=True)
        while time.monotonic() < expiry:
            try:
                packet = receive(rx, dest.packed, windows)
            except TimeoutError:
                continue
            if packet is None:
                continue
            ip, stamp, data = packet
            window = windows[ip]
            if window.first is not None and stamp - window.first >= WINDOW_NS:
                if window.points:
                    publish(rings[ip], actor, window)
                    emitted[window.source_id] += 1
                window = windows[ip] = PreviewWindow(window.source_id, window.frame_id)
            reason = window.add(data, stamp)
            if reason:
                errors[(window.source_id, reason)] += 1
            if reason == "time_discontinuity":
                windows[ip] = PreviewWindow(window.source_id, window.frame_id)
        print(json.dumps({"published": dict(emitted), "rejected": {"/".join(k): v for k, v in errors.items()}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--destination", type=IPv4Address, required=True)
    parser.add_argument("--source", action="append", nargs=3, required=True, metavar=("IP", "SOURCE_ID", "FRAME"))
    parser.add_argument("--actor-ref", required=True)
    parser.add_argument("--output", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--seconds", type=int, default=300, help="bounded capture duration, at most one hour")
    mode.add_argument("--continuous", action="store_true", help="keep publishing bounded windows for later tasks")
    args = parser.parse_args()
    sources = [(IPv4Address(ip), sid, frame) for ip, sid, frame in args.source]
    if (not 0 < args.seconds <= 3600 or not identifier(args.actor_ref) or not 1 <= len(sources) <= 8
            or len({s[0] for s in sources}) != len(sources) or len({s[1] for s in sources}) != len(sources)
            or any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", sid) or not identifier(frame)
                   for _, sid, frame in sources)):
        parser.error("invalid capture duration, source identity or actor")
    capture(args.interface, args.destination, args.actor_ref, sources, args.output, None if args.continuous else args.seconds)


if __name__ == "__main__":
    main()
