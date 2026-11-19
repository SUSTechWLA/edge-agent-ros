"""Measure a supplied planar trajectory over the v1 read-only Tool protocol."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math


def measure(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"points_m"} or not isinstance(arguments["points_m"], str):
        raise ValueError("expected a points_m JSON array string")
    points = json.loads(arguments["points_m"])
    if not isinstance(points, list) or not 1 <= len(points) <= 256:
        raise ValueError("expected 1 to 256 planar samples")
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("each sample must have two coordinates in metres")
        if not all(type(v) in (int, float) and math.isfinite(v) for v in point):
            raise ValueError("coordinates must be finite numbers")
    length = math.fsum(math.dist(a, b) for a, b in zip(points, points[1:]))
    displacement = math.dist(points[0], points[-1])
    if not math.isfinite(length) or not math.isfinite(displacement):
        raise ValueError("distance exceeds the numeric range")
    return {"sample_count": len(points), "path_length_m": length, "displacement_m": displacement}


def invoke(body):
    if not isinstance(body, dict):
        raise ValueError("expected a request object")
    op = body.get("operation_id")
    if not isinstance(op, str) or not op.strip() or len(op) > 256:
        raise ValueError("invalid operation_id")
    if body.get("version") != "1":
        return {"operation_id": op, "state": "BLOCKED",
                "error": {"code": "track.version_mismatch", "message": "expected Tool version 1"}}
    try:
        evidence = measure(body.get("arguments"))
    except (ValueError, OverflowError, RecursionError) as exc:
        return {"operation_id": op, "state": "BLOCKED",
                "error": {"code": "track.invalid_points", "message": str(exc)}}
    return {"operation_id": op, "state": "SUCCEEDED", "evidence": evidence}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/invoke":
            self.send_error(404)
            return
        try:
            self.connection.settimeout(5)
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536:
                raise ValueError("expected a request body of 1 to 65536 bytes")
            data = self.rfile.read(size)
            if len(data) != size:
                raise ValueError("incomplete body")
            result = invoke(json.loads(data.decode("utf-8")))
            status = 200
        except (ValueError, RecursionError, TimeoutError) as exc:
            result = {"error": {"code": "track.invalid_request", "message": str(exc)}}
            status = 400
        payload = json.dumps(result, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8125)
    args = parser.parse_args()
    with ThreadingHTTPServer((args.bind, args.port), Handler) as server:
        server.serve_forever()
