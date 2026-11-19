"""Serve public task facts from the read-only owner volume, without starting the Agent."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

if __package__:
    from .kernel import task_facts
else:
    import _contract_path
    from kernel import task_facts


class FactsHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        target = urlsplit(self.path)
        if target.path == "/agent/tasks":
            status, value = task_facts.detail_request(target.query, self.server.task_db)
        else:
            status, value = task_facts.read_request(target.path, target.query, self.server.task_db, self.server.registry_db)
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def method_not_allowed(self):
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = method_not_allowed


def make_server(address, task_dir):
    server = ThreadingHTTPServer(address, FactsHandler)
    server.task_db = Path(task_dir) / "tasks.db"
    server.registry_db = Path(task_dir) / "module-events.sqlite"
    return server


def check_handoff(task_dir):
    """The deployment guard permits an absent DB or the existing set of completed states."""
    path = Path(task_dir) / "tasks.db"
    if path.is_symlink():
        raise ValueError("unexpected task ledger link")
    # An initial absence is permitted. Once a ledger is seen, losing it while
    # reading is an error; a rotation cannot turn a failed check into success.
    try:
        path.stat()
    except FileNotFoundError:
        return
    with task_facts.read_db(path) as db:
        unfinished = db.execute("""SELECT 1 FROM tasks WHERE state IS NULL OR state NOT IN
                                    ('BLOCKED','SUCCEEDED','CANCELED') LIMIT 1""").fetchone()
        if unfinished:
            raise ValueError("reconcile existing tasks before switching profile")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-handoff", action="store_true", help="check the deployment task guard and exit")
    args = parser.parse_args()
    task_dir = Path(os.getenv("EDGE_TASK_DIR", "/var/lib/edge-tasks"))
    if args.check_handoff:
        try:
            check_handoff(task_dir)
        except (OSError, sqlite3.Error, ValueError) as error:
            parser.exit(1, str(error) + "\n")
        return
    address = (os.getenv("EDGE_FACTS_BIND", "127.0.0.1"), int(os.getenv("EDGE_FACTS_PORT", "8091")))
    with make_server(address, task_dir) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
