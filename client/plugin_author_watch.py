"""Observe blocked planner tasks and write proposed odometry tools to a private workspace."""
import argparse
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

if __package__:
    from .event_sink import Sink
else:
    from event_sink import Sink

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import author_tool


NETWORK_ERRORS = (URLError, ConnectionError, TimeoutError, IncompleteRead)


class Watch:
    def __init__(self, url, ollama_url, model, out_dir):
        self.url = url.rstrip("/")
        self.ollama_url = ollama_url
        self.model = model
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        # A separate database holds a process-lifetime lock without blocking Sink transactions.
        self.lock = sqlite3.connect(self.out / "consumer.lock.sqlite", timeout=0)
        try:
            self.lock.execute("BEGIN IMMEDIATE")
            self.sink = Sink(self.out / "watch.sqlite", self.url, {"kind":"agent.blocked"})
            self.sink.db.execute("""CREATE TABLE IF NOT EXISTS author_work(
                archive_id TEXT, seq INTEGER, finished INTEGER, attempts INTEGER, due REAL,
                detail TEXT, PRIMARY KEY(archive_id,seq))""")
            self.sink.db.execute("CREATE INDEX IF NOT EXISTS author_due ON author_work(finished,due)")
            self.sink.db.execute("CREATE TABLE IF NOT EXISTS intake(last_row INTEGER NOT NULL)")
            with self.sink.db:
                if not self.sink.db.execute("SELECT 1 FROM intake").fetchone():
                    self.sink.db.execute("INSERT INTO intake VALUES(0)")
        except BaseException:
            self.lock.close()
            raise

    def close(self):
        try:
            self.sink.close()
        finally:
            self.lock.close()

    def fetch_task(self, key):
        query = urlencode({"id":key, "limit":500})
        try:
            with urlopen(self.url + "/agent/tasks?" + query, timeout=10) as response:
                raw = response.read(2_000_001)
        except HTTPError as exc:
            if exc.code == 404:
                raise ValueError("source task no longer exists") from exc
            raise
        if len(raw) > 2_000_000:
            raise ValueError("source task exceeds the author input limit")
        return json.loads(raw)

    def decide(self, archive, seq, event):
        source = {"archive_id":archive, "seq":seq, "event_id":event.get("event_id"),
                  "trace_id":event.get("trace_id"), "task_id":event.get("task_id"), "reason_code":event.get("code")}
        identity = [source[k] for k in ("task_id","trace_id","event_id")]
        eligible = (event.get("kind") == "agent.blocked" and event.get("source") == "mission_agent/tasks"
                    and event.get("operation_id") is None and all(isinstance(k,str) and k for k in identity)
                    and source["reason_code"] in ("planner.capability_missing","planner.not_configured"))
        # The receipt belongs to a source archive row. A projection with a matching task ID
        # cannot claim a later native event's receipt. The tool ID follows the task across imports.
        token = hashlib.sha256(author_tool.json_bytes([archive,seq])).hexdigest()
        path = self.out / "decisions" / (token + ".json")
        if path.exists():
            # Reconcile a crash after atomic file replacement and before the SQLite commit.
            previous = author_tool.read_json(path, 2_000_000)
            if previous.get("status") not in ("proposed","declined"):
                raise RuntimeError("unreadable previous decision: " + str(path))
            return "recovered", token
        result = {"source":source}
        try:
            if not eligible:
                raise ValueError("archive event is not an eligible pre-dispatch planner failure")
            task = self.fetch_task(source["task_id"])
            if (not isinstance(task,dict) or task.get("id") != source["task_id"]
                    or task.get("trace_id") != source["trace_id"] or not task.get("events")):
                raise ValueError("task or trace does not match the archived failure")
            last = json.loads(task["events"][-1]["payload"])
            if (last["trace"]["event_id"] != source["event_id"] or last["diagnostic"]["code"] != source["reason_code"]):
                raise ValueError("current task's terminal diagnosis differs from the archive")
            # Raw task data belongs on the container's tmpfs, outside the persistent proposals volume.
            with tempfile.TemporaryDirectory() as temp:
                task_file, record = Path(temp)/"task.json", Path(temp)/"model.json"
                task_file.write_bytes(author_tool.json_bytes(task))
                tool_key = hashlib.sha256(author_tool.json_bytes(identity)).hexdigest()[:32]
                args = argparse.Namespace(id="derived.task_"+tool_key, from_task=task_file, request=None,
                                          ollama_url=self.ollama_url, model=self.model, record=record)
                try:
                    result["definition"] = author_tool.draft(args)
                finally:
                    if record.exists():
                        result["model"] = author_tool.read_json(record, 262144)
            result["status"] = "proposed"
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            result.update(status="declined", reason=str(exc)[:1024])
        author_tool.write_atomic(path, result)
        return result["status"], token

    def poll(self):
        more = self.sink.copy()
        db = self.sink.db
        with db:
            incoming = db.execute("""SELECT rowid,archive_id,seq FROM events
                WHERE rowid>(SELECT last_row FROM intake) ORDER BY rowid LIMIT 32""").fetchall()
            if incoming:
                db.executemany("INSERT OR IGNORE INTO author_work VALUES(?,?,0,0,0,NULL)",
                               [(archive, seq) for _, archive, seq in incoming])
                db.execute("UPDATE intake SET last_row=?", (incoming[-1][0],))
        rows = db.execute("""SELECT e.archive_id,e.seq,e.event,w.attempts FROM author_work w
            JOIN events e USING(archive_id,seq) WHERE w.finished=0 AND w.due<=?
            ORDER BY w.due, w.rowid LIMIT 32""", (time.time(),)).fetchall()
        counts = dict(proposed=0, declined=0, recovered=0, deferred=0)
        for archive, seq, raw, attempts in rows:
            try:
                status, detail = self.decide(archive, seq, json.loads(raw))
                finished, due = 1, 0
            except NETWORK_ERRORS as exc:
                # Schedule this event independently; a failing endpoint does not block later tasks.
                status, finished = "deferred", 0
                detail = type(exc).__name__
                if isinstance(exc, HTTPError):
                    detail += f" HTTP {exc.code}"
                due = time.time() + min(60, 2 ** min(attempts,6))
            with self.sink.db:
                self.sink.db.execute("INSERT OR REPLACE INTO author_work VALUES(?,?,?,?,?,?)",
                                     (archive,seq,finished,attempts+1,due,detail))
            counts[status] += 1
        return counts, more or len(incoming)==32 or len(rows)==32


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="read-only Edge console HTTP base URL")
    parser.add_argument("--ollama-url", required=True, help="Ollama base URL")
    parser.add_argument("--model", default=os.getenv("EDGE_OLLAMA_MODEL","qwen2.5:1.5b"))
    parser.add_argument("--out-dir", type=Path, required=True, help="private proposal and cursor directory")
    parser.add_argument("--once", action="store_true", help="import one page and process up to 32 due events, then exit")
    args = parser.parse_args()
    watch = Watch(args.url,args.ollama_url,args.model,args.out_dir)
    backoff = 1
    try:
        while True:
            try:
                counts, more = watch.poll()
                if args.once or any(counts.values()):
                    print(json.dumps(counts), flush=True)
                if args.once:
                    return 2 if counts["deferred"] else 0
                backoff = 1
                if not more:
                    time.sleep(1)
            except (NETWORK_ERRORS + (json.JSONDecodeError, UnicodeError)) as exc:
                if args.once:
                    raise
                print(f"archive unavailable ({type(exc).__name__}); retry in {backoff}s", file=sys.stderr, flush=True)
                time.sleep(backoff)
                backoff = min(60, backoff*2)
    except KeyboardInterrupt:
        return 0
    finally:
        watch.close()


if __name__ == "__main__":
    sys.exit(main())
