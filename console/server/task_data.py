"""Read-only task data for independent consumers; no session narrative policy."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.append(str(Path(__file__).resolve().parents[2] / "agent"))
from tracing import sanitized

MAX_CONTEXT_BYTES = 256 * 1024


def identifiers(task_id=None, trace_id=None):
    if (task_id is None) == (trace_id is None):
        raise ValueError("provide exactly one task_id or trace_id")
    value = task_id if task_id is not None else trace_id
    if (not isinstance(value, str) or not 1 <= len(value) <= 512
            or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("task or trace identifier must be 1 to 512 printable characters")
    return {"task_id":task_id} if task_id is not None else {"trace_id":trace_id}


def _read(path, task_id, trace_id):
    # Owner state, steps and related tasks share one SQLite snapshot.
    with closing(sqlite3.connect(path.absolute().as_uri()+"?mode=ro", uri=True, timeout=0.15)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
        if not columns:
            raise sqlite3.DatabaseError("task table missing")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        join, trace = "", "t.id"
        if "task_triggers" in tables:
            join = "LEFT JOIN task_triggers tr ON tr.task_id=t.id"
            trace = "coalesce(tr.trace_id,t.id)"
        names = ["id","text","state","detail","created","updated","provider","steps"]
        projection = ",".join("t."+name if name in columns else "NULL AS "+name for name in names)
        condition = "t.id=?" if task_id is not None else trace+"=?"
        row = db.execute(f"SELECT {projection}, {trace} AS trace_id FROM tasks t {join} WHERE {condition}"
                         " ORDER BY t.rowid DESC LIMIT 1", (task_id or trace_id,)).fetchone()
        if row is None:
            return None, [], False
        task = dict(row)
        related = [dict(item) for item in db.execute(
            f"SELECT t.id,t.text,t.state FROM tasks t {join} WHERE {trace}=? AND t.id!=?"
            " ORDER BY t.rowid DESC LIMIT 9", (task["trace_id"],task["id"]))]
        return task, related[:8], len(related)>8


def task_context(path, task_id=None, trace_id=None):
    identifiers(task_id, trace_id)
    notes = []
    try:
        task, related, more = _read(path, task_id, trace_id)
    except (OSError, sqlite3.Error):
        task, related, more = None, [], False
        notes.append("任务账本暂不可读，当前终态需要恢复连接后核对。")
    if task is not None:
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
    return result
