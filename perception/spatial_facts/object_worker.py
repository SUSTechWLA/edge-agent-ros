"""Run an approved CPU object detector on a private Unix socket, one RGB PNG per call."""
import argparse
import json
import os
from pathlib import Path
import socket
from socketserver import StreamRequestHandler, UnixStreamServer
import stat
import struct
import zlib

from edge_contracts.image_frames import chunk
from perception.spatial_facts.object_server import MAX_PNG, MAX_RESULT, read_message, write_message
from perception.spatial_facts.visual_grounding import rgb_raster


def warmup_png():
    width, height = 64, 64
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = bytes(height * (1 + width * 3))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def handler(predict):
    class Handler(StreamRequestHandler):
        timeout = 6.
        def handle(self):
            try:
                rgb = read_message(self.rfile, MAX_PNG)
                rgb_raster(rgb)
                result = json.dumps(predict(rgb), allow_nan=False, separators=(",", ":")).encode()
                write_message(self.wfile, result, MAX_RESULT)
            except (OSError, ValueError, TypeError, RuntimeError):
                try:
                    write_message(self.wfile, b'{"error":"object.model_inference_failed"}', MAX_RESULT)
                except OSError:
                    pass
    return Handler


def serve(address, predict):
    import fcntl
    with address.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if address.exists() or address.is_symlink():
            if address.is_symlink() or not stat.S_ISSOCK(address.lstat().st_mode):
                raise ValueError("expected the private detector socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.settimeout(.25)
                    probe.connect(os.fspath(address))
                except ConnectionRefusedError:
                    address.unlink()
                else:
                    raise ValueError("another peer owns the detector socket")
        try:
            with UnixStreamServer(os.fspath(address), handler(predict)) as server:
                address.chmod(0o660)
                print("detector socket ready", flush=True)
                server.serve_forever()
        finally:
            address.unlink(missing_ok=True)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--weights", type=Path, required=True)
    cli.add_argument("--expected-digest", required=True)
    cli.add_argument("--socket", type=Path, required=True)
    args = cli.parse_args()
    from perception.spatial_facts.object_models import load_yolo
    predict = load_yolo(args.weights, args.expected_digest, device="cpu")
    predict(warmup_png())
    serve(args.socket, predict)


if __name__ == "__main__":
    main()
