"""Draft, verify and publish a derived read-only tool. No ROS or third-party dependencies."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from urllib.request import Request, urlopen

from derived_tools import COMPARISON_OPS, MAX_BYTES, UNITS_BY_FIELD, capability, check_id, evaluate, parse_definition, validate
from tracing import clean_text


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
    return json.loads(raw.decode("utf-8-sig"))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+"\n").encode()


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
    finally:
        temp.unlink(missing_ok=True)


def change(args):
    if args.action=="remove":
        name = check_id(args.id)
        definition = None
        evidence = {}
    else:
        definition, evidence = checked_definition(args.definition, args.cases)
        name = check_id(definition["id"])
        evidence["version"] = capability(definition)["version"]
        if args.action=="check":
            return {"id":name, **evidence}
    directory = args.registry
    active = directory/(name+".json")
    with writer_lock(directory):
        current = read_definition(active) if active.exists() else None
        version = capability(current)["version"] if current else "absent"
        if version!=args.expect or (current and current["id"]!=name):
            raise ValueError(f"registry changed; current version is {version}")
        if current:
            write_atomic(directory/".history"/name/(version+".json"), current)
        if definition is None:
            if current is None:
                raise ValueError("tool is not installed")
            active.unlink()
        else:
            write_atomic(directory/".history"/name/(evidence["version"]+".json"), definition)
            write_atomic(active, definition)
    return {"action":args.action, "id":name, "previous":version, **evidence}


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
    args = parser.parse_args()
    try:
        result = draft(args) if args.action=="draft" else change(args)
        sys.stdout.buffer.write(json_bytes(result))
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__=="__main__":
    main()
