"""Public projections of the Agent task journal. Input and archive sequences are independent."""
from datetime import datetime, timezone
import json
import math
import re

from edge_contracts.trace_content import clean_text

PAGE_SIZE = 32
SYMBOL = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,95}")
UUID_HEX = re.compile(r"[a-fA-F0-9]{32}")
UTC = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(?:Z|\+00:00)", re.ASCII)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_ns(value):
    match = UTC.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("invalid journal UTC")
    try:
        parsed = datetime.fromisoformat(match[1]).replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("invalid journal UTC date or time") from None
    delta = parsed - EPOCH
    result = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int((match[2] or "").ljust(9,"0"))
    if not 0 < result < 2**63:
        raise ValueError("journal UTC is outside the archive range")
    return result


def symbol(value):
    return isinstance(value, str) and SYMBOL.fullmatch(value) and clean_text(value) == value


def page(task, after):
    rows = task.get("events", [])
    more = task.get("events_has_more", False)
    if not isinstance(rows, list) or len(rows)>PAGE_SIZE or type(more) is not bool or (more and not rows):
        raise ValueError("invalid Agent journal page")
    result = []
    for row in rows:
        if not isinstance(row, dict) or row.get("task_id") != task["id"]:
            raise ValueError("Agent event belongs to another task")
        seq = row.get("seq")
        if type(seq) is not int or not after < seq < 2**63:
            raise ValueError("Agent event sequences must advance within the task")
        step, kind = row.get("step"), row.get("kind")
        if type(step) is not int or not 0 <= step < 2**31 or not symbol(kind):
            raise ValueError("invalid Agent event step or kind")
        utc = row["utc"]
        event = dict(kind="agent_trace", event_id=f"agent-trace-{seq}", source_seq=seq, step=step, phase=kind,
                     task_id=task["id"], source_utc=utc, stamp_ns=utc_ns(utc), clock_domain="agent_utc")
        op = row.get("operation_id")
        if op is not None:
            if op != f"{task['id']}/step/{step+1}":
                raise ValueError("Agent operation does not belong to this task and step")
            event["operation_id"] = op
        payload = row.get("payload", {})
        if isinstance(payload,str):
            payload = json.loads(payload)
        if not isinstance(payload,dict) or not isinstance(payload.get("trace",{}),dict):
            raise ValueError("invalid journal payload")
        trace = payload.get("trace",{})
        if trace.get("task_id",task["id"])!=task["id"] or trace.get("operation_id",op)!=op:
            raise ValueError("trace payload disagrees with the source task or operation")
        if "trace_id" in trace and trace["trace_id"] != task.get("trace_id", task["id"]):
            raise ValueError("journal event belongs to a different trace")
        source_id = row.get("event_id")
        if "event_id" in trace and source_id != trace["event_id"]:
            raise ValueError("journal source event identity mismatch")
        if isinstance(source_id,str) and UUID_HEX.fullmatch(source_id):
            event["source_event_id"] = source_id
        for key in ("call_id","parent_event_id"):
            value = trace.get(key)
            if isinstance(value,str) and UUID_HEX.fullmatch(value):
                event[key] = value
        for key in ("layer","module","code","tool","provider","outcome"):
            if symbol(trace.get(key)):
                event[key] = trace[key]
        diagnosis = trace.get("diagnosis")
        if isinstance(diagnosis,dict) and symbol(diagnosis.get("error_type")):
            event["error_type"] = diagnosis["error_type"]
        duration = trace.get("duration_ms")
        if type(duration) in (int,float) and 0<=duration<=2**53 and math.isfinite(duration):
            event["duration_ms"] = duration
        if event.get("layer")=="tool" and "module" in event:
            event.setdefault("tool",event["module"])
        step_data = payload.get("step")
        if isinstance(step_data,dict) and symbol(step_data.get("tool")):
            event.setdefault("tool",step_data["tool"])
        if symbol(payload.get("provider")):
            event.setdefault("provider",payload["provider"])
        after = seq
        result.append(event)
    return result, more


def request_text(task):
    value = task.get("text")
    trigger = task.get("trigger") or {}
    if not isinstance(trigger,dict):
        raise ValueError("invalid Agent trigger")
    if value is None or "created" not in task or trigger.get("kind","user")!="user":
        return None
    if not isinstance(value,str) or not 1<=len(value)<=4096:
        raise ValueError("invalid Agent request text")
    return dict(kind="agent_request", event_id="agent-request", task_id=task["id"], phase="user_request",
                source_utc=task["created"], stamp_ns=utc_ns(task["created"]), clock_domain="agent_utc",
                text=clean_text(value))
