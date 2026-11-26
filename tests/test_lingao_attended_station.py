import http.client
import json
import socket
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from calibration.lingao_attended_station import Station


class Stub:
    def __init__(self):
        self.events = []
        self.wait_for_device = threading.Event()
        self.device_read = threading.Event()

    def status(self):
        return {"phase": "ready", "ready": True}

    def begin(self, op, holder, *, authorized, profile_id):
        self.events.append(("begin", op))
        return {"phase": "running"}

    def release(self, op, holder):
        self.events.append(("release", op))
        return {"phase": "stopping"}

    def heartbeat(self, op, holder, seq, *, authorized, held):
        self.events.append(("hold", seq))
        self.device_read.set()
        self.wait_for_device.wait(2)
        return {"phase": "running"}


class StationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "sock")
        self.core = Stub()
        server = Station(self.path, 8926, self.core, b"<script></script>")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 1)
        self.addCleanup(server.shutdown)
        self.op = str(uuid.uuid4())
        self.host = "127.0.0.1:8926"
        self.cookie = self.login()

    def request(self, path, *, method="GET", cookie=None, origin=None, host=None, body=None):
        client = http.client.HTTPConnection("localhost", timeout=3)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(self.path)
            client.sock = sock
            headers = {"Host": host or self.host}
            if cookie:
                headers["Cookie"] = cookie
            if origin:
                headers["Origin"] = origin
            if body is not None:
                body = json.dumps(body)
                headers["Content-Type"] = "application/json"
            client.request(method, path, body=body, headers=headers)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()

    def login(self):
        status, headers, html = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"nonce", html)
        return headers["Set-Cookie"].split(";")[0]

    def action(self, path, seq, *, cookie=None, origin=None, op=None):
        return self.request(path, method="POST", cookie=cookie or self.cookie,
                            origin=origin or "http://" + self.host,
                            body={"operation_id": op or self.op, "seq": seq})[0]

    def test_read_only_and_origins(self):
        self.assertEqual(self.request("/state", cookie=self.cookie)[0], 200)
        self.assertEqual(self.request("/state")[0], 404)
        self.assertEqual(self.request("/", host="other")[0], 403)
        self.assertEqual(self.action("/begin", 0, origin="http://other"), 403)
        self.assertEqual(self.request("/hold", cookie=self.cookie)[0], 404)
        self.assertEqual(self.core.events, [])

    def test_release_overtakes_begin(self):
        self.assertEqual(self.action("/release", 1), 200)
        self.assertEqual(self.action("/begin", 0), 409)
        self.assertEqual(self.action("/hold", 1), 409)
        self.assertEqual(self.core.events, [("release", self.op)])

    def test_owner_sequence_and_replay(self):
        other = self.login()
        self.core.wait_for_device.set()
        self.assertEqual(self.action("/begin", 0), 200)
        self.assertEqual(self.action("/hold", 1, cookie=other), 409)
        self.assertEqual(self.action("/hold", 1), 200)
        self.assertEqual(self.action("/hold", 1), 409)
        self.assertEqual(self.action("/release", 2), 200)
        self.assertEqual(self.action("/hold", 3), 409)
        self.assertEqual(self.action("/begin", 0, op=str(uuid.uuid4())), 409)
        self.assertEqual(self.core.events, [("begin", self.op), ("hold", 1), ("release", self.op)])

    def test_release_during_device_write(self):
        self.assertEqual(self.action("/begin", 0), 200)
        worker = threading.Thread(target=self.action, args=("/hold", 1))
        worker.start()
        try:
            self.assertTrue(self.core.device_read.wait(1))
            self.assertEqual(self.action("/release", 2), 200)
            self.assertTrue(worker.is_alive())
        finally:
            self.core.wait_for_device.set()
            worker.join(2)
