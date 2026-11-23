import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from perception.rover_preview.status import StatusHandler, StatusProvider, measured_status


def sample(now):
    return {"valid": True, "source_topic": "/lingao/odom", "frame_id": "odom", "child_frame_id": "base_foot",
            "stamp_ns": str(now), "wall_received_ns": str(now), "x_m": 1.2, "y_m": -2.3, "yaw_rad": 0.3,
            "linear_velocity_mps": [-0.15, 0., 0.], "map_from_odom": None}


class StatusTests(unittest.TestCase):
    def test_base_wire_and_freshness(self):
        now = time.time_ns()
        raw = sample(now)
        result = measured_status(raw, now)
        self.assertEqual(result["speed"], -0.15)
        self.assertEqual(result["stamp_ns"], now)
        self.assertEqual(result["source_stamp_ns"], str(now))
        self.assertEqual(result["source"], "/lingao/odom")
        self.assertEqual(result["frame"], "odom")
        self.assertIsNone(result["map_anchor"])
        for bad in ({**raw, "valid": False}, {**raw, "stamp_ns": str(now - 501_000_000)},
                    {**raw, "wall_received_ns": str(now + 51_000_000)},
                    {**raw, "linear_velocity_mps": None}, {**raw, "x_m": float("nan")}):
            with self.subTest(sample=bad), self.assertRaises(ValueError):
                measured_status(bad, now)

    def test_http_adapter_preserves_original_pose_after_restart(self):
        class Facts(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.reads.append(self.path)
                self.send_response(200)
                self.end_headers()
                raw = sample(time.time_ns()) if self.server.good else {"valid": False}
                self.wfile.write(json.dumps(raw).encode())

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            facts = ThreadingHTTPServer(("127.0.0.1", 0), Facts)
            facts.reads = []
            facts.good = True
            api = ThreadingHTTPServer(("127.0.0.1", 0), StatusHandler)
            upstream = "http://127.0.0.1:" + str(facts.server_port)
            api.provider = StatusProvider(state, upstream)
            for server in (facts, api):
                threading.Thread(target=server.serve_forever, daemon=True).start()
            origin = "http://127.0.0.1:" + str(api.server_port)

            def invoke(op):
                body = json.dumps({"operation_id": op, "arguments": {}}).encode()
                with urlopen(Request(origin + "/invoke", body, {"Content-Type": "application/json"})) as response:
                    return json.load(response)

            try:
                with self.assertRaises(HTTPError) as disabled:
                    urlopen(Request(origin+"/evaluate", b"{}", {"Content-Type": "application/json"}), timeout=2)
                self.assertEqual(disabled.exception.code, 404)
                disabled.exception.close()
                with urlopen(origin + "/health") as response:
                    self.assertEqual(json.load(response)["tool"], "status")
                first = invoke("pose-001")
                self.assertEqual(first["state"], "SUCCEEDED")
                api.provider.pool.shutdown()
                api.provider = StatusProvider(state, upstream)
                facts.good = False
                self.assertEqual(first, invoke("pose-001"))
                failed = invoke("pose-002")
                self.assertEqual(failed["state"], "FAILED")
                facts.good = True
                self.assertEqual(invoke("pose-002"), failed)
                again = invoke("pose-003")
                self.assertGreater(again["evidence"]["stamp_ns"], first["evidence"]["stamp_ns"])
                self.assertEqual(facts.reads, ["/pose/current"] * 3)
            finally:
                api.provider.pool.shutdown()
                for server in (api, facts):
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
