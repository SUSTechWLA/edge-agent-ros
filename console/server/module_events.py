"""Wire contract for observations published on /edge/events (std_msgs/String)."""
import hashlib
import json
import re
from datetime import datetime, timezone

IDENTIFIER = re.compile(r"[\w./:@-]{1,240}\Z")
SENSITIVE = re.compile(r"token|password|secret|authorization|credential|cookie|api[_-]?key|private[_-]?key", re.I)
BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
ASSIGNMENT = re.compile(r"(?i)(\b(?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s&,]+")


def scrub(value, depth=0):
    if depth > 16:
        raise ValueError("event payload nesting exceeds 16")
    if isinstance(value, dict):
        return {k:("[redacted]" if SENSITIVE.search(k) else scrub(v,depth+1)) for k,v in value.items()}
    if isinstance(value, list):
        return [scrub(v,depth+1) for v in value]
    if isinstance(value, str):
        return ASSIGNMENT.sub(r"\1[redacted]",BEARER.sub(r"\1 [redacted]",value))
    return value


def identifier(value, name):
    if not isinstance(value,str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {name}")


def parse_event(raw):
    if len(raw)>65536 or len(raw.encode("utf-8"))>65536:
        raise ValueError("module event exceeds 64 KiB")
    event=json.loads(raw)
    if not isinstance(event,dict) or type(event.get("schema_version")) is not int or event["schema_version"]!=1:
        raise ValueError("unsupported event schema")
    for field in ("event_id","trace_id","module","kind","code"):
        identifier(event.get(field),field)
    for field in ("task_id","mission_id","operation_id","parent_event_id","layer","call_id"):
        if event.get(field) is not None:
            identifier(event[field],field)
    if event.get("parent_event_id")==event["event_id"]:
        raise ValueError("event cannot be its own parent")
    if event.get("level","info") not in ("info","warn","error"):
        raise ValueError("unsupported level")
    trigger=event.get("trigger",{})
    if not isinstance(trigger,dict):
        raise ValueError("invalid trigger")
    if trigger:
        for k in ("kind","id","source"):
            identifier(trigger.get(k),"trigger."+k)
    if not isinstance(event.get("data",{}),dict) or not isinstance(event.get("detail",""),str):
        raise ValueError("invalid event data or detail")
    if "utc" in event:
        if not isinstance(event["utc"],str):
            raise ValueError("invalid utc")
        try:
            at=datetime.fromisoformat(event["utc"].replace("Z","+00:00"))
        except ValueError:
            raise ValueError("invalid utc") from None
        if at.tzinfo is None:
            raise ValueError("event time requires a timezone")
        event["utc"]=at.astimezone(timezone.utc).isoformat()
    event=scrub(event)
    canonical=json.dumps(event,ensure_ascii=False,allow_nan=False,separators=(",",":"),sort_keys=True)
    return event,hashlib.sha256(canonical.encode("utf-8")).hexdigest()
