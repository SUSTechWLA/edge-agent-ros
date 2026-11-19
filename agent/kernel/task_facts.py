"""Owner-side read-only task facts for Console and other independent consumers."""
import base64
import binascii
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs

from edge_contracts.trace_content import sanitized


@contextmanager
def read_db(path):
    parent_stat = path.parent.stat()
    initial = path.stat()
    options = "?mode=ro"
    # A clean, checkpointed WAL-mode DB has no WAL/SHM. On a read-only mount
    # SQLite cannot create them. Live WAL pages must always use normal locking.
    def sidecars():
        return any(path.with_name(path.name + ext).exists() for ext in ("-wal","-shm","-journal"))
    immutable = False
    if hasattr(os, "statvfs") and os.statvfs(path).f_flag & os.ST_RDONLY and not sidecars():
        with path.open("rb") as stream:
            header = stream.read(20)
        immutable = header[:16] == b"SQLite format 3\x00" and header[18:20] == b"\x02\x02"
        if immutable:
            options += "&immutable=1"
    with closing(sqlite3.connect(path.absolute().as_uri()+options, uri=True, timeout=0.15)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        try:
            yield db
        finally:
            if immutable:
                # Another mount can still host a writer. Never return facts
                # from the immutable view across a WAL creation/checkpoint,
                # file replacement or transient sidecar creation/removal.
                final = path.stat()
                if (sidecars() or path.parent.stat().st_ctime_ns != parent_stat.st_ctime_ns or
                        not os.path.samestat(initial, final) or initial.st_size != final.st_size or
                        initial.st_mtime_ns != final.st_mtime_ns or initial.st_ctime_ns != final.st_ctime_ns):
                    raise sqlite3.OperationalError("checkpoint changed while reading task facts")


MAX_CONTEXT_BYTES = 256 * 1024
# The HTTP client accepts at most four MiB. Leave room for pagination fields,
# counts, separators and the source identity in every response.
MAX_PAGE_BYTES = 4 * 1024 * 1024 - 8192
IDENTITY = re.compile(r"[A-Za-z0-9_.-]{1,96}")


def _task_identity(db, path, tables):
    if "audit_identity" not in tables:
        stat = path.stat()
        return f"legacy:{stat.st_dev}:{stat.st_ino}"
    row = db.execute("SELECT id FROM audit_identity WHERE singleton=1").fetchone()
    if not row or not isinstance(row[0],str) or not IDENTITY.fullmatch(row[0]):
        raise sqlite3.DatabaseError("missing or invalid task journal identity")
    return row[0]


def _task_trace(tables):
    if "task_triggers" in tables:
        return "LEFT JOIN task_triggers tr ON tr.task_id=t.id", "coalesce(tr.trace_id,t.id)"
    return "", "t.id"


def identifiers(task_id=None, trace_id=None):
    if (task_id is None) == (trace_id is None):
        raise ValueError("provide exactly one task_id or trace_id")
    value = task_id if task_id is not None else trace_id
    if (not isinstance(value, str) or not 1 <= len(value) <= 512
            or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("task or trace identifier must be 1 to 512 printable characters")
    return {"task_id":task_id} if task_id is not None else {"trace_id":trace_id}


def _read(path, task_id, trace_id, after, limit, expected):
    # Owner state, steps and related tasks share one SQLite snapshot.
    with read_db(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
        if not columns:
            raise sqlite3.DatabaseError("task table missing")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        join, trace = _task_trace(tables)
        if expected is not None:
            _check_source("agent:" + _task_identity(db, path, tables), expected)
        names = ["id","text","state","detail","created","updated","provider"]
        # Keep at most one context budget plus a sentinel per text field. The
        # public truncation below still reports any omission.
        def bounded_field(name):
            if name not in columns:
                return "NULL AS "+name
            if name in ("text", "detail"):
                return f"CASE WHEN typeof(t.{name})='text' THEN substr(t.{name},1,{MAX_CONTEXT_BYTES+1}) ELSE t.{name} END AS {name}"
            return "t."+name
        projection = ",".join(bounded_field(name) for name in names)
        # A damaged or imported ledger can have much larger JSON than the public
        # context allows. SQLite checks the byte length before sending it to Python.
        if "steps" in columns:
            too_large = f"length(CAST(t.steps AS BLOB))>{MAX_CONTEXT_BYTES}"
            projection += f",CASE WHEN {too_large} THEN NULL ELSE t.steps END AS steps,{too_large} AS oversize"
        else:
            projection += ",NULL AS steps,0 AS oversize"
        condition = "t.id=?" if task_id is not None else trace+"=?"
        row = db.execute(f"SELECT {projection}, {trace} AS trace_id FROM tasks t {join} WHERE {condition}"
                         " ORDER BY t.rowid DESC LIMIT 1", (task_id or trace_id,)).fetchone()
        page = {"event_window":None, "events":[], "events_has_more":False, "next_after":after}
        if row is None:
            return None, [], False, page
        task = dict(row)
        related = [dict(item) for item in db.execute(
            f"SELECT t.id,{bounded_field('text')},t.state FROM tasks t {join} WHERE {trace}=? AND t.id!=?"
            " ORDER BY t.rowid DESC LIMIT 9", (task["trace_id"],task["id"]))]
        if "events" in tables:
            source = "agent:" + _task_identity(db, path, tables)
            first, last = db.execute("SELECT min(seq),max(seq) FROM events WHERE task_id=?",
                                     (task["id"],)).fetchone()
            page["event_window"] = {"source_id":source, "first_seq":first, "last_seq":last}
            page.update(_task_events(db, source, after, limit, task["id"], MAX_PAGE_BYTES-MAX_CONTEXT_BYTES))
        return task, related[:8], len(related)>8, page


def task_context(path, task_id=None, trace_id=None, after=0, limit=0, source_id=None):
    identifiers(task_id, trace_id)
    notes = []
    try:
        task, related, more, page = _read(path, task_id, trace_id, after, limit, source_id)
    except (OSError, sqlite3.Error):
        task, related, more = None, [], False
        page = {"event_window":None, "events":[], "events_has_more":False, "next_after":after}
        notes.append("任务账本暂不可读，当前终态需要恢复连接后核对。")
    if task is not None:
        if task.pop("oversize"):
            notes.append("任务上下文超过 256 KiB 公开预算，已省略步骤；细节请继续读取公开事件。")
        try:
            steps = json.loads(task["steps"]) if isinstance(task["steps"], str) else task["steps"]
            if steps is None:
                steps = []
            if not isinstance(steps, list):
                raise ValueError("steps must be a list")
        except (ValueError, TypeError, RecursionError):
            steps = []
            notes.append("任务步骤数据不可解析，未提供步骤内容。")
        task["steps"] = steps
    result = {"schema_version":1,
            "scope":{"task_id":task["id"] if task else task_id,
                     "trace_id":task["trace_id"] if task else trace_id},
            "task":task, "related_tasks":related, "related_tasks_has_more":more,
            "task_available":task is not None, "notes":notes}
    try:
        result = sanitized(result)
        raw = json.dumps(result,ensure_ascii=False,allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError):
        if task:
            task["steps"] = []
        notes.append("任务步骤结构无法安全公开，未提供步骤内容。")
        result = sanitized({**result,"task":task,"notes":notes})
        raw = json.dumps(result,ensure_ascii=False,allow_nan=False).encode("utf-8")
    if len(raw)>MAX_CONTEXT_BYTES:
        # The context endpoint exposes data, not a summary. Omitted content is
        # explicit; callers can continue using the independent event API.
        if result["task"]:
            result["task"]["steps"] = []
            for key in ("text","detail"):
                value = result["task"].get(key)
                if isinstance(value,str):
                    result["task"][key] = value[:8192]
        for related in result["related_tasks"]:
            if isinstance(related.get("text"),str):
                related["text"] = related["text"][:1024]
        result["notes"].append("任务上下文超过 256 KiB 公开预算，已省略步骤并限制长文本；细节请继续读取公开事件。")
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_CONTEXT_BYTES:
            raise _RowTooLarge(page["event_window"]["source_id"] if page["event_window"] else None, task_id or trace_id)
    return {**result, **page}


STATES = {
    "active": ("PLANNING", "RUNNING", "WAITING"),
    "succeeded": ("SUCCEEDED",),
    "issues": ("BLOCKED", "RECOVERY_REQUIRED", "FAILED"),
}


def page_cursor(identity, state, rowid):
    return base64.urlsafe_b64encode(json.dumps([identity,state,rowid]).encode()).decode().rstrip("=")


def cursor_position(token, identity, state):
    try:
        if not 1 <= len(token) <= 512:
            raise ValueError()
        data = json.loads(base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True))
        if (not isinstance(data,list) or len(data)!=3 or data[:2]!=[identity,state]
                or type(data[2]) is not int or not 1 <= data[2] <= 2**63-1):
            raise ValueError()
        return data[2]
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("invalid before cursor for this task journal and state") from exc


def task_page(path, state="all", before=None, limit=30):
    if state != "all" and state not in STATES:
        raise ValueError("state must be all, active, succeeded or issues")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with read_db(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        identity = _task_identity(db, path, tables)
        conditions, values = [], []
        if before is not None:
            conditions.append("t.rowid < ?")
            values.append(cursor_position(before,identity,state))
        if state in STATES:
            conditions.append("t.state IN (" + ",".join("?" for _ in STATES[state]) + ")")
            values.extend(STATES[state])
        by_state = dict(db.execute("SELECT state,count(*) FROM tasks GROUP BY state"))
        counts = {"all":sum(by_state.values()), **{group:sum(by_state.get(s,0) for s in states)
                  for group, states in STATES.items()}}
        join, trace = _task_trace(tables)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        # Fetch positions before potentially large rows. A single oversized row
        # must not prevent delivery of its valid predecessors in the same page.
        positions = db.execute(f"SELECT t.rowid FROM tasks t {where} ORDER BY t.rowid DESC LIMIT ?", [*values,limit+1])
        sql = f"""SELECT t.id,t.text,t.state,t.detail,t.created,t.updated,{trace} AS trace_id
                  FROM tasks t {join} WHERE t.rowid=?"""
        items, last, more = _bounded_page(db, positions, limit, MAX_PAGE_BYTES, "agent:"+identity,
                                         lambda pos:dict(db.execute(sql,(pos,)).fetchone()))
        return {"tasks":items, "counts":counts, "next_before":page_cursor(identity,state,last) if more else None}


ROUTES = {"/agent/task-context": {"task_id", "trace_id", "source_id", "after", "limit"},
          "/agent/task-index": {"state", "before", "limit"},
          "/agent/task-events": {"source_id", "after", "limit"},
          "/agent/registry-events": {"source_id", "after", "limit"}}


def _registry_source(db, tables):
    row = db.execute("SELECT value FROM module_bus_meta WHERE key='source_id'").fetchone()
    if not row or not isinstance(row[0],str) or not IDENTITY.fullmatch(row[0]):
        raise sqlite3.DatabaseError("invalid registry identity")
    heartbeat = None
    if "module_bus_heartbeat" in tables:
        entry = db.execute("SELECT pulse,ttl_s FROM module_bus_heartbeat WHERE singleton=1").fetchone()
        if entry is not None:
            pulse, ttl = entry
            if not isinstance(pulse,str) or not IDENTITY.fullmatch(pulse) or type(ttl) is not int or not 2 <= ttl <= 3600:
                # The reader archives valid event rows before diagnosing a bad
                # heartbeat. Preserve that ordering without exporting the bad value.
                heartbeat = [None, None]
            else:
                heartbeat = [pulse, ttl]
    return "journal:runtime.registry:" + row[0], heartbeat


class _SourceChanged(Exception):
    pass


class _RowTooLarge(Exception):
    pass


def _check_source(source, expected):
    if expected is not None and expected != source:
        raise _SourceChanged(source)


def _cursor(params, default_limit, maximum):
    limit = int(params.get("limit", default_limit))
    after = int(params.get("after", 0))
    expected = params.get("source_id")
    if not 0 <= limit <= maximum or not 0 <= after <= 2**63-1 or (after and not expected):
        raise ValueError("invalid event cursor or limit")
    if expected is not None and not 1 <= len(expected) <= 160:
        raise ValueError("invalid source_id")
    return after, limit, expected


def _bounded_page(db, positions, limit, budget, source, load):
    # Apply SQLite's native length limit before materializing a corrupt/huge
    # row. Position lookups and data reads use the same read-only transaction.
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, budget)
    rows, used, last = [], 0, None
    for position, in positions:
        if len(rows) == limit:
            return rows, last, True
        try:
            item = load(position)
            size = len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode()) + 2
        except sqlite3.DataError:
            size = budget + 1
        except (TypeError, ValueError, OverflowError) as exc:
            raise sqlite3.DatabaseError("event or task row cannot be represented as JSON") from exc
        if used + size > budget:
            if not rows:
                raise _RowTooLarge(source, position)
            return rows, last, True
        rows.append(item)
        used += size
        last = position
    return rows, last, False


def _task_events(db, source, after, limit, task_id=None, budget=MAX_PAGE_BYTES):
    clause, values = (" AND task_id=?", (task_id,)) if task_id is not None else ("", ())
    positions = db.execute("SELECT seq FROM events WHERE seq>?"+clause+" ORDER BY seq LIMIT ?",
                           (after, *values, limit+1))
    def read_one(seq):
        row = dict(db.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone())
        # Session-facing context is redacted like its task and steps. The
        # global ingress feed preserves old payload text for archive replay.
        return sanitized(row) if task_id is not None else row
    rows, last, more = _bounded_page(db, positions, limit, budget, source, read_one)
    return {"events":rows, "next_after":last if rows else after, "events_has_more":more}


def read_task(db, key, *, trigger_columns=("task_id", "context", "trace_id"), has_events=True):
    """The writer and both HTTP services project the original, persisted task."""
    join = "LEFT JOIN task_triggers tr ON tr.task_id=tasks.id" if "task_id" in trigger_columns else ""
    context = "tr.context" if join and "context" in trigger_columns else "NULL"
    trace = "tr.trace_id" if join and "trace_id" in trigger_columns else "tasks.id"
    latest = """(SELECT kind FROM events WHERE task_id=tasks.id AND
        kind NOT IN ('settlement_deferred','settlement_recovered') ORDER BY seq DESC LIMIT 1)""" if has_events else "NULL"
    row = db.execute(f"""SELECT tasks.*, {context} AS context, {trace} AS trace_id, {latest} AS latest_event
                         FROM tasks {join} WHERE tasks.id=?""", (key,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["steps"] = json.loads(item["steps"]) if item.get("steps") is not None else []
    trigger = item.pop("context")
    item["trigger"] = json.loads(trigger) if trigger is not None else None
    item["trace_id"] = item["trace_id"] or key
    item["can_retry"] = item.pop("latest_event") == "retry_exhausted" and item["state"] in ("BLOCKED","RECOVERY_REQUIRED")
    return item


def detail_events(db, key, after=None, limit=128):
    """Keep the existing /agent/tasks tail page, forward cursor and payload-byte cut."""
    if not 1 <= limit <= 500 or (after is not None and not 0 <= after <= 2**63-1):
        raise ValueError("事件分页参数无效")
    rows, size = [], 0
    reverse = after is None
    order = "DESC" if reverse else "ASC"
    stream = db.execute("SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq "+order+" LIMIT ?",
                        (key,after or 0,limit+1))
    for row in stream:
        item = dict(row)
        # The writer adds these two columns during normal legacy migration.
        item.setdefault("payload", "{}")
        item.setdefault("event_id", None)
        size += len(item["payload"].encode())
        if len(rows)==limit or (rows and size>350000):
            return (rows[::-1] if reverse else rows), True
        rows.append(item)
    return (rows[::-1] if reverse else rows), False


def detail_request(query_string, path):
    """Read the old task detail wire shape without opening/migrating a Journal."""
    try:
        query = parse_qs(query_string, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
        if query.keys() - {"id","after","limit"} or any(len(v) != 1 for v in query.values()):
            raise ValueError("invalid task query")
        key = query.get("id", [""])[0]
        if not key:
            return 404, {"detail":"未找到记录"}
        after = int(query["after"][0]) if "after" in query else None
        limit = int(query.get("limit", ["128"])[0])
        if not 1 <= limit <= 500 or (after is not None and not 0 <= after <= 2**63-1):
            raise ValueError("事件分页参数无效")
    except ValueError as error:
        return 400, {"code":"facts.query_invalid", "detail":str(error)}
    try:
        with read_db(Path(path)) as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_PAGE_BYTES)
            columns = {r[1] for r in db.execute("PRAGMA table_info(task_triggers)")}
            events = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone())
            item = read_task(db, key, trigger_columns=columns, has_events=events)
            if item is None:
                return 404, {"detail":"未找到记录"}
            item["events"], item["events_has_more"] = detail_events(db, key, after, limit) if events else ([], False)
            if len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode()) > MAX_PAGE_BYTES:
                return 413, {"code":"facts.row_too_large", "task_id":key}
            return 200, item
    except sqlite3.DataError:
        return 413, {"code":"facts.row_too_large", "task_id":key}
    except (OSError, sqlite3.Error, TypeError, ValueError, RecursionError, AttributeError) as error:
        return 503, {"code":"facts.unavailable", "reason":type(error).__name__}


def _event_page(path, params, registry):
    maximum = 32 if registry else 256
    after, limit, expected = _cursor(params, maximum, maximum)
    if registry and path.is_symlink():
        raise sqlite3.DatabaseError("invalid registry source")
    with read_db(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not registry:
            source = "agent:" + _task_identity(db, path, tables)
            _check_source(source, expected)
            return {"source_id":source, **_task_events(db, source, after, limit)}
        source, heartbeat = _registry_source(db, tables)
        _check_source(source, expected)
        # A 65537-byte prefix preserves the verifier's invalid-size sentinel.
        # Even after base64 encoding the largest possible page is under 3 MiB.
        stream = db.execute("""SELECT seq,substr(CAST(event AS BLOB),1,65537) FROM module_bus_events
                               WHERE seq>? ORDER BY seq LIMIT ?""", (after, limit+1))
        events = []
        more = False
        for seq, raw in stream:
            if len(events) == limit:
                more = True
                break
            events.append({"seq":seq, "raw_b64":base64.b64encode(raw).decode("ascii") if raw is not None else None})
        return {"source_id":source, "events":events, "events_has_more":more,
                "next_after":events[-1]["seq"] if events else after, "heartbeat":heartbeat}


def read_request(path, query_string, task_db, registry_db):
    """Serve the owner's fact routes without opening a writer or depending on ROS."""
    if path not in ROUTES:
        return 404, {"code":"facts.not_found"}
    try:
        params = parse_qs(query_string, keep_blank_values=True, strict_parsing=True, max_num_fields=5)
        if params.keys() - ROUTES[path] or any(len(values) != 1 for values in params.values()):
            raise ValueError("unknown or repeated query parameter")
        params = {key:values[0] for key,values in params.items()}
        if path == "/agent/task-context":
            after, limit, source = _cursor(params, 0, 256)
            return 200, task_context(Path(task_db), params.get("task_id"), params.get("trace_id"), after, limit, source)
        if path == "/agent/task-index":
            return 200, task_page(Path(task_db), params.get("state", "all"), params.get("before"),
                                  int(params.get("limit", 30)))
        registry = path == "/agent/registry-events"
        return 200, _event_page(Path(registry_db if registry else task_db), params, registry)
    except _SourceChanged as error:
        return 409, {"code":"facts.source_changed", "source_id":error.args[0]}
    except _RowTooLarge as error:
        source, position = error.args
        return 413, {"code":"facts.row_too_large", "source_id":source, "position":position}
    except ValueError as error:
        return 400, {"code":"facts.query_invalid", "detail":str(error)}
    except (OSError, sqlite3.Error) as error:
        reason = "absent" if isinstance(error,FileNotFoundError) else type(error).__name__
        return 503, {"code":"facts.unavailable", "reason":reason}
