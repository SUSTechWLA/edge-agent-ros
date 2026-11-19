"""Read-only operator index over the Agent's task journal."""
import base64
import binascii
from contextlib import closing
import json
import sqlite3


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
    with closing(sqlite3.connect(path.absolute().as_uri()+"?mode=ro",uri=True,timeout=0.15)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "audit_identity" in tables:
            row = db.execute("SELECT id FROM audit_identity WHERE singleton=1").fetchone()
            if row is None:
                raise sqlite3.DatabaseError("missing task journal identity")
            identity = row[0]
        else:
            stat = path.stat()
            identity = f"legacy:{stat.st_dev}:{stat.st_ino}"
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
        trace, join = "t.id", ""
        if "task_triggers" in tables:
            trace = "coalesce(tr.trace_id,t.id)"
            join = "LEFT JOIN task_triggers tr ON tr.task_id=t.id"
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        # The writer inserts once and updates in place during execution. Insertion order keeps
        # continuation stable across status/progress updates and host clock changes.
        rows = db.execute(f"""SELECT t.rowid AS position,t.id,t.text,t.state,t.detail,t.created,t.updated,
                            {trace} AS trace_id FROM tasks t {join} {where} ORDER BY t.rowid DESC LIMIT ?""",
                          [*values,limit+1]).fetchall()
        items = [{key:row[key] for key in row.keys() if key!="position"} for row in rows[:limit]]
        return {"tasks":items, "counts":counts, "next_before":page_cursor(identity,state,rows[limit-1]["position"])
                if len(rows)>limit else None}
