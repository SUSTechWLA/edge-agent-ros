"""Local, versioned prompt/diagnostic contracts and bounded event content."""
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path


BUILTINS = Path(__file__).with_name("contracts.json")
CODE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,95}")
SECRET = r"(?:password|passwd|secret|access[_-]?token|refresh[_-]?token|api[_-]?key|authorization|credential|cookie|private[_-]?key|token)"
MAX_CONTENT = 16384


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",",":")).encode()


def clean_text(value):
    value = re.sub(r"(?i)\bBearer\s+[\w./+=-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)\b(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{12,})", "[REDACTED]", value)
    value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
    return re.sub(r"""(?i)(?<![\w-])(["']?[\w-]{0,64}""" + SECRET +
                  r"""["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;&}]+)""",
                  r'\1"[REDACTED]"', value)


def sanitized(value):
    if isinstance(value,dict):
        return {k:("[REDACTED]" if re.search(SECRET,k,re.I) else sanitized(v)) for k,v in value.items()}
    if isinstance(value,list):
        return [sanitized(v) for v in value]
    if isinstance(value,str):
        # Ollama's user message and response may themselves contain serialized JSON.
        if value.lstrip().startswith(("{","[")):
            try:
                nested=json.loads(value)
                return json.dumps(sanitized(nested),ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        return clean_text(value)
    return value


def artifact(value):
    raw=encoded(value)
    meta={"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw),"encoding":"canonical-json-utf8"}
    if os.getenv("EDGE_TRACE_CONTENT","redacted")!="redacted":
        return {**meta,"mode":"metadata"}
    clean=sanitized(value)
    content=encoded(clean)
    if len(content)>MAX_CONTENT:
        return {**meta,"mode":"redacted","truncated":True,"preview":content[:MAX_CONTENT].decode("utf-8","ignore")}
    return {**meta,"mode":"redacted","truncated":False,"content":clean}


def contract(section,key):
    """Resolve one immutable snapshot per call. A bad override never erases defaults."""
    value=json.loads(BUILTINS.read_text(encoding="utf-8")).get(section,{}).get(key)
    directory=Path(os.getenv("EDGE_CONTRACT_DIR","/etc/edge/contracts"))
    field="text" if section=="prompts" else "hint"
    for path in sorted(directory.glob("*.json")):
        try:
            if path.stat().st_size>131072:
                continue
            doc=json.loads(path.read_text(encoding="utf-8-sig"))
            entry=doc[section][key]
            if (doc["schema_version"]==1 and isinstance(entry["version"],str) and
                    0<len(entry["version"])<=64 and isinstance(entry[field],str) and 0<len(entry[field])<=8192):
                value=entry
        except (ValueError,KeyError,TypeError,OSError,RecursionError):
            continue
    if not value:
        return None
    return {"id":key,"version":value["version"],"sha256":hashlib.sha256(value[field].encode()).hexdigest(),
            field:value[field]}


def diagnosis(exc, fallback, origin):
    code=getattr(exc,"code",None)
    if not isinstance(code,str) or not CODE.fullmatch(code):
        code=fallback
    item={"code":code,"origin":getattr(exc,"origin",origin),"error_type":type(exc).__name__,
          "message":clean_text(str(exc))[:1200],"catalog":contract("diagnostics",code)}
    if getattr(exc,"details",None) is not None:
        item["details"]=artifact(exc.details)
    return item


def exchange(trace, layer, module, request, invoke, *, meta=None, commit_candidate=None):
    """Persist intent before IO; a request with no receipt is meaningful after a crash."""
    if trace is None:
        return invoke()
    call_id=uuid.uuid4().hex
    common={"module":module,"layer":layer,"call_id":call_id,**(meta or {})}
    started=time.monotonic()
    trace(layer+".request",{**common,"artifact":artifact(request)})
    try:
        response=invoke()
    except Exception as exc:
        diagnostic=diagnosis(exc, layer+".failed",module)
        trace(layer+".error",{**common,"diagnosis":diagnostic,
                             "duration_ms":round((time.monotonic()-started)*1000,2)})
        raise
    if commit_candidate is not None and response is not None:
        # Store the full candidate and its bounded trace in one task-journal transaction.
        commit_candidate(response, {**common,"outcome":"CANDIDATE","artifact":artifact(response),
                                    "duration_ms":round((time.monotonic()-started)*1000,2)})
        return response
    if layer=="tool":
        common["outcome"]="RUNNING" if response is None else "SUCCEEDED"
    trace(layer+".response",{**common,"artifact":artifact(response),
                             "duration_ms":round((time.monotonic()-started)*1000,2)})
    return response
