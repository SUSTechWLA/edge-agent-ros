"""Draft, verify and publish a derived read-only tool. No ROS or third-party dependencies."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.request import Request, urlopen
import uuid

from derived_tools import COMPARISON_OPS, MAX_BYTES, UNITS_BY_FIELD, capability, check_id, evaluate, parse_definition, validate
from tracing import clean_text
from module_journal import ModuleJournal


PROMPT = """Choose a read-only odometry tool for the exact user request.
x and y are coordinates in the odometry frame (metres); yaw is orientation (radians);
speed is signed forward velocity (metres per second), not total speed or proven standstill.
Select one field, its requested unit, and a comparison in that output unit if requested.
Use op=read and threshold=0 for a direct measurement. Use field=unsupported when the
request needs any other source, state change, motion command, or unsupported calculation."""


def read_json(path, limit):
    with Path(path).open("rb") as stream:
        raw = stream.read(limit+1)
    if len(raw) > limit:
        raise ValueError(f"{path}: input too large")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except RecursionError as exc:
        raise ValueError("JSON input is too deeply nested") from exc


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+"\n").encode()


def public_reference(value):
    # IDs travel to the shared archive. URLs and recognizable credentials belong in private records.
    return (isinstance(value, str) and re.fullmatch(r"[\w./:@-]{1,240}", value) is not None
            and "://" not in value and clean_text(value) == value)


def read_definition(path):
    with Path(path).open("rb") as stream:
        return parse_definition(stream.read(MAX_BYTES+1))


def draft(args):
    check_id(args.id)
    source_task = None
    if args.from_task:
        task = read_json(args.from_task, 2_000_000)
        if (not isinstance(task,dict) or task.get("state")!="BLOCKED" or task.get("steps")!=[]
                or not isinstance(task.get("id"),str) or not isinstance(task.get("trace_id"),str)):
            raise ValueError("expected a blocked task with no dispatched step")
        events = task.get("events")
        if not isinstance(events,list) or not events or task.get("events_has_more"):
            raise ValueError("provide the terminal page of the task journal")
        if any(not isinstance(row,dict) or row.get("task_id")!=task["id"]
               or row.get("operation_id") is not None or row.get("kind")=="dispatch" for row in events):
            raise ValueError("expected only this task's pre-dispatch journal")
        event = events[-1]
        if event.get("kind")!="blocked" or not isinstance(event.get("payload"),str):
            raise ValueError("missing terminal planner diagnosis")
        payload = json.loads(event["payload"])
        if not isinstance(payload,dict) or not isinstance(payload.get("diagnostic"),dict):
            raise ValueError("missing terminal diagnostic fields")
        trace = payload.get("trace")
        if not isinstance(trace,dict) or trace.get("trace_id")!=task["trace_id"] or trace.get("layer")!="planner":
            raise ValueError("terminal event does not belong to this planner trace")
        code = payload["diagnostic"].get("code")
        if code not in ("planner.capability_missing","planner.not_configured"):
            raise ValueError("this failure needs recovery or diagnosis rather than a new definition")
        args.request = task["text"]
        source_task = {"task_id":task["id"], "trace_id":task["trace_id"], "reason_code":code}
    if not isinstance(args.request,str) or not 1 <= len(args.request.strip()) <= 200:
        raise ValueError("provide a user request of 1–200 characters")
    description = clean_text(args.request.strip())
    if not args.ollama_url:
        raise ValueError("configure EDGE_OLLAMA_URL or pass --ollama-url")
    schema = {"type":"object", "additionalProperties":False,
              "required":["field","unit","op","threshold"],
              "properties":{"field":{"enum":[*UNITS_BY_FIELD, "unsupported"]},
                            "unit":{"enum":sorted({u for units in UNITS_BY_FIELD.values() for u in units} | {"none"})},
                            "op":{"enum":["read", *sorted(COMPARISON_OPS)]}, "threshold":{"type":"number"}}}
    context = {"request":description, "allowed_units":UNITS_BY_FIELD}
    if source_task:
        context["reason_code"] = source_task["reason_code"]
    body = {"model":args.model, "stream":False, "format":schema, "options":{"temperature":0},
            "messages":[{"role":"system","content":PROMPT},
                        {"role":"user","content":json.dumps(context,ensure_ascii=False)}]}
    request = Request(args.ollama_url.rstrip("/")+"/api/chat", data=json_bytes(body),
                      headers={"Content-Type":"application/json"})
    with urlopen(request, timeout=90) as response:
        raw = response.read(131073)
    if len(raw)>131072:
        raise ValueError("model response is too large")
    reply = json.loads(json.loads(raw)["message"]["content"])
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_bytes(json_bytes({"author_protocol":"ollama.chat", "model":args.model, "id":args.id,
            "request":description, "source_task":source_task, "prompt_sha256":hashlib.sha256(PROMPT.encode()).hexdigest(),
            "reply":reply, "response_sha256":hashlib.sha256(raw).hexdigest()}))
    if not isinstance(reply, dict) or set(reply)!=set(schema["required"]):
        raise ValueError("model returned an incomplete tool proposal")
    if reply["field"]=="unsupported":
        raise ValueError("the requested capability requires another provider")
    result = {"schema_version":1, "id":args.id, "description":description,
              "field":reply["field"], "unit":reply["unit"]}
    if reply["op"]!="read":
        result["comparison"]={"op":reply["op"], "threshold":reply["threshold"]}
    return validate(result)


def checked_definition(path, cases_path):
    definition = read_definition(path)
    cases = read_json(cases_path, 262144)
    if not isinstance(cases, list) or not 2 <= len(cases) <= 256:
        raise ValueError("provide 2–256 independently specified cases")
    comparison = "comparison" in definition
    expected_keys = {"unit","value","matches"} if comparison else {"unit","value"}
    values, outcomes = set(), set()
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict) or set(case)!={"sample","expected"}:
            raise ValueError(f"case {index}: expected sample and expected")
        expected = case["expected"]
        if not isinstance(expected, dict) or set(expected)!=expected_keys:
            raise ValueError(f"case {index}: incorrect expectation fields")
        actual = evaluate(definition, case["sample"])
        number = expected["value"]
        if (type(number) not in (float,int) or not math.isfinite(number) or expected["unit"]!=actual["unit"]
                or not math.isclose(actual["value"], number, rel_tol=1e-9, abs_tol=1e-9)):
            raise ValueError(f"case {index}: measurement differs from expectation")
        values.add(actual["value"])
        if comparison:
            if type(expected["matches"]) is not bool or expected["matches"] is not actual["matches"]:
                raise ValueError(f"case {index}: predicate differs from expectation")
            outcomes.add(expected["matches"])
    if len(values)<2 or comparison and len(outcomes)!=2:
        raise ValueError("use distinct measurements and cover each comparison outcome")
    return definition, {"case_count":len(cases), "cases_sha256":hashlib.sha256(json_bytes(cases)).hexdigest()}


@contextmanager
def writer_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/".publish.lock").open("a+b") as handle:
        if os.name=="nt":
            import msvcrt
            if handle.tell()==0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        # Both operating systems release the lock on close, including process exit.
        yield


def sync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".staging-", delete=False) as stream:
        temp = Path(stream.name)
    try:
        with temp.open("wb") as stream:
            stream.write(json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def installed(directory, name):
    try:
        current = read_definition(directory / (name+".json"))
    except FileNotFoundError:
        return None, "absent"
    if current["id"] != name:
        raise ValueError("registry changed; installed tool has a different id")
    return current, capability(current)["version"]


class DerivedRelease:
    module = "plugin.publisher"
    id_field = "tool_id"
    installed = staticmethod(installed)
    check_id = staticmethod(check_id)

    @staticmethod
    def candidate(args):
        if args.cases is None:
            raise ValueError("derived tools require independent --cases")
        definition, evidence = checked_definition(args.definition, args.cases)
        return definition, {**evidence, "version":capability(definition)["version"]}

    @staticmethod
    def check_graph(directory, name, definition):
        # A derived tool reads a fixed odometry contract and has no registry dependencies.
        return None


class ReleaseRejected(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


def release_event(journal, context, kind, code, level, **fields):
    event_id = journal.emit({"schema_version":1, "module":context.get("module","plugin.publisher"), "layer":"publication",
                         "kind":"plugin.release."+kind, "code":"release."+code, "level":level,
                         "trace_id":context["trace_id"], "parent_event_id":context["parent_event_id"],
                         "data":{**context["data"], **fields}}, key=context["data"]["change_id"]+"."+kind)
    if "request_key" in context:
        if kind == "requested":
            journal.db.execute("INSERT INTO publication_receipts VALUES(?,?,?)",
                               (context["request_key"], context["fingerprint"], event_id))
        else:
            journal.db.execute("UPDATE publication_receipts SET event_id=? WHERE request_key=?",
                               (event_id, context["request_key"]))
    return event_id


class PriorRequestError(ReleaseRejected):
    """A previous outcome or a conflicting reuse; this invocation cannot rewrite that receipt."""


def prior_request(journal, context, request_id, trace, parent, id_field):
    if not public_reference(request_id):
        raise ValueError("provide a valid request identifier")
    data = context["data"]
    key = hashlib.sha256(request_id.encode()).hexdigest()
    content = {k:v for k,v in data.items() if k!="change_id"}
    fingerprint = hashlib.sha256(json_bytes([content,trace,parent])).hexdigest()
    row = journal.db.execute("""SELECT r.fingerprint, e.event FROM publication_receipts r
        JOIN module_bus_events e ON e.event_id=r.event_id WHERE r.request_key=?""",(key,)).fetchone()
    if row:
        if row[0] != fingerprint:
            data["prior_event_id"] = json.loads(row[1])["event_id"]
            raise ReleaseRejected("request_conflict", "request id already identifies a different publication")
        event = json.loads(row[1])
        if event["kind"] != "plugin.release.applied":
            raise PriorRequestError(event["code"], f"previous request: {event['code']} ({event['event_id']})")
        saved = event["data"]
        evidence = {k:saved[k] for k in ("case_count","cases_sha256","declared_version") if k in saved}
        if saved["action"]=="publish":
            evidence["version"]=saved["target_version"]
        return {"action":saved["action"], "id":saved[id_field], "previous":saved["previous_version"],
                "change_id":saved["change_id"], "event_id":event["event_id"], **evidence}
    context.update(request_key=key, fingerprint=fingerprint)


def finish_release(journal, context, kind, code, level, **fields):
    with journal.db:
        event_id = release_event(journal, context, kind, code, level, **fields)
        journal.db.execute("DELETE FROM publication_pending WHERE change_id=?", (context["data"]["change_id"],))
    return event_id


def observed_version(directory, name, policy=DerivedRelease):
    try:
        return policy.installed(directory, name)[1]
    except (OSError, ValueError):
        return "unreadable"


def reconcile_releases(journal, directory, policy=DerivedRelease):
    rows = journal.db.execute("SELECT context FROM publication_pending ORDER BY rowid").fetchall()
    for (raw,) in rows:
        context = json.loads(raw)
        data = context["data"]
        version = observed_version(directory, data[policy.id_field], policy)
        if version == "unreadable":
            observation = "unreadable"
        elif version == data["target_version"] == data["previous_version"]:
            observation = "versions_identical"
        elif version == data["target_version"]:
            observation = "target_present"
        elif version == data["previous_version"]:
            observation = "previous_present"
        else:
            observation = "diverged"
        finish_release(journal, context, "reconciled", observation, "warn",
                       observed_version=version, observation=observation, writer_attribution="unknown")


def publish_managed(args, journal, policy=DerivedRelease):
    directory = args.registry
    with journal.db:
        journal.db.execute("CREATE TABLE IF NOT EXISTS publication_pending(change_id TEXT PRIMARY KEY,context TEXT NOT NULL)")
        journal.db.execute("""CREATE TABLE IF NOT EXISTS publication_receipts(
            request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, event_id TEXT NOT NULL)""")
    reconcile_releases(journal, directory, policy)
    change_id = uuid.uuid4().hex
    expected = args.expect if re.fullmatch(r"absent|[a-f0-9]{64}", args.expect) else "invalid"
    data = {"change_id":change_id, "action":args.action, "expected_version":expected}
    trace = getattr(args, "trace_id", None)
    parent = getattr(args, "parent_event_id", None)
    context = {"module":policy.module, "trace_id":trace if public_reference(trace) else change_id,
               "parent_event_id":parent if public_reference(parent) else None, "data":data}
    evidence = {}
    definition = None
    rejection = "input_invalid"
    try:
        if any(ref is not None and not public_reference(ref) for ref in (trace,parent)):
            rejection = "invalid_reference"
            raise ValueError("provide a valid public event or trace identifier")
        if args.action == "remove":
            name = policy.check_id(args.id)
            target = "absent"
        else:
            definition, evidence = policy.candidate(args)
            name = definition["id"]
            target = evidence["version"]
            data.update({key:value for key,value in evidence.items() if key != "version"})
        data.update({policy.id_field:name, "target_version":target})
        if getattr(args, "request_id", None) is not None:
            replay = prior_request(journal, context, args.request_id, trace, parent, policy.id_field)
            if replay is not None:
                return replay
        rejection = "registry_unreadable"
        current, version = policy.installed(directory, name)
        data["previous_version"] = version
        if version != args.expect:
            rejection = "version_conflict"
            raise ValueError(f"registry changed; current version is {version}")
        if definition is None and current is None:
            rejection = "already_absent"
            raise ValueError("tool is not installed")
        policy.check_graph(directory, name, definition)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        if isinstance(exc, PriorRequestError):
            raise
        if isinstance(exc, ReleaseRejected):
            rejection = exc.code
        with journal.db:
            context["parent_event_id"] = release_event(journal, context, "requested", "requested", "info")
            release_event(journal, context, "rejected", rejection, "warn",
                          observed_version=data.get("previous_version","unknown"))
        raise

    # The intent and pending row are one transaction. A replacement and a SQL commit
    # cannot be one transaction. On restart, report the observed version without reapplying.
    with journal.db:
        context["parent_event_id"] = release_event(journal, context, "requested", "requested", "info")
        journal.db.execute("INSERT INTO publication_pending VALUES(?,?)", (change_id, json.dumps(context)))
    active = directory/(name+".json")
    try:
        if current:
            write_atomic(directory/".history"/name/(version+".json"), current)
        if definition is None:
            active.unlink()
            sync_directory(directory)
        else:
            write_atomic(directory/".history"/name/(target+".json"), definition)
            write_atomic(active, definition)
    except OSError as exc:
        finish_release(journal, context, "failed", "write_failed", "error", error_type=type(exc).__name__,
                       observed_version=observed_version(directory, name, policy))
        raise
    observed = observed_version(directory, name, policy)
    if observed != target:
        finish_release(journal, context, "failed", "postcondition_mismatch", "error", observed_version=observed)
        raise ValueError("registry differs after writing the managed definition")
    event_id = finish_release(journal, context, "applied", "applied", "info", observed_version=observed)
    return {"action":args.action, "id":name, "previous":version, "change_id":change_id,
            "event_id":event_id, **evidence}


def change(args):
    if args.action == "check":
        definition, evidence = checked_definition(args.definition, args.cases)
        return {"id":definition["id"], "version":capability(definition)["version"], **evidence}
    with writer_lock(args.registry):
        journal = ModuleJournal(args.registry / ".history" / "module-events.sqlite")
        try:
            return publish_managed(args, journal)
        finally:
            journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    author = commands.add_parser("draft", help="ask a model for a candidate JSON definition")
    author.add_argument("--id", required=True)
    origin = author.add_mutually_exclusive_group(required=True)
    origin.add_argument("--request")
    origin.add_argument("--from-task", type=Path, help="JSON exported from GET /agent/tasks?id=...; an unserved input before dispatch")
    author.add_argument("--ollama-url", default=os.getenv("EDGE_OLLAMA_URL"))
    author.add_argument("--model", default=os.getenv("EDGE_OLLAMA_MODEL","qwen2.5:1.5b"))
    author.add_argument("--record", type=Path, help="save the request and model decision as an audit artifact")
    for action in ("check","publish","remove"):
        cmd = commands.add_parser(action)
        if action=="remove":
            cmd.add_argument("id")
        else:
            cmd.add_argument("definition", type=Path)
            cmd.add_argument("--cases", type=Path, required=True)
        if action!="check":
            cmd.add_argument("--registry", type=Path, required=True, help="managed derived definitions directory")
            cmd.add_argument("--expect", required=True, help="current content version or absent")
            cmd.add_argument("--trace-id", help="trace of the proposal or change request")
            cmd.add_argument("--parent-event-id", help="proposal or other source event for this change")
    args = parser.parse_args()
    try:
        result = draft(args) if args.action=="draft" else change(args)
        sys.stdout.buffer.write(json_bytes(result))
    except sqlite3.Error as exc:
        parser.exit(1, f"publication audit database error: {exc}\n")
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__=="__main__":
    main()
