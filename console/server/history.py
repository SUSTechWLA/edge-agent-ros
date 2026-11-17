"""Durable, read-only-to-clients console event log. Source cursors survive restarts."""
import json
import os
import sqlite3
import threading
import time
import uuid
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
TRACE_FIELDS = ("event_id","trace_id","parent_event_id","task_id","module","layer","code","call_id",
                "trigger_kind","trigger_id","trigger_source")
EVENT_COLUMNS = ("seq,utc,observed_at,kind,level,source,session_id,mission_id,operation_id,"
                 "detail,data,cause_seq,source_id,source_seq," + ",".join(TRACE_FIELDS))


class EventConflict(ValueError):
    """A module reused a published event ID with different content."""


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
            CREATE TABLE IF NOT EXISTS archive_identity (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL, pruned_through INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO archive_identity VALUES(1,lower(hex(randomblob(16))),0);
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
        # Older records retain their original timestamps and identities. Leave
        # unavailable provenance NULL instead of backfilling a guessed trigger.
        with self.db:
            existing = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
            for name in (*TRACE_FIELDS,"ingress_hash"):
                if name not in existing:
                    self.db.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT")
            for name in ("event_id","trace_id","parent_event_id","task_id","module","code","trigger_kind","trigger_id","call_id","operation_id"):
                self.db.execute(f"CREATE INDEX IF NOT EXISTS trace_{name} ON events({name},seq) WHERE {name} IS NOT NULL")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS wire_event_id ON events(event_id) WHERE ingress_hash IS NOT NULL")
        self.db.execute(f"CREATE INDEX IF NOT EXISTS ordinary_by_seq ON events(seq) WHERE {ORDINARY_SAMPLE}")
        self.db.execute(f"CREATE INDEX IF NOT EXISTS ordinary_by_age ON events(observed_at) WHERE {ORDINARY_SAMPLE}")
        self.archive_id,self.pruned_through=self.db.execute("SELECT id,pruned_through FROM archive_identity").fetchone()
        with self.db:
            self.prune(time.time())
        self.earliest, self.cursor = self.db.execute("SELECT coalesce(min(seq),0),coalesce(max(seq),0) FROM events").fetchone()

    def prune(self, now):
        # Keep any sample referenced by an audit event, including those from older
        # releases. SQLite automatically reuses the freed pages without a VACUUM.
        eligible = f"""{ORDINARY_SAMPLE} AND NOT EXISTS
            (SELECT 1 FROM events AS child WHERE child.cause_seq=events.seq)"""
        def remove(condition,value):
            rows=self.db.execute(f"DELETE FROM events WHERE {condition} AND {eligible} RETURNING seq",(value,))
            cut=max((row[0] for row in rows),default=0)
            if cut>self.pruned_through:
                self.db.execute("UPDATE archive_identity SET pruned_through=? WHERE singleton=1",(cut,))
                self.pruned_through=cut
        if self.days:
            remove("observed_at<?",now-self.days*86400)
        if self.max_samples:
            cutoff = self.db.execute(f"""SELECT seq FROM events WHERE {ORDINARY_SAMPLE}
                ORDER BY seq DESC LIMIT 1 OFFSET ?""", (self.max_samples,)).fetchone()
            if cutoff:
                remove("seq<=?",cutoff[0])

    def retention(self):
        with self.lock:
            samples = self.db.execute(f"SELECT count(*) FROM events WHERE {ORDINARY_SAMPLE}").fetchone()[0]
            total = self.db.execute("SELECT count(*) FROM events").fetchone()[0]
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            allocated = self.db.execute("PRAGMA page_count").fetchone()[0] * page_size
            reusable = self.db.execute("PRAGMA freelist_count").fetchone()[0] * page_size
            return {"archive_id":self.archive_id,"pruned_through_seq":self.pruned_through,
                    "retention_days":self.days, "max_samples":self.max_samples, "samples":samples,
                    "events":total, "earliest_cursor":self.earliest, "latest_cursor":self.cursor,
                    "database_bytes":allocated, "reusable_bytes":reusable}

    def source_cursor(self, source_id):
        with self.lock:
            return self.db.execute("SELECT coalesce(max(source_seq),0) FROM events WHERE source_id=?",
                                   (source_id,)).fetchone()[0]

    def record(self, snapshot, *, kind, source, detail, level="info", mission_id=None,
               operation_id=None, data=None, event_utc=None, source_id=None,
               source_seq=None, source_cause=None, cause=None, trace=None, ingress_hash=None):
        with self.lock, self.db:
            trace = dict(trace or {})
            event_id = trace.get("event_id") or uuid.uuid4().hex
            if ingress_hash:
                seen = self.db.execute("SELECT seq,ingress_hash FROM events WHERE event_id=? AND ingress_hash IS NOT NULL",
                                       (event_id,)).fetchone()
                if seen:
                    if seen["ingress_hash"]!=ingress_hash:
                        raise EventConflict("module event ID was reused with different content")
                    return seen["seq"]
            if source_cause is not None:
                row = self.db.execute("SELECT seq FROM events WHERE source_id=? AND source_seq=?",
                                      (source_id, source_cause)).fetchone()
                cause = row[0] if row else None
            parent_id = trace.get("parent_event_id")
            if parent_id:
                if ingress_hash and self.db.execute("SELECT 1 FROM events WHERE parent_event_id=? LIMIT 1",
                                                      (event_id,)).fetchone():
                    cycle=self.db.execute("""WITH RECURSIVE parents(id) AS (
                        SELECT ? UNION SELECT e.parent_event_id FROM events e JOIN parents p ON p.id=e.event_id
                        WHERE e.parent_event_id IS NOT NULL)
                        SELECT 1 FROM parents WHERE id=? LIMIT 1""",(parent_id,event_id)).fetchone()
                    if cycle:
                        raise ValueError("producer reported a cyclic event cause")
                parent=self.db.execute("SELECT seq FROM events WHERE event_id=? ORDER BY seq LIMIT 1",(parent_id,)).fetchone()
                cause=parent["seq"] if parent else None
            elif cause:
                parent=self.db.execute("SELECT event_id FROM events WHERE seq=?",(cause,)).fetchone()
                parent_id = parent["event_id"] if parent else None
            trigger = trace.get("trigger") or {}
            metadata = (event_id,trace.get("trace_id"),parent_id,trace.get("task_id"),trace.get("module") or source,
                        trace.get("layer"),trace.get("code") or kind,trace.get("call_id"),
                        trigger.get("kind"),trigger.get("id"),trigger.get("source"))
            at = time.time()
            insert = self.db.execute("""
                INSERT INTO events(utc,observed_at,kind,level,source,session_id,mission_id,operation_id,
                  detail,data,cause_seq,source_id,source_seq,snapshot,ingress_hash,
                  event_id,trace_id,parent_event_id,task_id,module,layer,code,call_id,trigger_kind,trigger_id,trigger_source)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (event_utc or utc(at), at, kind, level, source, snapshot["session_id"], mission_id,
                  operation_id, detail, encode(data or {}), cause, source_id, source_seq, pack(snapshot),ingress_hash,*metadata))
            self.cursor = insert.lastrowid
            self.db.execute("UPDATE events SET cause_seq=? WHERE parent_event_id=? AND cause_seq IS NULL AND seq!=?",
                            (self.cursor,event_id,self.cursor))
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
            row = self.db.execute(f"SELECT {EVENT_COLUMNS}, snapshot FROM events WHERE seq=?", (seq,)).fetchone()
        if row is None:
            return None
        event = self.public(row)
        result = json.loads(zlib.decompress(event.pop("snapshot")))
        result.setdefault("extra_sources", {})
        result.update(mode="replay", cursor=seq,event=event)
        return result

    def selection(self, after, before, filters, maximum=None):
        values = []
        where = []
        for key in ("mission_id","kind","session_id","operation_id","level","source",*(f for f in TRACE_FIELDS if f not in ("task_id","trace_id"))):
            if key in filters:
                where.append(key+"=?")
                values.append(filters[key])
        if "task_id" in filters:
            where.append("""(task_id=? OR mission_id=? OR mission_id IN
              (SELECT operation_id FROM events WHERE source='mission_agent/tasks' AND mission_id=?
               AND operation_id IS NOT NULL))""")
            values.extend([filters["task_id"]]*3)
        if "trace_id" in filters:
            # A ROS action server may only know the delegated mission ID. Join
            # it to the agent's original dispatch without rewriting that event.
            where.append("""(trace_id=? OR mission_id IN
                 (SELECT operation_id FROM events WHERE trace_id=? AND source='mission_agent/tasks'
                  AND kind='agent.dispatch' AND operation_id IS NOT NULL))""")
            values.extend([filters["trace_id"]]*2)
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
            cursor,pruned = self.cursor,self.pruned_through
        return {"events":[self.public(r) for r in rows[:limit]], "has_more":len(rows)>limit,
                "cursor":cursor,"archive_id":self.archive_id,"pruned_through_seq":pruned}

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
                    snapshot = json.loads(zlib.decompress(row.pop("snapshot")))
                    snapshot.setdefault("extra_sources", {})
                    snapshot.update(mode="replay",cursor=row["seq"],event=dict(row))
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
            row=self.db.execute("""SELECT seq,mission_id,trace_id,trigger_kind,trigger_id,trigger_source FROM events
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
