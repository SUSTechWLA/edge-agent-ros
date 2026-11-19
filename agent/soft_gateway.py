"""Fixed GET-only public facts and legacy read-provider routes for the isolated soft network."""

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
from socketserver import ThreadingMixIn, UnixStreamServer
import sys
import threading
from urllib.parse import parse_qsl, urlsplit


FACTS = {"/api/session", "/api/events", "/api/task-context", "/api/event-stats"}
EVIDENCE = {"read_session", "query_evidence", "evidence_schema"}
LIMIT = 1024 * 1024


def route(port, method, target):
    url = urlsplit(target)
    if url.scheme or url.netloc or url.fragment or len(target) > 8192:
        return None
    if port == 8129 and method == "GET" and url.path in FACTS:
        return "console", 8080
    if port == 8126 and ((method, target) in {("GET", "/health"), ("POST", "/session")}):
        return "soft-session", 8126
    if port == 8125:
        if method == "GET" and target in ("/health", "/schema"):
            return "soft-evidence", 8125
        query = parse_qsl(url.query)
        if method == "POST" and url.path == "/invoke" and len(query) == 1 and query[0][0] == "tool" and query[0][1] in EVIDENCE:
            return "soft-evidence", 8125
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def forward(self):
        upstream = route(self.server.server_port, self.command, self.path)
        if upstream is None:
            return self.send_error(403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 <= length <= LIMIT:
                return self.send_error(400)
            self.connection.settimeout(10)
            data = self.rfile.read(length) if length else None
            if data is not None and len(data) != length:
                return self.send_error(400)
            if upstream[0]=="console" and getattr(self.server,"board_port",None):
                conn=HTTPConnection("127.0.0.1",self.server.board_port,timeout=9)
            elif upstream[0]=="console" and os.environ.get("EDGE_SOFT_FACT_SOCKET"):
                conn=UnixHTTPConnection(os.environ["EDGE_SOFT_FACT_SOCKET"])
            else:
                conn = HTTPConnection(*upstream, timeout=9)
            try:
                conn.request(self.command, self.path, body=data, headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                payload = response.read(LIMIT + 1)
                if len(payload) > LIMIT or 300 <= response.status < 400:
                    return self.send_error(502)
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                conn.close()
        except (ValueError, OSError, EOFError):
            self.send_error(502)

    do_GET = forward
    do_POST = forward


class UnixHTTPConnection(HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost",timeout=9)
        self.path=path

    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class FactsSocket(ThreadingMixIn,UnixStreamServer):
    daemon_threads=True
    server_port=8129


if __name__ == "__main__" and sys.argv[1:]==["--board-facts"]:
    path=Path("/facts/console.sock")
    path.unlink(missing_ok=True)
    with FactsSocket(str(path),Handler) as server:
        path.chmod(0o660)
        server.board_port=int(os.environ["EDGE_CONSOLE_PORT"])
        server.serve_forever()
elif __name__ == "__main__":
    servers = [ThreadingHTTPServer(("0.0.0.0", port), Handler) for port in (8125, 8126, 8129)]
    for server in servers[:-1]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    servers[-1].serve_forever()
