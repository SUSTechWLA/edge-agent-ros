"""Import trusted, local producer journals into the existing read-only event archive."""
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from history import EventConflict
from module_events import parse_event


MODULE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
IDENTITY = re.compile(r"[A-Za-z0-9_.-]{1,96}\Z")


@dataclass
class Lease:
    source_id: str
    pulse: str
    deadline: float
    overdue: bool
    previous_event: str | None


class JournalIngress:
    def __init__(self, builtin, extensions, clock=time.monotonic):
        self.builtin = builtin
        self.extensions = Path(extensions)
        self.health = {}
        self.clock = clock
        self.leases = {}

    def poll(self, history, frame):
        sources = dict(self.builtin)
        try:
            for path in self.extensions.iterdir():
                if MODULE.fullmatch(path.name) and path.name not in sources and path.is_dir() and not path.is_symlink():
                    sources[path.name] = path / "module-events.sqlite"
            self.status(history, frame, "@sources", "available")
        except FileNotFoundError:
            self.status(history, frame, "@sources", "absent")
        except OSError:
            self.status(history, frame, "@sources", "unreadable")
        for module in self.health.keys() - sources.keys() - {"@sources"}:
            self.status(history, frame, module, "absent")
        for module, path in sources.items():
            try:
                if path.is_symlink():
                    raise ValueError("expected a journal file on its assigned mount")
                path.stat()
                self.read(history, frame, module, path)
            except FileNotFoundError:
                self.status(history, frame, module, "absent")
            except (OSError, sqlite3.Error, ValueError) as error:
                self.status(history, frame, module, type(error).__name__)
            else:
                self.status(history, frame, module, "available")

    def status(self, history, frame, module, state):
        previous = self.health.get(module)
        if previous == state or previous is None and state == "absent":
            return
        if previous is not None or state != "available":
            good = state == "available"
            history.record(frame(None), kind="module.journal_available" if good else "module.journal_unavailable",
                           source="edge_console", level="info" if good else "warn",
                           detail="模块事件源恢复" if good else "模块事件源无法读取",
                           data={"journal":module, "status":state, "previous":previous})
        self.health[module] = state

    def read(self, history, frame, module, path):
        # Short read transactions keep the producer independent of snapshot compression / archive writes.
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.05)) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT value FROM module_bus_meta WHERE key='source_id'").fetchone()
            if not row or not isinstance(row[0],str) or not IDENTITY.fullmatch(row[0]):
                raise ValueError("invalid source identity")
            source = "journal:" + module
            source_id = source + ":" + row[0]
            cursor = history.source_cursor(source_id)
            rows = db.execute("""SELECT seq, substr(CAST(event AS BLOB),1,65537) FROM module_bus_events
                                 WHERE seq>? ORDER BY seq LIMIT 32""", (cursor,)).fetchall()
            heartbeat = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='module_bus_heartbeat'").fetchone():
                heartbeat = db.execute("SELECT pulse,ttl_s FROM module_bus_heartbeat WHERE singleton=1").fetchone()
        for seq, raw in rows:
            try:
                if not isinstance(raw,bytes) or len(raw)>65536:
                    raise ValueError("invalid event size or encoding")
                event, digest = parse_event(raw.decode("utf-8"))
                if event["module"] != module or not event.get("utc"):
                    raise ValueError("journal ownership or source time mismatch")
                history.record(frame(event["utc"]), kind=event["kind"], source=source,
                               detail=event.get("detail",""), level=event.get("level","info"),
                               mission_id=event.get("mission_id"), operation_id=event.get("operation_id"),
                               data=event.get("data",{}), event_utc=event["utc"], source_id=source_id,
                               source_seq=seq, trace=event, ingress_hash=digest)
            except (ValueError, OverflowError, RecursionError) as exc:
                # Acknowledge this row with a durable diagnostic in the same unique source position.
                # The untrusted payload remains at its owner. It may contain credentials.
                kind = "module.event_conflict" if isinstance(exc,EventConflict) else "module.event_invalid"
                history.record(frame(None), kind=kind, source="edge_console", level="error",
                               detail="模块事件未通过归档校验", source_id=source_id, source_seq=seq,
                               data={"journal":module, "source_seq":seq, "reason":type(exc).__name__})
        if heartbeat:
            self.observe_heartbeat(history, frame, module, source_id, *heartbeat)

    def observe_heartbeat(self, history, frame, module, source_id, pulse, ttl):
        if type(ttl) is not int or not 2 <= ttl <= 3600 or not isinstance(pulse,str) or not IDENTITY.fullmatch(pulse):
            raise ValueError("invalid heartbeat")
        now = self.clock()
        lease = self.leases.get(module)
        if not lease or lease.source_id != source_id:
            # A stopped Console has no monotonic clock history. Grant one observation window;
            # a previously reported timeout stays open until an actual new pulse appears.
            latest = history.page(None,None,1,{"trace_id":source_id,"source":"edge_console",
                                               "module":"journal.watchdog"})["events"]
            last = latest[0] if latest else {}
            overdue = last.get("kind") == "module.heartbeat_overdue"
            lease = Lease(source_id, last["data"]["pulse"] if overdue else pulse, now + ttl,
                          overdue, last.get("event_id"))
            self.leases[module] = lease
        renewed = lease.pulse != pulse
        if renewed and not lease.overdue:
            lease.pulse, lease.deadline = pulse, now + ttl
        overdue = not renewed and now >= lease.deadline
        if not ((lease.overdue and renewed) or (overdue and not lease.overdue)):
            return
        event_id = uuid.uuid4().hex
        kind = "module.heartbeat_overdue" if overdue else "module.heartbeat_restored"
        history.record(frame(None), kind=kind, source="edge_console", level="warn" if overdue else "info",
                       detail="模块心跳超时" if overdue else "已观察到新的模块心跳",
                       trace={"event_id":event_id, "parent_event_id":lease.previous_event,
                              "trace_id":source_id, "module":"journal.watchdog", "code":kind},
                       data={"journal":module, "pulse":pulse, "ttl_s":ttl})
        lease.previous_event, lease.overdue = event_id, overdue
        lease.pulse, lease.deadline = pulse, now + ttl
