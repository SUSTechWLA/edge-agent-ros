"""DSH odometry status, backed by the site's read-only current pose endpoint."""
import argparse
from http.client import HTTPException
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

try:
    from .planner import finite, stamp
    from .provider import Handler, Provider
except ImportError:
    from planner import finite, stamp
    from provider import Handler, Provider


def measured_status(sample, now_ns):
    if not isinstance(sample, dict) or sample.get("valid") is not True:
        raise ValueError("odometry unavailable")
    source_ns = stamp(sample.get("stamp_ns"))
    received_ns = stamp(sample.get("wall_received_ns"))
    if any(not -50_000_000 <= now_ns - t <= 500_000_000 for t in (source_ns, received_ns)):
        raise ValueError("stale odometry or clock mismatch")
    if any(not isinstance(sample.get(k), str) or not sample[k] for k in ("frame_id", "child_frame_id", "source_topic")):
        raise ValueError("odometry source or frame unavailable")
    if not all(finite(sample.get(k)) for k in ("x_m", "y_m", "yaw_rad")):
        raise ValueError("invalid planar position")
    linear = sample.get("linear_velocity_mps")
    if not isinstance(linear, list) or len(linear) != 3 or not all(finite(v) for v in linear):
        raise ValueError("odometry requires signed body-frame velocity")

    seconds, nanos = divmod(source_ns, 1_000_000_000)
    return {
        "source": sample["source_topic"], "frame": sample["frame_id"], "child_frame": sample["child_frame_id"],
        "stamp_ns": source_ns, "source_stamp_ns": str(source_ns), "stamp": {"sec": seconds, "nanosec": nanos},
        "x": sample["x_m"], "y": sample["y_m"], "yaw": sample["yaw_rad"], "speed": linear[0],
        "pose": sample, "map_anchor": sample.get("map_from_odom"),
    }


def read_current_pose(url):
    with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("odometry sample exceeds limit")
    return measured_status(json.loads(raw), time.time_ns())


class StatusProvider(Provider):
    version = "1.0.0"
    def __init__(self, state, facts):
        super().__init__(state, {}, facts)
        self.url = facts.rstrip("/") + "/pose/current"

    def run(self, operation_id):
        try:
            measurement = read_current_pose(self.url)
            receipt = {"operation_id": operation_id, "state": "SUCCEEDED", "evidence": measurement}
        except (OSError, HTTPException, ValueError, TypeError, OverflowError) as exc:
            receipt = {"operation_id": operation_id, "state": "FAILED",
                       "error": {"code": "tool.odom_stale", "message": str(exc)[:240]}}
        self.update(operation_id, "complete", receipt)
        return receipt


class StatusHandler(Handler):
    def do_GET(self):
        if urlsplit(self.path).path == "/health":
            return self.send_json(200, {"ready": True, "plugin_id": "sensing.rover_odometry",
                                        "plugin_version": "1.0.0", "tool": "status"})
        return super().do_GET()


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--facts", required=True)
    cli.add_argument("--state", default=Path("/state"), type=Path)
    cli.add_argument("--bind", default="127.0.0.1")
    cli.add_argument("--port", default=8151, type=int)
    cli.add_argument("--check", action="store_true", help="read a new measured sample and exit")
    args = cli.parse_args()
    if args.check:
        try:
            print(json.dumps(read_current_pose(args.facts.rstrip("/") + "/pose/current"), allow_nan=False))
        except (OSError, HTTPException, ValueError, TypeError, OverflowError) as exc:
            cli.exit(1, str(exc) + "\n")
    else:
        server = ThreadingHTTPServer((args.bind, args.port), StatusHandler)
        server.provider = StatusProvider(args.state, args.facts)
        server.serve_forever()
