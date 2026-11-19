"""Durable producer of public v1 module events. The caller owns the transaction."""
import hashlib
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The console and producers share the same validation, normalization and redaction.
sys.path.append(str(Path(__file__).resolve().parents[1] / "console" / "server"))
from module_events import parse_event


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class ModuleJournal:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=1, check_same_thread=False)
        try:
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS module_bus_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO module_bus_meta VALUES('source_id',lower(hex(randomblob(16))));
                CREATE TABLE IF NOT EXISTS module_bus_events(
                    seq INTEGER PRIMARY KEY, event TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE,
                    event_key TEXT UNIQUE, key_digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS module_bus_heartbeat(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), pulse TEXT NOT NULL, ttl_s INTEGER NOT NULL);
            """)
            self.source_id = self.db.execute("SELECT value FROM module_bus_meta WHERE key='source_id'").fetchone()[0]
        except BaseException:
            self.db.close()
            raise

    def emit(self, event, *, key=None):
        """Append a fact without committing; a key identifies one logical producer intent."""
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 256):
            raise ValueError("module event key must contain 1 to 256 characters")
        envelope = dict(event)
        envelope.setdefault("schema_version", 1)
        envelope.setdefault("event_id", uuid.uuid4().hex)
        envelope.setdefault("utc", datetime.now(timezone.utc).isoformat())
        clean, _ = parse_event(canonical(envelope))
        intent = {name:value for name,value in clean.items() if name not in ("event_id", "utc")}
        digest = hashlib.sha256(canonical(intent).encode()).hexdigest()
        body = canonical(clean)
        stored_key = hashlib.sha256(key.encode()).hexdigest() if key is not None else None
        inserted = self.db.execute("""INSERT INTO module_bus_events(event,event_id,event_key,key_digest)
            VALUES(?,?,?,?) ON CONFLICT DO NOTHING""", (body, clean["event_id"], stored_key, digest))
        if inserted.rowcount:
            return clean["event_id"]
        existing = self.db.execute("""SELECT event_id,key_digest,event_key,event FROM module_bus_events
            WHERE event_id=? OR event_key=?""", (clean["event_id"], stored_key)).fetchall()
        if (len(existing) != 1 or existing[0][1:3] != (digest, stored_key)
                or "event_id" in event and event["event_id"] != existing[0][0]
                or "utc" in event and clean["utc"] != json.loads(existing[0][3])["utc"]):
            raise ValueError("module event ID or key was reused for a different intent")
        return existing[0][0]

    def heartbeat(self, ttl_s):
        """Renew one producer lease in the caller's transaction, independently of event volume."""
        if type(ttl_s) is not int or not 2 <= ttl_s <= 3600:
            raise ValueError("heartbeat TTL must be an integer in [2,3600]")
        self.db.execute("INSERT OR REPLACE INTO module_bus_heartbeat VALUES(1,?,?)", (uuid.uuid4().hex, ttl_s))

    def close(self):
        self.db.close()
