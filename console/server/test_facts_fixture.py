"""Serve the Agent's production read queries over loopback without starting ROS workers."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

sys.path.append(str(Path(__file__).resolve().parents[2]))
from agent.kernel.task_facts import read_request


class ReadHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        request = urlsplit(self.path)
        status, result = read_request(request.path, request.query, self.server.task_db, self.server.registry_db)
        body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextmanager
def agent_facts_server(task_db, registry_db=None):
    with ThreadingHTTPServer(("127.0.0.1", 0), ReadHandler) as server:
        server.task_db = Path(task_db)
        server.registry_db = Path(registry_db or server.task_db.with_name("module-events.sqlite"))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval":0.02})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()
