"""Read-only ROS RGB-D subscriber and pixel measurement HTTP plugin."""
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CameraInfo, Image

from pairs import configuration, Synchronizer
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))
from edge_contracts.preview_http import PreviewHTTP, json_reply
from module_journal import ModuleJournal

WEB = Path(__file__).with_name("web.html")
QOS = QoSProfile(depth=2, reliability=ReliabilityPolicy.BEST_EFFORT)


class RGBD(Node):
    def __init__(self):
        super().__init__("edge_rgbd_measurement")
        self.lock = threading.RLock()
        self.run = uuid.uuid4().hex
        self.journal = ModuleJournal(os.getenv("EDGE_RGBD_JOURNAL", "/journal/module-events.sqlite"))
        self.started_event = self.fact("started", {}, None)
        self.state_event = self.started_event
        self.sync = Synchronizer(self.run)
        self.config = {}
        self.config_error = None
        try:
            with open(os.getenv("EDGE_RGBD_CONFIG", "/config/simulation.json"), "rb") as f:
                self.config = configuration(f.read(8193))
            for kind, cls, key in (("color", Image, "color_topic"), ("depth", Image, "aligned_depth_topic"),
                                   ("info", CameraInfo, "info_topic")):
                self.create_subscription(cls, self.config[key], lambda msg, k=kind: self.receive(k, msg), QOS)
        except (OSError, ValueError, RuntimeError) as ex:
            self.config_error = str(ex) if isinstance(ex, ValueError) else "config_unavailable"
            self.sync.clear("invalid", self.config_error)
        self.state_event = self.fact("configured", {"source_id": self.config.get("id"), "sources": self.config,
                                     "error": self.config_error}, self.started_event)
        self.next_pulse = 0
        self.create_timer(.25, self.tick, clock=Clock(clock_type=ClockType.STEADY_TIME))

    def fact(self, kind, data, parent):
        with self.journal.db:
            return self.journal.emit({"module": "rgbd", "kind": "rgbd." + kind, "code": "rgbd." + kind,
                                      "trace_id": self.run, "parent_event_id": parent, "data": data})

    def transition(self, previous):
        if previous != (self.sync.status, self.sync.reason):
            data = {"source_id": self.config.get("id"), "status": self.sync.status, "reason": self.sync.reason,
                    "alignment": self.config.get("alignment")}
            if self.sync.history:
                data.update(self.sync.history[0].details())
            self.state_event = self.fact("source_state", data, self.state_event)

    def receive(self, kind, msg):
        with self.lock:
            old = self.sync.status, self.sync.reason
            self.sync.accept(kind, msg, time.monotonic(), self.get_clock().now().nanoseconds)
            self.transition(old)

    def refresh(self):
        old = self.sync.status, self.sync.reason
        self.sync.refresh(time.monotonic(), self.get_clock().now().nanoseconds)
        self.transition(old)

    def tick(self):
        with self.lock:
            self.refresh()
            now = time.monotonic()
            if now >= self.next_pulse:
                with self.journal.db:
                    self.journal.heartbeat(10)
                self.next_pulse = now + 2

    def response(self, path):
        route = urlsplit(path)
        if route.path == "/health":
            return json_reply(200, {"id": "rgbd", "version": 1, "started_event": self.started_event})
        if route.path == "/":
            return 200, "text/html; charset=utf-8", WEB.read_bytes(), {}
        try:
            args = parse_qs(route.query, max_num_fields=3, keep_blank_values=True)
        except ValueError:
            return json_reply(400, {"error": "invalid_query"})
        if any(len(values) != 1 for values in args.values()):
            return json_reply(400, {"error": "duplicate_parameter"})
        if route.path not in ("/api/rgbd", "/api/sample", "/frame/color.png", "/frame/depth.png"):
            return json_reply(404, {"error": "not_found"})
        with self.lock:
            self.refresh()
            if route.path == "/api/rgbd":
                data = {"status": self.sync.status, "reason": self.sync.reason, "pair_id": None,
                        "sources": self.config, "ttl_s": self.sync.ttl, "max_skew_ms": self.sync.skew_ns / 1e6}
                if self.sync.history:
                    data.update(self.sync.history[0].details())
                return json_reply(200, data)
            result, pair = self.sync.find(args.get("pair", [None])[0])
            if result != 200:
                return json_reply(result, pair)
        if route.path == "/api/sample":
            try:
                u, v = (int(args[k][0]) for k in ("u", "v"))
            except (ValueError, KeyError):
                return json_reply(400, {"error": "invalid_pixel"})
            result, payload = pair.sample(u, v)
            return json_reply(result, payload)
        kind = "color" if route.path == "/frame/color.png" else "depth"
        # Work on this immutable pair, outside ROS callback locking. Limit duplicate encodes.
        with self.render:
            return 200, "image/png", pair.preview(kind), {"X-Pair-Id": pair.id}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        status, content_type, body, headers = self.server.node.response(self.path)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.path.partition("?")[0] == "/health":
            self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def main():
    rclpy.init()
    node = RGBD()
    node.render = threading.Lock()
    http = PreviewHTTP((os.getenv("EDGE_RGBD_BIND", "0.0.0.0"), int(os.getenv("EDGE_RGBD_PORT", "8772"))), node, Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        rclpy.spin(node)
    finally:
        http.shutdown()
        thread.join()
        http.server_close()
        node.destroy_node()
        node.journal.close()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
