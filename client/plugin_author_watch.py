"""Observe blocked planner tasks and write proposed odometry tools to a private workspace."""
import argparse
from datetime import datetime, timezone
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
from module_journal import ModuleJournal


NETWORK_ERRORS = (URLError, ConnectionError, TimeoutError, IncompleteRead)
REVIEW_ACTIONS = {
    "source_ineligible":"Inspect the original event source and planner diagnosis.",
    "source_unavailable":"Restore access to the original task journal and inspect its terminal event.",
    "source_mismatch":"Match the original task ID, trace and terminal planner event before drafting.",
    "proposal_rejected":"Review the requested measurement and available sensors; supply a suitable provider if needed.",
    "receipt_invalid":"Inspect the preserved private receipt and its originating archive row.",
    "declined":"Review the original diagnosis and private decision before choosing a provider.",
}


class InvalidReceipt(Exception):
    """A durable receipt already exists but cannot safely identify a decision."""


def decision_event(archive, seq, event, receipt_id, status, code, data, utc):
    # Native and imported rows can predate the v1 wire identifiers.
    def identifier(field):
        value = event.get(field)
        return value if author_tool.public_reference(value) else None

    archive_id = archive if author_tool.public_reference(archive) else hashlib.sha256(archive.encode()).hexdigest()
    return {"schema_version":1, "module":"plugin.author", "kind":"plugin.author."+status,
            "code":"author."+code, "level":"info" if status=="proposed" else "warn",
            "trace_id":identifier("trace_id") or receipt_id, "parent_event_id":identifier("event_id"),
            "task_id":identifier("task_id"), "layer":"author",
            "data":{"archive_id":archive_id, "source_seq":seq, "receipt_id":receipt_id, **data},
            **({"utc":utc} if utc else {})}


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
            self.sink.db.execute("CREATE TABLE IF NOT EXISTS author_outbox(event_key TEXT PRIMARY KEY,event TEXT NOT NULL)")
            with self.sink.db:
                if not self.sink.db.execute("SELECT 1 FROM intake").fetchone():
                    self.sink.db.execute("INSERT INTO intake VALUES(0)")
            path = os.environ.get("EDGE_AUTHOR_EVENT_DB") or self.out / "module-events.sqlite"
            self.journal = ModuleJournal(path)
        except BaseException:
            if hasattr(self, "sink"):
                self.sink.close()
            self.lock.close()
            raise

    def close(self):
        try:
            self.sink.close()
        finally:
            try:
                self.journal.close()
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

    def decide(self, archive, seq, event, token):
        source = {"archive_id":archive, "seq":seq, "event_id":event.get("event_id"),
                  "trace_id":event.get("trace_id"), "task_id":event.get("task_id"), "reason_code":event.get("code")}
        identity = [source[k] for k in ("task_id","trace_id","event_id")]
        eligible = (event.get("kind") == "agent.blocked" and event.get("source") == "mission_agent/tasks"
                    and event.get("operation_id") is None and all(author_tool.public_reference(k) for k in identity)
                    and source["reason_code"] in ("planner.capability_missing","planner.not_configured"))
        # The receipt belongs to a source archive row. A projection with a matching task ID
        # cannot claim a later native event's receipt. The tool ID follows the task across imports.
        path = self.out / "decisions" / (token + ".json")
        if path.exists():
            # Reconcile a crash after atomic file replacement and before the SQLite commit.
            try:
                previous = author_tool.read_json(path, 2_000_000)
                if (not isinstance(previous,dict) or previous.get("status") not in ("proposed","declined")
                        or previous.get("source") != source or previous.get("reason_code") not in
                        (None,"source_ineligible","source_unavailable","source_mismatch","proposal_rejected")):
                    raise ValueError("durable receipt no longer matches its source")
                if previous["status"] == "proposed":
                    author_tool.capability(previous["definition"])
                if "utc" in previous and datetime.fromisoformat(previous["utc"]).tzinfo is None:
                    raise ValueError("decision timestamp has no timezone")
            except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
                raise InvalidReceipt from exc
            return "recovered", previous
        result = {"source":source}
        reason_code = "source_ineligible"
        try:
            if not eligible:
                raise ValueError("archive event is not an eligible pre-dispatch planner failure")
            reason_code = "source_unavailable"
            task = self.fetch_task(source["task_id"])
            reason_code = "source_mismatch"
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
                reason_code = "proposal_rejected"
                try:
                    result["definition"] = author_tool.draft(args)
                finally:
                    if record.exists():
                        result["model"] = author_tool.read_json(record, 262144)
            result["status"] = "proposed"
        except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError) as exc:
            result.update(status="declined", reason=str(exc)[:1024], reason_code=reason_code)
        result["utc"] = datetime.now(timezone.utc).isoformat()
        result["decision_schema"] = 2
        author_tool.write_atomic(path, result)
        return result["status"], result

    def flush_outbox(self):
        db = self.sink.db
        for key, raw in db.execute("SELECT event_key,event FROM author_outbox ORDER BY rowid").fetchall():
            with self.journal.db:
                self.journal.emit(json.loads(raw), key=key)
            with db:
                db.execute("DELETE FROM author_outbox WHERE event_key=?", (key,))

    def poll(self):
        self.flush_outbox()
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
            event = json.loads(raw)
            receipt_id = hashlib.sha256(author_tool.json_bytes([archive,seq])).hexdigest()
            try:
                status, receipt = self.decide(archive, seq, event, receipt_id)
                new_receipt = receipt.get("decision_schema")==2
                detail = receipt_id
                finished, due = 1, 0
                outcome = receipt["status"]
                utc = receipt.get("utc")  # Legacy local receipts have no known decision time.
                code = receipt.get("reason_code", outcome)
                data = {"receipt_sha256":hashlib.sha256(author_tool.json_bytes(receipt)).hexdigest()}
                if outcome=="proposed":
                    candidate = author_tool.capability(receipt["definition"])
                    data.update(tool_id=candidate["id"], candidate_version=candidate["version"])
                key = receipt_id + ".decision"
            except InvalidReceipt:
                new_receipt = False
                # Preserve the conflicting private file for inspection. Never recreate the
                # proposal with another model call or hide a later healthy work item behind it.
                status = outcome = "declined"
                finished, due = 1, 0
                code = detail = "receipt_invalid"
                key = receipt_id + ".invalid"
                data, utc = {}, None
            except NETWORK_ERRORS as exc:
                # Schedule this event independently; a failing endpoint does not block later tasks.
                status, finished = "deferred", 0
                detail = type(exc).__name__
                if isinstance(exc, HTTPError):
                    detail += f" HTTP {exc.code}"
                retry_seconds = min(60, 2 ** min(attempts,6))
                due = time.time() + retry_seconds
                outcome = status
                utc = datetime.now(timezone.utc).isoformat()
                code = "dependency_unavailable"
                data = {"attempt":attempts+1,
                        "retry_seconds":retry_seconds, "error_type":type(exc).__name__}
                if isinstance(exc, HTTPError):
                    data["http_status"] = exc.code
                key = f"{receipt_id}.deferred.{attempts+1}"
            public = decision_event(archive, seq, event, receipt_id, outcome, code, data, utc)
            if outcome=="declined" and new_receipt:
                public["data"]["action"] = REVIEW_ACTIONS.get(code, REVIEW_ACTIONS["declined"])
            with self.sink.db:
                # Persist the exact public evidence with the work checkpoint. Independent journals
                # cannot share a transaction; replay the small outbox using the same key after a crash.
                self.sink.db.execute("INSERT OR REPLACE INTO author_outbox VALUES(?,?)",
                                     (key,json.dumps(public, ensure_ascii=False)))
                if outcome=="declined" and not new_receipt:
                    # Historical keyed outcomes are immutable. Attach guidance as a second
                    # fact when an old or unreadable receipt requires operator intervention.
                    advisory = {**public,"kind":"plugin.author.review_required",
                                "data":{**public["data"],"action":REVIEW_ACTIONS.get(code,REVIEW_ACTIONS["declined"])}}
                    self.sink.db.execute("INSERT OR REPLACE INTO author_outbox VALUES(?,?)",
                                         (key+".review",json.dumps(advisory,ensure_ascii=False)))
                self.sink.db.execute("INSERT OR REPLACE INTO author_work VALUES(?,?,?,?,?,?)",
                                     (archive,seq,finished,attempts+1,due,detail))
            counts[status] += 1
        self.flush_outbox()
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
