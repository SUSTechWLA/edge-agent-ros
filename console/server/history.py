"""Durable, read-only-to-clients console event log. Source cursors survive restarts."""
import json
import os
import sqlite3
import threading
import time
import zlib
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds")


def encode(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def pack(obj):
    return zlib.compress(encode(obj).encode(), 3)


# Only an unassociated, informational periodic observation is a pruning candidate.
ORDINARY_SAMPLE = """kind='telemetry.sample' AND source='edge_console' AND level='info'
    AND mission_id IS NULL AND operation_id IS NULL AND cause_seq IS NULL AND source_id IS NULL"""
EVENT_COLUMNS = ("seq,utc,observed_at,kind,level,source,session_id,mission_id,operation_id,"
                 "detail,data,cause_seq,source_id,source_seq")


def retention_setting(name, default):
    value = int(os.environ.get(name, str(default)))
    if not 0 <= value <= 2**31-1:
        raise ValueError(f"{name} must be a nonnegative integer below 2^31")
    return value


class History:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.days = retention_setting("EDGE_TELEMETRY_RETENTION_DAYS", 7)
        self.max_samples = retention_setting("EDGE_TELEMETRY_MAX_SAMPLES", 10080)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, utc TEXT NOT NULL, observed_at REAL NOT NULL,
              kind TEXT NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL, session_id TEXT NOT NULL,
              mission_id TEXT, operation_id TEXT, detail TEXT NOT NULL, data TEXT NOT NULL,
              cause_seq INTEGER, source_id TEXT, source_seq INTEGER, snapshot BLOB NOT NULL,
              UNIQUE(source_id,source_seq)
            );
            CREATE INDEX IF NOT EXISTS events_mission ON events(mission_id,seq);
            CREATE INDEX IF NOT EXISTS events_kind ON events(kind,seq);
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id,seq);
            CREATE INDEX IF NOT EXISTS events_cause ON events(cause_seq) WHERE cause_seq IS NOT NULL;
        """)
        self.db.execute(f"CREATE INDEX IF NOT EXISTS ordinary_by_seq ON events(seq) WHERE {ORDINARY_SAMPLE}")
        self.db.execute(f"CREATE INDEX IF NOT EXISTS ordinary_by_age ON events(observed_at) WHERE {ORDINARY_SAMPLE}")
        with self.db:
            self.prune(time.time())
        self.earliest, self.cursor = self.db.execute("SELECT coalesce(min(seq),0),coalesce(max(seq),0) FROM events").fetchone()

    def prune(self, now):
        # Keep any sample referenced by an audit event, including those from older
        # releases. SQLite automatically reuses the freed pages without a VACUUM.
        eligible = f"""{ORDINARY_SAMPLE} AND NOT EXISTS
            (SELECT 1 FROM events AS child WHERE child.cause_seq=events.seq)"""
        if self.days:
            self.db.execute(f"DELETE FROM events WHERE observed_at<? AND {eligible}",
                            (now - self.days * 86400,))
        if self.max_samples:
            cutoff = self.db.execute(f"""SELECT seq FROM events WHERE {ORDINARY_SAMPLE}
                ORDER BY seq DESC LIMIT 1 OFFSET ?""", (self.max_samples,)).fetchone()
            if cutoff:
                self.db.execute(f"DELETE FROM events WHERE seq<=? AND {eligible}", (cutoff[0],))

    def retention(self):
        with self.lock:
            samples = self.db.execute(f"SELECT count(*) FROM events WHERE {ORDINARY_SAMPLE}").fetchone()[0]
            total = self.db.execute("SELECT count(*) FROM events").fetchone()[0]
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            allocated = self.db.execute("PRAGMA page_count").fetchone()[0] * page_size
            reusable = self.db.execute("PRAGMA freelist_count").fetchone()[0] * page_size
            return {"retention_days":self.days, "max_samples":self.max_samples, "samples":samples,
                    "events":total, "earliest_cursor":self.earliest, "latest_cursor":self.cursor,
                    "database_bytes":allocated, "reusable_bytes":reusable}

    def source_cursor(self, source_id):
        with self.lock:
            return self.db.execute("SELECT coalesce(max(source_seq),0) FROM events WHERE source_id=?",
                                   (source_id,)).fetchone()[0]

    def record(self, snapshot, *, kind, source, detail, level="info", mission_id=None,
               operation_id=None, data=None, event_utc=None, source_id=None,
               source_seq=None, source_cause=None, cause=None):
        with self.lock, self.db:
            if source_cause is not None:
                row = self.db.execute("SELECT seq FROM events WHERE source_id=? AND source_seq=?",
                                      (source_id, source_cause)).fetchone()
                cause = row[0] if row else None
            at = time.time()
            insert = self.db.execute("""
                INSERT INTO events(utc,observed_at,kind,level,source,session_id,mission_id,operation_id,
                  detail,data,cause_seq,source_id,source_seq,snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (event_utc or utc(at), at, kind, level, source, snapshot["session_id"], mission_id,
                  operation_id, detail, encode(data or {}), cause, source_id, source_seq, pack(snapshot)))
            self.cursor = insert.lastrowid
            if kind == "telemetry.sample":
                self.prune(at)
                self.earliest = self.db.execute("SELECT coalesce(min(seq),0) FROM events").fetchone()[0]
            elif not self.earliest:
                self.earliest = self.cursor
            return self.cursor

    @staticmethod
    def public(row):
        doc = dict(row)
        doc["data"] = json.loads(doc["data"])
        return doc

    def replay(self, seq):
        with self.lock:
            row = self.db.execute("SELECT snapshot FROM events WHERE seq=?", (seq,)).fetchone()
        if row is None:
            return None
        result = json.loads(zlib.decompress(row[0]))
        result.setdefault("extra_sources", {})
        result.update(mode="replay", cursor=seq)
        return result

    def selection(self, after, before, filters, maximum=None):
        values = []
        where = []
        for key in ("mission_id","kind","session_id","operation_id","level"):
            if key in filters:
                where.append(key+"=?")
                values.append(filters[key])
        if "task_id" in filters:
            where.append("""(mission_id=? OR mission_id IN
              (SELECT operation_id FROM events WHERE source='mission_agent/tasks' AND mission_id=?
               AND operation_id IS NOT NULL))""")
            values.extend([filters["task_id"]]*2)
        for cond, val in (("seq>?", after), ("seq<?", before), ("seq<=?",maximum)):
            if val is not None:
                where.append(cond)
                values.append(val)
        return (" WHERE " + " AND ".join(where) if where else ""), values

    def page(self, after, before, limit, filters, maximum=None):
        selection, values = self.selection(after,before,filters,maximum)
        sql = f"SELECT {EVENT_COLUMNS} FROM events" + selection
        sql += " ORDER BY seq " + ("ASC" if after is not None else "DESC") + " LIMIT ?"
        with self.lock:
            rows = self.db.execute(sql, [*values,limit+1]).fetchall()
            cursor = self.cursor
        return {"events":[self.public(r) for r in rows[:limit]], "has_more":len(rows)>limit,
                "cursor":cursor}

    def export(self, filters, include_snapshot=False):
        # A separate WAL reader pins one consistent view across streamed rows,
        # even if the writer prunes a periodic sample while the client downloads.
        selection, values = self.selection(None,None,filters)
        columns = EVENT_COLUMNS + (",snapshot" if include_snapshot else "")
        with closing(sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            for item in reader.execute(f"SELECT {columns} FROM events"+selection+" ORDER BY seq",values):
                row = self.public(item)
                row["schema_version"] = 1
                if include_snapshot:
                    snapshot = json.loads(zlib.decompress(row["snapshot"]))
                    snapshot.setdefault("extra_sources", {})
                    snapshot.update(mode="replay",cursor=row["seq"])
                    row["snapshot"] = snapshot
                yield (encode(row)+"\n").encode("utf-8")

    def close(self):
        with self.lock:
            self.db.close()

    def previous_task_event(self, source_id, task_id):
        with self.lock:
            row = self.db.execute("SELECT max(seq) FROM events WHERE source_id=? AND mission_id=?",
                                  (source_id,task_id)).fetchone()
            return row[0]

    def dispatch_origin(self, source_id, delegated_id):
        with self.lock:
            row=self.db.execute("""SELECT seq,mission_id FROM events
                WHERE source_id=? AND operation_id=? AND kind='agent.dispatch'
                ORDER BY source_seq LIMIT 1""", (source_id,delegated_id)).fetchone()
            return dict(row) if row else None


def ledger_batch(path, history):
    """Read one consistent view; a new core ledger starts a separate source sequence."""
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=0.15)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        source_id = (db.execute("SELECT id FROM audit_identity WHERE singleton=1").fetchone()[0]
                     if "audit_identity" in tables else f"legacy:{path.stat().st_dev}:{path.stat().st_ino}")
        events = []
        if "mission_events" in tables:
            last = history.source_cursor(source_id)
            events = [dict(r) for r in db.execute(
                "SELECT * FROM mission_events WHERE seq>? ORDER BY seq LIMIT 256", (last,))]
        missions = [dict(r) for r in db.execute("""
          SELECT mission_id,state,detail FROM missions ORDER BY
            (state IN ('RUNNING','CANCELING','RECOVERY_REQUIRED')) DESC, rowid DESC LIMIT 64
        """)]
        observed = {m["mission_id"] for m in missions} | {e["mission_id"] for e in events}
        steps = {}
        if observed:
            placeholders = ",".join("?" for _ in observed)
            for row in db.execute(f"SELECT * FROM steps WHERE mission_id IN ({placeholders}) ORDER BY step",
                                  tuple(observed)):
                steps.setdefault(row["mission_id"], []).append(dict(row))
        for mission in missions:
            mission["steps"] = steps.get(mission["mission_id"], [])
        return source_id, missions, events, steps


def task_batch(path, history):
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=0.15)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        identity = db.execute("SELECT id FROM audit_identity WHERE singleton=1").fetchone()[0]
        source_id = "agent:"+identity
        last = history.source_cursor(source_id)
        events = db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 256",(last,))
        return source_id, [dict(row) for row in events]
