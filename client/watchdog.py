"""Independent single-edge health supervision. Only operator-configured, fixed GET endpoints."""
import argparse
from contextlib import closing
import hashlib
import http.client
import json
import math
from pathlib import Path
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from module_journal import ModuleJournal, canonical


PATHS = {"agent": ("/agent/capabilities", "/agent/plugins"),
         "console": ("/api/event-stats?kind=watchdog.detected", "/api/history")}
STEPS = {
    "console": ["查看 Console 容器状态和日志，核对监听地址、归档卷权限与磁盘空间。",
                "恢复原归档卷并分别读取 /api/event-stats 与 /api/history，核对归档身份和游标。"],
    "agent": ["查看 Agent 容器状态和日志，核对端口、插件清单及任务账本挂载。",
              "修复后分别读取 /agent/capabilities 与 /agent/plugins；按原操作 ID 核查已有任务。"],
}
MAX_REPLY = 256 * 1024
MAX_SQLITE_INT = 2**63 - 1


class ProbeError(Exception):
    """Contains only a stable, public error code."""


def origin(value):
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/")
            or parts.username is not None or parts.password is not None or parts.query or parts.fragment):
        raise ValueError("expected an HTTP origin without path or credentials")
    return parts.hostname, parts.port, parts.scheme


class Reader:
    def __init__(self, urls, timeout):
        self.origins = {t:origin(urls[t]) for t in PATHS}
        self.timeout = timeout

    def __call__(self, target, reconcile, operation=None):
        data = self.request(target, PATHS[target][int(reconcile)], operation)
        try:
            return summarize(target, reconcile, data)
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise ProbeError("invalid_reply") from exc

    def page(self, after, cutoff, operation):
        # 'before' is exclusive. At SQLite's largest possible sequence there cannot be a later row.
        before = f"&before={cutoff+1}" if cutoff < MAX_SQLITE_INT else ""
        path = f"/api/events?kind=agent.blocked&source=mission_agent%2Ftasks&after={after}{before}&limit=16"
        return self.request("console", path, operation)

    def request(self, target, path, operation):
        host, port, scheme = self.origins[target]
        connection = (http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection)(
            host, port, timeout=self.timeout)
        try:
            connection.connect()
            sock = connection.sock
            def abort():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            # Bound the complete response as well as individual socket reads (slow streaming peers).
            timer = threading.Timer(self.timeout, abort)
            timer.daemon = True
            timer.start()
            try:
                headers = {"X-Edge-Operation-Id":operation} if operation else {}
                connection.request("GET", path, headers=headers)
                with connection.getresponse() as response:
                    if response.status != 200:
                        raise ProbeError(f"http_{response.status}")
                    raw = response.read(MAX_REPLY+1)
                    if response.length and len(raw) <= MAX_REPLY:
                        raise ProbeError("incomplete_reply")
                if not timer.is_alive():
                    raise ProbeError("timeout")
            finally:
                timer.cancel()
        except (TimeoutError, socket.timeout) as exc:
            raise ProbeError("timeout") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ProbeError("connection_unavailable") from exc
        finally:
            connection.close()
        if len(raw) > MAX_REPLY:
            raise ProbeError("reply_too_large")
        try:
            return json.loads(raw)
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise ProbeError("invalid_reply") from exc


def natural(value):
    if type(value) is not int or not 0 <= value <= MAX_SQLITE_INT:
        raise ValueError("expected a nonnegative SQLite integer")
    return value


def summarize(target, reconcile, doc):
    if target == "console":
        archive = doc["archive_id"]
        if not isinstance(archive,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", archive):
            raise ValueError("invalid archive identity")
        cursor = natural(doc["latest_cursor" if reconcile else "cursor"])
        pruned = natural(doc["pruned_through_seq"])
        if pruned > cursor:
            raise ValueError("invalid retention watermark")
        return {"archive_id":archive, "cursor":cursor, "pruned_through_seq":pruned,
                "count":natural(doc["events" if reconcile else "count"])}
    rows = doc["plugins" if reconcile else "capabilities"]
    if not isinstance(rows,list) or len(rows)>4096 or not reconcile and not rows:
        raise ValueError("invalid directory")
    if reconcile:
        counts = dict(ready=0, blocked=0, draining=0, invalid=0)
        for row in rows:
            counts[row["status"]] += 1
        return counts
    signatures = []
    for item in rows:
        if (not isinstance(item["id"],str) or not item["id"] or not isinstance(item["version"],str)
                or not item["version"] or item["effect"] not in ("read","idempotent")):
            raise ValueError("invalid capability")
        signatures.append((item["id"],item["version"],item["effect"]))
    return {"capability_count":len(rows), "catalog_sha256":hashlib.sha256(canonical(sorted(signatures)).encode()).hexdigest()}


def read_safely(reader, name, reconcile=False, operation=None):
    try:
        return reader(name, reconcile, operation), None
    except ProbeError as exc:
        return None, str(exc)


class Watchdog:
    def __init__(self, path, reader, *, threshold=3, grace=10, attempts=3, recheck=60, ttl=60, clock=time.monotonic):
        self.reader, self.threshold, self.budget = reader, threshold, attempts
        self.clock, self.start_at, self.recheck, self.ttl = clock, clock()+grace, recheck, ttl
        self.next_check = {}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # An independent process-lifetime lock prevents two supervisors sharing one incident stream.
        self.lock = sqlite3.connect(path.parent/"watchdog.lock.sqlite", timeout=0)
        try:
            self.lock.execute("BEGIN IMMEDIATE")
            self.journal = ModuleJournal(path)
        except BaseException:
            self.lock.close()
            raise
        self.db = self.journal.db
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS watchdog_targets(target TEXT PRIMARY KEY, record TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS watchdog_cursor(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                            "archive_id TEXT NOT NULL, seq INTEGER NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS watchdog_events(archive_id TEXT,seq INTEGER,event_id TEXT,"
                            "task_id TEXT,trace_id TEXT,code TEXT,PRIMARY KEY(archive_id,seq),UNIQUE(archive_id,event_id))")
        self.boot = uuid.uuid4().hex
        with self.db:
            self.journal.emit({"module":"watchdog","kind":"watchdog.started","code":"watchdog.started",
                               "trace_id":self.boot,"data":{"components":list(PATHS)}})

    def close(self):
        try:
            with self.db:
                self.journal.emit({"module":"watchdog","kind":"watchdog.stopped","code":"watchdog.stopped",
                                   "trace_id":self.boot})
        finally:
            self.journal.close()
            self.lock.close()

    def load(self, target):
        row = self.db.execute("SELECT record FROM watchdog_targets WHERE target=?", (target,)).fetchone()
        return json.loads(row[0]) if row else {"failures":0,"baseline":None,"incident":None,"view":None}

    def save(self, target, state):
        self.db.execute("INSERT OR REPLACE INTO watchdog_targets VALUES(?,?)",(target,canonical(state)))

    def emit(self, target, incident, kind, level="info", **data):
        incident["head"] = self.journal.emit({"module":"watchdog","kind":"watchdog."+kind,"code":"watchdog."+kind,
                   "level":level, "trace_id":incident["id"], "operation_id":incident["operation"],
                   "parent_event_id":incident["head"], "data":{"target":target,**data}})

    def tick(self):
        with self.db:
            self.journal.heartbeat(self.ttl)
        for target in PATHS:
            probe, error = read_safely(self.reader, target)
            self.observe(target, probe, error)

    def sync(self, probe, state):
        """Incrementally rebuild this consumer's local mirror and checkpoint one bounded page atomically."""
        row = self.db.execute("SELECT archive_id,seq FROM watchdog_cursor WHERE singleton=1").fetchone()
        archive = probe["archive_id"]
        incident = state["incident"]
        if not row and not incident:
            with self.db:
                self.db.execute("INSERT INTO watchdog_cursor VALUES(1,?,?)",(archive,probe["cursor"]))
                self.journal.emit({"module":"watchdog","trace_id":self.boot,"kind":"watchdog.subscription_started",
                                   "code":"watchdog.subscription_started","data":{"archive_id":archive,"cursor":probe["cursor"]}})
            return True
        changed = bool(row and row[0] != archive)
        after = row[1] if row and not changed else 0
        gap = probe["pruned_through_seq"] > after
        if (changed or gap) and not incident:
            raise ProbeError("archive_changed" if changed else "retention_crossed")
        if probe["cursor"] < after:
            raise ProbeError("cursor_regressed")
        if incident:
            repair = incident.get("repair")
            if not repair or repair["archive_id"] != archive:
                repair = {"archive_id":archive,"from_seq":after,"through_seq":after,"cutoff":probe["cursor"],
                          "rows_copied":0,"rows_reused":0,"pages":0,"archive_changed":changed,"retention_crossed":gap}
                incident["repair"] = repair
            cutoff = repair["cutoff"]
        else:
            cutoff = probe["cursor"]
        if after >= cutoff and row and not changed:
            return True
        page = self.reader.page(after,cutoff,incident["operation"] if incident else None)
        rows = []
        try:
            if (not isinstance(page,dict) or page["archive_id"] != archive or natural(page["cursor"]) < cutoff
                    or type(page["has_more"]) is not bool or not isinstance(page["events"],list)
                    or len(page["events"]) > 16 or page["has_more"] and not page["events"]):
                raise ValueError("invalid page")
            gap = gap or natural(page["pruned_through_seq"]) > after
            if gap and not incident:
                raise ProbeError("retention_crossed")
            previous = after
            for item in page["events"]:
                seq = natural(item["seq"])
                fields = [item[k] for k in ("event_id","task_id","trace_id","code")]
                if (item["kind"]!="agent.blocked" or item["source"]!="mission_agent/tasks" or not previous < seq <= cutoff
                        or not all(isinstance(v,str) and re.fullmatch(r"[\w./:@-]{1,240}",v) for v in fields)):
                    raise ValueError("row outside requested scope")
                rows.append((archive,seq,*fields))
                previous = seq
            position = previous if page["has_more"] else cutoff
        except (ValueError,TypeError,KeyError) as exc:
            raise ProbeError("mirror_invalid_page") from exc
        try:
            with self.db:
                copied = 0
                for record in rows:
                    stored = self.db.execute("INSERT OR IGNORE INTO watchdog_events VALUES(?,?,?,?,?,?)",record)
                    if stored.rowcount:
                        copied += 1
                    else:
                        previous = self.db.execute("SELECT * FROM watchdog_events WHERE archive_id=? AND seq=?",
                                                   record[:2]).fetchone()
                        if previous != record:
                            raise ProbeError("mirror_event_conflict")
                if rows:
                    self.db.execute("DELETE FROM watchdog_events WHERE rowid <= "
                                    "(SELECT rowid FROM watchdog_events ORDER BY rowid DESC LIMIT 1 OFFSET 4096)")
                self.db.execute("INSERT OR REPLACE INTO watchdog_cursor VALUES(1,?,?)",(archive,position))
                if incident:
                    repair["rows_copied"] += copied
                    repair["rows_reused"] = repair.get("rows_reused",0) + len(rows)-copied
                    repair["through_seq"], repair["pages"] = position, repair["pages"]+1
                    repair["retention_crossed"] |= gap
                    if (repair["retention_crossed"] or changed) and not incident.get("coverage_reported"):
                        incident["coverage_reported"] = True
                        self.emit("console",incident,"intervention_required","warn",
                                  reason="archive_changed" if changed else "retention_crossed",
                                  steps=["核对旧归档备份和源任务账本，确认受影响区间中的缺样。"],
                                  previous_archive=row[0] if row else None, coverage=repair)
                    self.save("console",state)
        except sqlite3.IntegrityError as exc:
            raise ProbeError("mirror_event_conflict") from exc
        return not page["has_more"]

    def observe(self, target, probe, error):
        s = self.load(target)
        incident = s["incident"]
        baseline = s["baseline"] or {}
        if target == "console" and probe and baseline:
            if probe["archive_id"] == baseline["archive_id"] and probe["cursor"] < baseline["cursor"]:
                error = "cursor_regressed"
            elif not incident and probe["archive_id"] != baseline["archive_id"]:
                error = "archive_changed"
        if not incident:
            if target == "console" and not error:
                try:
                    self.sync(probe,s)
                except ProbeError as exc:
                    error = str(exc)
            if not error:
                if s["failures"] or s["baseline"] != probe:
                    s.update(failures=0, baseline=probe)
                    with self.db:
                        self.save(target, s)
                return
            if not s["failures"] and self.clock() < self.start_at:
                return
            s["failures"] += 1
            with self.db:
                if s["failures"] >= self.threshold:
                    identity = "watchdog-" + uuid.uuid4().hex
                    incident = {"id":identity, "operation":identity+"/sync", "head":None, "attempt":0,
                                "pending":False, "reported":False, "reason":error}
                    s["incident"] = incident
                    self.emit(target, incident, "detected", "warn", reason=error, failures=s["failures"],
                              probe_path=PATHS[target][0], baseline=baseline)
                    self.emit(target, incident, "action_requested", action="refresh_authoritative_view",
                              method="GET", path=PATHS[target][1])
                self.save(target, s)
            if not incident:
                return
        if incident["reported"] and not incident["pending"]:
            if error or self.clock() < self.next_check.get(target, 0):
                return
            self.next_check[target] = self.clock()+self.recheck
        if not incident["pending"]:
            with self.db:
                incident["attempt"] += 1
                incident["pending"] = True
                if not incident["reported"]:
                    self.emit(target,incident,"action_attempt",attempt=incident["attempt"])
                self.save(target,s)
        view, read_error = read_safely(self.reader, target, True, incident["operation"])
        reason = read_error or error
        if target == "console" and not reason:
            if view["archive_id"] != probe["archive_id"] or view["cursor"] < probe["cursor"]:
                reason = "archive_inconsistent"
            else:
                try:
                    if not self.sync(probe, s):
                        return
                except ProbeError as exc:
                    reason = str(exc)
        with self.db:
            incident["pending"] = False
            if reason:
                if not incident["reported"] or incident.get("last_error") != reason:
                    self.emit(target,incident,"action_unavailable","warn",reason=reason,attempt=incident["attempt"])
                incident["last_error"] = reason
                if incident["attempt"] >= self.budget and not incident["reported"]:
                    self.emit(target,incident,"intervention_required","error",reason=reason,steps=STEPS[target],
                              attempts=incident["attempt"])
                    incident["reported"] = True
            else:
                coverage = {}
                if target == "console" and baseline:
                    coverage = {"archive_changed":view["archive_id"] != baseline["archive_id"],
                                "retention_crossed":view["pruned_through_seq"] > baseline["cursor"]}
                self.emit(target,incident,"action_confirmed",probe=probe,observation=view,coverage=coverage,
                          repair=incident.get("repair"),method="GET",path=PATHS[target][1],attempt=incident["attempt"])
                self.emit(target,incident,"recovered",result="authoritative_view_refreshed")
                s.update(incident=None,failures=0,baseline=probe,view=view)
                self.next_check.pop(target,None)
            self.save(target,s)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-url", default="http://agent:8090")
    parser.add_argument("--console-url", default="http://console:8080")
    parser.add_argument("--journal", type=Path, default=Path("/state/watchdog/module-events.sqlite"))
    for name, default, cast in (("interval",5,float),("timeout",2,float),("grace",10,float),
                                ("threshold",3,int),("attempts",3,int),("recheck",60,float)):
        parser.add_argument("--"+name, type=cast, default=default)
    args = parser.parse_args()
    if not (0.1<=args.interval<=120 and 0.1<=args.timeout<=10 and 0<=args.grace<=600
            and 1<=args.threshold<=30 and 1<=args.attempts<=10 and 0.5<=args.recheck<=600):
        parser.error("invalid watchdog time or attempt budget")
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    try:
        reader = Reader({"agent":args.agent_url, "console":args.console_url},args.timeout)
        with closing(Watchdog(args.journal, reader, threshold=args.threshold, grace=args.grace, attempts=args.attempts,
                              recheck=args.recheck, ttl=math.ceil(12*args.timeout+3*args.interval+5))) as monitor:
            while not stopped.is_set():
                monitor.tick()
                stopped.wait(args.interval)
    except (sqlite3.Error,OSError,ValueError) as exc:
        print(json.dumps({"code":"watchdog.unavailable","error_type":type(exc).__name__}),file=sys.stderr,flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
