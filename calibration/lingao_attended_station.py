"""Operator-only field console over an SSH-forwarded, owner-private Unix socket."""
import argparse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import secrets
import signal
from socketserver import ThreadingMixIn, UnixStreamServer
import stat
import threading
import uuid


PROFILE = "forward-50mm-100mmps"


class Station(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    request_queue_size = 20

    def __init__(self, path, port, core, page):
        self.host = f"127.0.0.1:{port}"
        self.core = core
        self.page = page
        self.holders = {}
        self.identities = threading.Lock()
        self.claims = threading.Lock()
        self.operation = None
        self.last_seq = -1
        self.released = True
        super().__init__(str(path), Handler)
        os.chmod(path, 0o600)

    def login(self, cookie):
        with self.identities:
            if cookie in self.holders:
                return cookie
            if len(self.holders) >= 32:
                raise ValueError("operator_session_limit")
            token = secrets.token_urlsafe(32)
            self.holders[token] = secrets.token_hex(16)
            return token

    def view(self, result=None):
        state = result if isinstance(result, dict) else self.core.status()
        return {**state, "profile_id":PROFILE}

    def perform(self, verb, op, holder, seq):
        with self.claims:
            if verb == "release":
                if self.operation is None:
                    # Fail closed if an old browser sends a release before its begin.
                    self.operation = op, holder
                elif self.operation != (op, holder):
                    raise ValueError("operation_owner_mismatch")
                self.released = True
                self.last_seq = max(self.last_seq, seq)
            elif verb == "begin":
                if self.operation is not None or seq != 0:
                    raise ValueError("station_operation_already_used")
                self.operation = op, holder
                self.released = False
                self.last_seq = seq
            else:
                if verb != "hold" or self.operation != (op, holder) or self.released or seq != self.last_seq + 1:
                    raise ValueError("inactive_or_stale_hold")
                self.last_seq = seq
        # The core durably tombstones early release and fences in-flight writes.
        # Never hold the HTTP ownership lock while waiting on device I/O.
        if verb == "release":
            return self.core.release(op, holder)
        if verb == "begin":
            try:
                return self.core.begin(op, holder, authorized=True, profile_id=PROFILE)
            except Exception:
                with self.claims:
                    self.released = True
                raise
        return self.core.heartbeat(op, holder, seq, authorized=True, held=True)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def log_message(self, *_):
        pass

    def send(self, status, payload, *, html=False, cookie=None, nonce=None):
        body = payload if html else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ("text/html" if html else "application/json")+"; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'nonce-"+(nonce or "-")+
                         "'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        if cookie:
            self.send_header("Set-Cookie", f"edge_attended={cookie}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get_all("Host") == [self.server.host]

    def cookie(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            return cookies["edge_attended"].value if "edge_attended" in cookies else ""
        except ValueError:
            return ""

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {"reason":"unknown_origin"})
        if self.path == "/":
            try:
                token = self.server.login(self.cookie())
            except ValueError:
                return self.send(429, {"reason":"operator_session_limit"})
            nonce = secrets.token_urlsafe(24)
            html = self.server.page.replace(b"<script>", f'<script nonce="{nonce}">'.encode())
            return self.send(200, html, html=True, cookie=token, nonce=nonce)
        if self.path == "/state" and self.cookie() in self.server.holders:
            return self.send(200, self.server.view())
        self.send(404, {"reason":"unavailable"})

    def do_POST(self):
        allowed = (self.valid_host() and self.headers.get_all("Origin") == ["http://"+self.server.host]
                   and self.headers.get_content_type() == "application/json"
                   and self.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin")
        holder = self.server.holders.get(self.cookie())
        if not allowed or holder is None:
            return self.send(403, {"reason":"operator_session_required"})
        if self.path not in ("/begin", "/hold", "/release"):
            return self.send(404, {"reason":"unknown_operation"})
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if not 1 <= size <= 512 or self.headers.get("Transfer-Encoding"):
                raise ValueError()
            args = json.loads(self.rfile.read(size))
            op, seq = args["operation_id"], args["seq"]
            if (set(args) != {"operation_id", "seq"} or type(seq) is not int or not 0 <= seq <= 65535 or
                    not isinstance(op, str) or str(uuid.UUID(op)) != op):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            return self.send(400, {"reason":"invalid_request"})
        try:
            result = self.server.perform(self.path[1:], op, holder, seq)
        except (KeyError, ValueError, RuntimeError) as error:
            return self.send(HTTPStatus.CONFLICT, {"reason":str(error)[:180]})
        self.send(200, self.server.view(result))


def main():
    import fcntl
    from .lingao_attended import AttendedJog
    from .lingao_attended_io import LingaoAttendedIO
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--operator-port", type=int, default=8926)
    parser.add_argument("--robot-id", required=True)
    args = parser.parse_args()
    if not 1024 <= args.operator_port <= 65535:
        parser.error("invalid operator port")
    os.umask(0o077)
    folder = args.directory
    folder.mkdir(mode=0o700, parents=False, exist_ok=True)
    attrs = folder.lstat()
    if not stat.S_ISDIR(attrs.st_mode) or attrs.st_uid != os.getuid() or attrs.st_mode & 0o077:
        raise ValueError("private operator directory required")
    socket_path = folder/"station.sock"
    owner = os.open(folder/"owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        existing = socket_path.lstat()
    except FileNotFoundError:
        existing = None
    if existing:
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
            raise ValueError("unexpected operator socket")
        socket_path.unlink()
    page = (Path(__file__).parent/"station"/"attended-trial.html").read_bytes()
    io = LingaoAttendedIO()
    core = AttendedJog(folder/"trials.sqlite", args.robot_id, io)
    try:
        # Observe the installed arbitration path. Motion commissioning requires a
        # deployment with a robot-side, expiring grant accepted by that path.
        io.start()
        with Station(socket_path, args.operator_port, core, page) as server:
            def stop(signum, frame):
                threading.Thread(target=server.shutdown, daemon=True).start()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            core.start_watchdog()
            print("Attended station listening on private SSH socket", flush=True)
            server.serve_forever(poll_interval=.1)
    finally:
        try:
            core.close()
        finally:
            try:
                io.close()
            finally:
                socket_path.unlink(missing_ok=True)
                os.close(owner)


if __name__ == "__main__":
    main()
