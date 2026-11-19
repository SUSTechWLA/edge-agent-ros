"""Local journals advance while the Console is waiting for either Agent facts route."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from urllib.parse import urlencode, urlsplit
from urllib.request import build_opener, ProxyHandler

from agent.module_journal import ModuleJournal


class JournalWorkersTest(unittest.TestCase):
    def test_local_commit_before_owner_answers_either_read(self):
        # A response is held until the event written during *that request* is visible.
        # This exercises the production pollers and local HTTP/SQLite paths together.
        module = "probe.workers"
        routes = ("/agent/task-events", "/agent/registry-events")
        checked = {route:Event() for route in routes}
        seen = {}
        local = build_opener(ProxyHandler({}))
        with tempfile.TemporaryDirectory() as temp, socket.socket() as port:
            folder = Path(temp)
            port.bind(("127.0.0.1", 0))
            http_port = port.getsockname()[1]
            port.close()
            sources = folder / "sources"
            class Owner(BaseHTTPRequestHandler):
                def do_GET(self):
                    route = urlsplit(self.path).path
                    if route in checked and not checked[route].is_set():
                        event_id = "during-" + route.rsplit("/",1)[1]
                        with closing(ModuleJournal(sources/module/"module-events.sqlite")) as journal, journal.db:
                            journal.emit({"event_id":event_id, "trace_id":"workers", "module":module,
                                          "kind":"probe.sample", "code":"probe.sample"})
                        query = urlencode({"event_id":event_id})
                        until = time.monotonic() + 1.2  # AgentFacts' socket timeout is 1.5 seconds.
                        while time.monotonic() < until:
                            try:
                                with local.open(f"http://127.0.0.1:{http_port}/api/events?{query}", timeout=.2) as reply:
                                    rows = json.load(reply)["events"]
                                if [row["event_id"] for row in rows] == [event_id]:
                                    seen[route] = True
                                    break
                            except OSError:
                                pass
                            time.sleep(.025)
                        checked[route].set()
                    self.send_response(503)
                    self.send_header("Content-Type","application/json")
                    self.send_header("Content-Length","2")
                    self.end_headers()
                    self.wfile.write(b"{}")

                def log_message(self, *args):
                    pass
            with ThreadingHTTPServer(("127.0.0.1",0), Owner) as api, (folder/"console.log").open("wb") as log:
                thread = Thread(target=api.serve_forever)
                thread.start()
                env = dict(os.environ, EDGE_HTTP_BIND="127.0.0.1", EDGE_HTTP_PORT=str(http_port),
                           EDGE_MODULE_SOURCES=str(sources), EDGE_JOURNAL_SOURCES="{}",
                           EDGE_CONSOLE_DIR=str(folder/"console"), EDGE_LEDGER_PATH=str(folder/"missing"),
                           EDGE_FACTS_URL=f"http://127.0.0.1:{api.server_port}")
                proc = subprocess.Popen([sys.executable,"-B",str(Path(__file__).with_name("main.py"))],
                                        env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    for route in routes:
                        self.assertTrue(checked[route].wait(12), (route, (folder/"console.log").read_text()[-500:]))
                    self.assertEqual(seen, {route:True for route in routes})
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=4)
                    api.shutdown()
                    thread.join()


if __name__ == "__main__":
    unittest.main()
