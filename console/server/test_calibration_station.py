"""Console proxy contract for registration and task-scoped calibration recordings."""
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from main import Handler


class Station(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            body = json.dumps(self.server.status).encode()
        elif self.path == "/live-frame":
            body = json.dumps(self.server.live).encode()
        elif self.path == "/preview.jpg?task_id=active":
            body = b"test JPEG"
        else:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.writes.append((self.path,json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"confirmed":true}')

    def log_message(self, *args):
        pass


class StationProxy(TestCase):
    def server(self,handler):
        server = ThreadingHTTPServer(("127.0.0.1",0),handler)
        Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def setUp(self):
        self.station = self.server(Station)
        self.station.status = dict(robot_id="rover",environment="hardware",state="PAUSED_FOR_OPERATOR",
                                   message="hold",task_id="active",capture_feedback={"condition":"target_absent"})
        self.station.writes = []
        port = self.station.server_port
        env = patch.dict(os.environ,{"EDGE_CALIBRATION_URL":f"http://127.0.0.1:{port}","EDGE_CONSOLE_OBSERVE_ONLY":"0"})
        env.start()
        self.addCleanup(env.stop)
        observer = SimpleNamespace(robot_id="rover",environment="hardware")
        self.origin = "http://127.0.0.1:" + str(self.server(partial(Handler,observer=observer)).server_port)

    def post(self):
        return Request(self.origin+"/api/calibration-board-geometry",b'{"confirmed":true}',
                       {"Content-Type":"application/json","Origin":self.origin})

    def test_proxies_status_measurements_and_matching_task_media(self):
        with urlopen(self.origin+"/api/calibration-status") as r:
            self.assertEqual(json.load(r)["state"],"PAUSED_FOR_OPERATOR")
        with urlopen(self.origin+"/api/calibration-preview.jpg?task_id=active&frame=1") as r:
            self.assertEqual(r.headers.get_content_type(),"image/jpeg")
            self.assertEqual(r.read(),b"test JPEG")
        with urlopen(self.post()) as r:
            self.assertEqual(json.load(r),{"confirmed":True})
        self.assertEqual(self.station.writes,[("/board-geometry",{"confirmed":True})])
        with self.assertRaises(HTTPError) as exc:
            urlopen(self.origin+"/api/calibration-preview.jpg?task_id=older")
        self.assertEqual(exc.exception.code,404)

    def test_proxies_verified_camera_metrics_to_the_workbench(self):
        result = dict(k=[[460,0,320],[0,460,240],[0,0,1]],holdout_rms_px=.98,
                      transforms={"front":{"validation":{"max_hole_error_m":.015}}})
        self.station.status.update(state="VERIFIED", camera_result=result, samples=10)
        with urlopen(self.origin+"/api/calibration-status") as response:
            status = json.load(response)
        self.assertEqual(status["camera_result"], result)
        self.assertEqual(status["samples"], 10)

    def test_rejects_wrong_robot_external_origin_and_read_only_registration(self):
        req = self.post()
        req.add_header("Origin","http://another-origin")
        with self.assertRaises(HTTPError) as exc:
            urlopen(req)
        self.assertEqual(exc.exception.code,403)
        with patch.dict(os.environ,{"EDGE_CONSOLE_OBSERVE_ONLY":"1"}), self.assertRaises(HTTPError) as exc:
            urlopen(self.post())
        self.assertEqual(exc.exception.code,403)
        self.station.status["robot_id"] = "different"
        with self.assertRaises(HTTPError) as exc:
            urlopen(self.post())
        self.assertEqual(exc.exception.code,503)
        self.assertEqual(self.station.writes,[])

    def test_live_frame_in_idle_and_paused_station_validates_freshness_and_identity(self):
        self.station.live = dict(available=True,source="live_camera",robot_id="rover",environment="hardware",
                                 jpeg="anBlZw==",stamp_ns="42",received_unix_ns=str(time.time_ns()),max_age_ms=1500)
        for task in ("active",None):
            self.station.status["task_id"] = task
            with urlopen(self.origin+"/api/calibration-live-frame") as r:
                self.assertEqual(r.getheader("Cache-Control"),"no-store")
                self.assertEqual(json.load(r)["stamp_ns"],"42")
        for invalid in (dict(received_unix_ns=str(time.time_ns()-2_000_000_000)),dict(robot_id="wrong"),
                        dict(source="archive"),dict(available=False)):
            with patch.dict(self.station.live,invalid), self.assertRaises(HTTPError) as exc:
                urlopen(self.origin+"/api/calibration-live-frame")
            self.assertEqual(exc.exception.code,503)
            self.assertNotIn("jpeg",json.load(exc.exception))
        self.assertEqual(self.station.writes,[])

    def test_motion_check_matches_station_and_expires_independently_of_test_receipt(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "probe.json"
            sample = dict(schema="edge.calibration.motion-preflight.v1", robot_id="rover", task_id="active",
                          observed_at_ns=str(time.time_ns()), valid_for_ms=5000, status="blocked",
                          checks=[dict(label="rear guard", ok=False, detail="disabled")], last_test=None)
            url = self.origin + "/api/calibration-motion-preflight"
            with patch.dict(os.environ, {"EDGE_CALIBRATION_MOTION_PREFLIGHT": str(path)}):
                path.write_text(json.dumps(sample))
                with urlopen(url) as response:
                    live = json.load(response)
                self.assertTrue(live["fresh"])
                self.assertEqual(live["status"], "blocked")
                self.assertIsNone(live["last_test"])
                receipt = dict(schema="edge.calibration.motion-test-summary.v1", task_id="other",
                               robot_id="rover", status="ZERO_COMMAND_VERIFIED", summary="zero only")
                last_test = path.with_name("last-motion-test.json")
                last_test.write_text(json.dumps(receipt))
                with urlopen(url) as response:
                    self.assertIsNone(json.load(response)["last_test"])
                last_test.write_text(json.dumps({**receipt, "task_id": "active"}))
                with urlopen(url) as response:
                    checked = json.load(response)
                self.assertEqual(checked["last_test"]["status"], "ZERO_COMMAND_VERIFIED")
                self.assertEqual(checked["status"], "blocked")
                path.write_text(json.dumps({**sample, "observed_at_ns": str(time.time_ns()-6_000_000_000)}))
                with urlopen(url) as response:
                    old = json.load(response)
                self.assertFalse(old["fresh"])
                self.assertEqual((old["status"], old["observed_status"]), ("unavailable", "blocked"))
                path.write_text(json.dumps({**sample, "task_id": "other"}))
                with self.assertRaises(HTTPError) as exc:
                    urlopen(url)
                self.assertEqual(exc.exception.code, 404)
