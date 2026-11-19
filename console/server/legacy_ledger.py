"""Deprecated read-only adapter for the execution core mission ledger (SDD-049)."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3


class LegacyLedgerReader:
    def __init__(self):
        self.path = Path(os.getenv("EDGE_LEDGER_PATH", "/var/lib/edge-agent/edge.db")).absolute()

    def batch(self, history):
        """Read one consistent view; a new core ledger starts a separate source sequence."""
        path = self.path
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
