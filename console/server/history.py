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
            CREATE TABLE IF NOT EXISTS observer_fact_sources (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), task_source TEXT NOT NULL, core_source TEXT
            );
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
            for name in (*TRACE_FIELDS,"ingress_hash","peer_source"):
                if name not in existing:
                    self.db.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT")
            for name in ("event_id","trace_id","parent_event_id","task_id","module","code","trigger_kind","trigger_id","call_id","operation_id"):
                self.db.execute(f"CREATE INDEX IF NOT EXISTS trace_{name} ON events({name},seq) WHERE {name} IS NOT NULL")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS wire_event_id ON events(event_id) WHERE ingress_hash IS NOT NULL")
            self.db.execute("""CREATE INDEX IF NOT EXISTS core_mission_scope ON events(source_id,mission_id,seq)
                WHERE source='edge_core/ledger' AND peer_source IS NOT NULL""")
        self.db.execute(f"CREATE INDEX IF NOT EXISTS ordinary_by_seq ON events(seq) WHERE {ORDINARY_SAMPLE}")
        self.db.execute(f"CREATE INDEX IF NOT EXISTS ordinary_by_age ON events(observed_at) WHERE {ORDINARY_SAMPLE}")
        self.archive_id,self.pruned_through=self.db.execute("SELECT id,pruned_through FROM archive_identity").fetchone()
        with self.db:
            self.prune(time.time())
        self.earliest, self.cursor = self.db.execute("SELECT coalesce(min(seq),0),coalesce(max(seq),0) FROM events").fetchone()
        # A restart may expire the newest sample, including the last retained row.
        self.cursor = max(self.cursor, self.pruned_through)

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

    def fact_sources(self):
        with self.lock:
            row = self.db.execute("SELECT task_source,core_source FROM observer_fact_sources WHERE singleton=1").fetchone()
            if row:
                return tuple(row)
            old = self.db.execute("""SELECT source_id FROM events WHERE source='mission_agent/tasks'
                AND source_id IS NOT NULL ORDER BY seq DESC LIMIT 1""").fetchone()
            return (old[0] if old else None), None

    def remember_sources(self, task_source, core_source):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO observer_fact_sources VALUES(1,?,?)", (task_source, core_source))

    def journal_health(self):
        """Restore observer-owned transitions once when opening an archive."""
        with self.lock:
            rows = self.db.execute("""
                SELECT data,event_id FROM events WHERE seq IN (
                    SELECT max(seq) FROM events
                    WHERE source='edge_console' AND module='journal.ingress'
                    AND kind IN ('module.journal_available','module.journal_unavailable')
                    GROUP BY trace_id)
            """).fetchall()
        result = {}
        for row in rows:
            data = json.loads(row["data"])
            result[data["journal"]] = (data["status"], row["event_id"])
        return result

    def record(self, snapshot, *, kind, source, detail, level="info", mission_id=None,
               operation_id=None, data=None, event_utc=None, source_id=None,
               source_seq=None, source_cause=None, cause=None, trace=None, ingress_hash=None, peer_source=None):
        with self.lock, self.db:
            if source == "edge_core/ledger" and peer_source is not None:
                # A replacement Agent cannot take over a mission already witnessed by this core.
                first = self.db.execute("""SELECT peer_source FROM events WHERE source='edge_core/ledger'
                    AND source_id=? AND mission_id=? AND peer_source IS NOT NULL ORDER BY seq LIMIT 1""",
                    (source_id, mission_id)).fetchone()
                if first:
                    peer_source = first[0]
            trace = dict(trace or {})
            event_id = trace.get("event_id") or uuid.uuid4().hex
            if ingress_hash:
                seen = self.db.execute("SELECT seq,ingress_hash,source,source_id,source_seq FROM events WHERE event_id=? ORDER BY seq LIMIT 1",
                                       (event_id,)).fetchone()
                if seen:
                    if (seen["ingress_hash"] != ingress_hash or seen["source"] != source
                            or seen["source_id"] != source_id or seen["source_seq"] != source_seq):
                        raise EventConflict("module event ID was reused with different content or source")
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
                  detail,data,cause_seq,source_id,source_seq,snapshot,ingress_hash,peer_source,
                  event_id,trace_id,parent_event_id,task_id,module,layer,code,call_id,trigger_kind,trigger_id,trigger_source)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (event_utc or utc(at), at, kind, level, source, snapshot["session_id"], mission_id,
                  operation_id, detail, encode(data or {}), cause, source_id, source_seq, pack(snapshot),ingress_hash,peer_source,*metadata))
            self.cursor = insert.lastrowid
            if peer_source and source == "mission_agent/tasks" and kind == "agent.dispatch" and operation_id:
                self._link_dispatch(source_id, peer_source, operation_id)
            elif peer_source and source == "edge_core/ledger":
                self._link_dispatch(peer_source, source_id, mission_id, core_seq=self.cursor)
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
        for prefix in filters.get("exclude_kind_prefixes", []):
            where.append("substr(kind,1,?) != ?")
            values.extend((len(prefix),prefix))
        if filters.get("view")=="changes":
            # Explicit task/call/cause/trigger links carry diagnostic meaning,
            # even when no mission ID is available. This is a view predicate,
            # not a change to the independent archive retention policy.
            # event_id, module and code are assigned to ordinary samples too.
            where.append(f"""NOT ({ORDINARY_SAMPLE}
                AND task_id IS NULL AND trace_id IS NULL AND parent_event_id IS NULL
                AND call_id IS NULL AND trigger_kind IS NULL AND trigger_id IS NULL
                AND trigger_source IS NULL)""")
        elif filters.get("view")=="issues":
            where.append("""(level IN ('warn','error') OR kind IN
                ('agent.blocked','agent.error','agent.timeout','agent.retry_exhausted','agent.steps_quarantined'))""")
        for key in ("mission_id","kind","session_id","operation_id","level","source",*(f for f in TRACE_FIELDS if f not in ("task_id","trace_id"))):
            if key in filters:
                where.append(key+"=?")
                values.append(filters[key])
        # Earlier archives and ROS observations support a query-time operation lookup.
        # Newly ingested core rows have an explicit peer scope, including "" for unknown.
        operation_lookup = "(source!='edge_core/ledger' OR peer_source IS NULL)"
        if "task_id" in filters:
            where.append(f"""(task_id=? OR mission_id=? OR ({operation_lookup} AND mission_id IN
              (SELECT operation_id FROM events WHERE source='mission_agent/tasks' AND mission_id=?
               AND operation_id IS NOT NULL)))""")
            values.extend([filters["task_id"]]*3)
        if "trace_id" in filters:
            # A ROS action server may only know the delegated mission ID. Join
            # it to the agent's original dispatch without rewriting that event.
            where.append(f"""(trace_id=? OR ({operation_lookup} AND mission_id IN
                 (SELECT operation_id FROM events WHERE trace_id=? AND source='mission_agent/tasks'
                  AND kind='agent.dispatch' AND operation_id IS NOT NULL)))""")
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

    def stats(self, filters, before=None):
        """Public counts and the cutoff are observed together with retention state."""
        with self.lock:
            cursor = self.cursor
            where, values = self.selection(None,before,filters,cursor)
            row = self.db.execute("SELECT count(*),min(seq),max(seq),"
                "coalesce(sum(length(CAST(data AS BLOB))+length(CAST(detail AS BLOB))),0) FROM events"
                +where,values).fetchone()
            return {"archive_id":self.archive_id,"cursor":cursor,"pruned_through_seq":self.pruned_through,
                    "count":row[0],"first_seq":row[1],"last_seq":row[2],"public_payload_bytes":row[3]}

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

    def _link_dispatch(self, agent_source, core_source, mission, core_seq=None):
        """Join exact producer evidence atomically, whichever side was observed first."""
        candidates = self.db.execute("""SELECT seq,event_id,mission_id,trace_id,trigger_kind,trigger_id,trigger_source,
                peer_source FROM events WHERE source='mission_agent/tasks' AND kind='agent.dispatch'
                AND source_id=? AND operation_id=? AND (peer_source=? OR peer_source IS NULL)
                ORDER BY source_seq""", (agent_source, mission, core_source))
        for dispatch in candidates:
            if dispatch["peer_source"] is None:
                # Pre-migration dispatches can prove the core identity via their stored snapshot
                # or an existing creation which explicitly named this dispatch.
                snapshot = self.db.execute("SELECT snapshot FROM events WHERE seq=?", (dispatch["seq"],)).fetchone()[0]
                if json.loads(zlib.decompress(snapshot)).get("ledger_id") != core_source:
                    witness = self.db.execute("""SELECT 1 FROM events WHERE source='edge_core/ledger'
                        AND source_id=? AND mission_id=? AND kind='mission.created' AND task_id=?
                        AND json_extract(data,'$.agent_dispatch_seq')=? LIMIT 1""",
                        (core_source, mission, dispatch["mission_id"], dispatch["seq"])).fetchone()
                    if not witness:
                        continue
            break
        else:
            return
        scope = """source='edge_core/ledger' AND source_id=? AND mission_id=? AND peer_source=? AND task_id IS NULL"""
        binding = (core_source, mission, agent_source)
        if core_seq is not None:
            scope += " AND seq=?"
            binding += (core_seq,)
        # Keep a producer's own cause even when its referenced event has not arrived.
        root = """cause_seq IS NULL AND parent_event_id IS NULL AND json_extract(data,'$.source_cause_seq') IS NULL"""
        self.db.execute(f"""UPDATE events SET data=json_set(data,'$.parent_task_id',?,'$.agent_dispatch_seq',?),
            cause_seq=CASE WHEN {root} THEN ? ELSE cause_seq END,
            parent_event_id=CASE WHEN {root} THEN ? ELSE parent_event_id END
            WHERE {scope} AND kind='mission.created'""", (dispatch["mission_id"], dispatch["seq"],
            dispatch["seq"], dispatch["event_id"], *binding))
        self.db.execute(f"""UPDATE events SET task_id=?, trace_id=?, trigger_kind=?, trigger_id=?, trigger_source=?
            WHERE {scope}""", (dispatch["mission_id"], dispatch["trace_id"], dispatch["trigger_kind"],
            dispatch["trigger_id"], dispatch["trigger_source"], *binding))
