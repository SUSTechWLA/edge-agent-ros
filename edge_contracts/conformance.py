"""Exercise a deployed read-only Tool or a service's explicitly selected HTTP GET representation."""
import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException, IncompleteRead
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import math
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event, Thread
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from .descriptor import NAME, binding
from .errors import CODE

MAX_BYTES = 2_000_000
ERRORS = (OSError, ValueError, HTTPException, RecursionError, OverflowError)
REPORT_ERRORS = ERRORS + (TypeError, KeyError, subprocess.SubprocessError)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def finite_number(token):
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("non-finite number in provider JSON")
    return value


def decode(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("JSON exceeds the runtime response limit")
    def unique_fields(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("ambiguous duplicate JSON fields")
        return result
    return json.loads(raw.decode("utf-8-sig"), parse_float=finite_number,
                      parse_constant=finite_number, object_pairs_hook=unique_fields)


def load_cases_or_manifest(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES+1)
    return decode(raw), hashlib.sha256(raw).hexdigest()


def validate_case(case, parameters):
    if not isinstance(case,dict) or set(case) not in ({"arguments","expected"}, {"arguments","error"}):
        raise ValueError("each case needs arguments and exactly one of expected or error")
    args = case["arguments"]
    if not isinstance(args,dict) or set(args) != set(parameters):
        raise ValueError("arguments must contain exactly the declared parameters")
    types = {"string":(str,), "number":(float,int), "boolean":(bool,)}
    for key, kind in parameters.items():
        if type(args[key]) not in types[kind]:
            raise ValueError(f"argument {key} must be {kind}")
        if kind == "number":
            finite_number(args[key])
    if "expected" in case:
        if not isinstance(case["expected"],dict) or not case["expected"]:
            raise ValueError("expected must assert at least one evidence field")
    elif not isinstance(case["error"],str) or not CODE.fullmatch(case["error"]):
        raise ValueError("expected a stable provider error code")


def equal_value(a,b):
    # JSON numbers share one numeric domain, but true is not the number 1.
    if type(a) in (int,float) and type(b) in (int,float):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a,dict):
        return a.keys() == b.keys() and all(equal_value(a[key],b[key]) for key in a)
    if isinstance(a,list):
        return len(a)==len(b) and all(equal_value(x,y) for x,y in zip(a,b))
    return a == b


def verify(case, cap, row):
    data = row["receipt"]
    if "error" in case:
        error = data.get("error")
        if (data.get("state") not in ("FAILED","BLOCKED") or not isinstance(error,dict)
                or error.get("code") != case["error"]):
            raise ValueError("terminal failure or stable error code differs from the fixture")
        return
    if not 200 <= row["http_status"] < 300 or data.get("state") != "SUCCEEDED":
        raise ValueError("expected a successful terminal receipt")
    evidence = data.get("evidence")
    if not isinstance(evidence,dict):
        raise ValueError("expected evidence fields")
    for field in cap.get("outcomes",{}).values():
        if field not in evidence or evidence[field] in (None,""):
            raise ValueError(f"missing evidence for declared outcome field {field}")
    for field, value in case["expected"].items():
        if field not in evidence or not equal_value(evidence[field],value):
            raise ValueError(f"independent expectation failed for evidence field {field}")


def validate_manifest(cap):
    if not isinstance(cap, dict) or cap.get("effect") != "read" or cap.get("effects", "read") != "read":
        raise ValueError("only an explicitly read-only capability can be tested")
    if not isinstance(cap.get("id"), str) or not NAME.fullmatch(cap["id"]):
        raise ValueError("invalid capability id")
    if not isinstance(cap.get("version"), str) or not cap["version"]:
        raise ValueError("expected a bound version")
    protocol = cap.get("protocol", "tool-v1")
    if protocol == "tool-v1":
        address(cap.get("endpoint"))
        params = cap.get("parameters")
        if not isinstance(params, dict) or not all(t in ("string", "number", "boolean") for t in params.values()):
            raise ValueError("invalid Tool parameter types")
        outcomes = cap.get("outcomes", {})
        if not isinstance(outcomes, dict) or not all(isinstance(v, str) and v for v in outcomes.values()):
            raise ValueError("invalid Tool outcome fields")
    elif protocol != "http-get":
        raise ValueError("unknown conformance protocol")
    return protocol


def address(url):
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError("expected an HTTP endpoint URL")
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.fragment):
        raise ValueError("expected an HTTP(S) endpoint without credentials or fragment")
    return parts


def get_url(base, path):
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError("GET fixtures need a same-origin absolute path")
    origin, resource = address(base), urlsplit(path)
    if resource.netloc or resource.fragment or any(ord(c) <= 32 for c in path):
        raise ValueError("invalid read-only resource path")
    return urlunsplit((origin.scheme, origin.netloc, resource.path, resource.query, ""))


def validate_get(case, url):
    if not isinstance(case,dict) or not {"path","expected"} <= case.keys() <= {"path","expected","http_status"}:
        raise ValueError("each GET fixture needs path, expected and optional http_status")
    get_url(url, case["path"])
    status = case.get("http_status", 200)
    if type(status) is not int or not (200 <= status < 300 or 400 <= status < 500 and status != 429):
        raise ValueError("expected a successful or terminal client-error HTTP status")
    if not isinstance(case["expected"], dict) or not case["expected"]:
        raise ValueError("a GET fixture needs an independent field expectation")


def check_fields(actual, fields):
    for key, expected in fields.items():
        if not isinstance(actual, dict) or key not in actual or not equal_value(actual[key], expected):
            raise ValueError(f"independent expectation failed for field {key}")


def operation(url, cap, case, op):
    if cap.get("protocol") == "http-get":
        return Request(get_url(url, case["path"]), headers={"X-Operation-Id":op})
    body = {"version":cap["version"], "arguments":case["arguments"], "operation_id":op}
    return Request(url, data=json.dumps(body, allow_nan=False).encode(), headers={"Content-Type":"application/json"})


def read_json(opener, req, timeout):
    try:
        response = opener.open(req, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read(MAX_BYTES+1)
        length = response.headers.get("Content-Length")
        if length is not None and len(raw) != int(length):
            raise IncompleteRead(raw)
        return {"http_status":response.status, "receipt":decode(raw)}


def receipt(opener, req, op, attempts, timeout):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        row = read_json(opener, req, max(.001, until-time.monotonic()))
        attempts.append(row)
        code, data = row["http_status"], row["receipt"]
        if 300 <= code < 400 or code == 429 or code >= 500:
            raise ValueError(f"HTTP {code} is not a terminal provider receipt")
        if req.get_method() == "GET":
            return row
        if not isinstance(data,dict) or data.get("operation_id") != op:
            raise ValueError("receipt did not identify the original operation")
        if data.get("state") != "RUNNING":
            return row
        if code >= 400:
            raise ValueError("error HTTP status with a running receipt")
        time.sleep(min(.2, max(0, until-time.monotonic())))
    raise TimeoutError("operation did not reach a terminal state within its deadline")


def check_result(cap, case, row):
    if cap.get("protocol") == "http-get":
        if row["http_status"] != case.get("http_status", 200):
            raise ValueError("unexpected GET status")
        check_fields(row["receipt"], case["expected"])
    else:
        verify(case, cap, row)


def same_result(cap, case, first, replay):
    check_result(cap, case, first)
    check_result(cap, case, replay)
    # GET is a view, with no promise that unrelated live fields are immutable.
    if cap.get("protocol") != "http-get" and not equal_value(first, replay):
        raise ValueError("replaying the operation changed its terminal receipt")


def check_health(opener, url, fixture, timeout):
    row = read_json(opener, Request(get_url(url, fixture["path"])), timeout)
    data = row["receipt"]
    if row["http_status"] != 200 or not isinstance(data, dict) or not data:
        raise ValueError("health resource did not return a JSON object with HTTP 200")
    if (data.get("ok") is False or data.get("healthy") is False or data.get("error")
            or data.get("status") in ("unhealthy","failed","offline","down","error","unavailable")):
        raise ValueError("provider reported unhealthy")
    check_fields(data, fixture.get("expected", {}))
    return row


def tool_refusal(opener, url, cap, arguments, op, timeout):
    req = operation(url, cap, {"arguments":arguments}, op)
    attempts = []
    first = receipt(opener, req, op, attempts, timeout)
    data = first["receipt"]
    error = data.get("error")
    if (data.get("state") not in ("FAILED", "BLOCKED") or not isinstance(error, dict)
            or not isinstance(error.get("code"), str) or not CODE.fullmatch(error["code"])):
        raise ValueError("invalid request must yield a terminal refusal and stable error code")
    replay = receipt(opener, req, op, attempts, timeout)
    # Some v1 providers create an audit correlation ID even for pre-admission errors.
    verify({"error":error["code"]}, cap, replay)
    if replay["http_status"] != first["http_status"]:
        raise ValueError("refusal status changed on replay")
    return {"operation_id":op, "attempts":attempts}


def check_view_version(opener, url, rule, version, timeout):
    if not isinstance(rule, dict) or set(rule) != {"path","field"} or not isinstance(rule["field"], list) or not rule["field"]:
        raise ValueError("version_check needs path and non-empty field path (keys or array indices)")
    row = read_json(opener, Request(get_url(url, rule["path"])), timeout)
    value = row["receipt"]
    if row["http_status"] != 200:
        raise ValueError("version resource unavailable")
    for key in rule["field"]:
        if isinstance(value, dict) and isinstance(key, str) and key in value:
            value = value[key]
        elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
            value = value[key]
        else:
            raise ValueError("version field absent")
    def bind(required):
        if not equal_value(required, value):
            raise ValueError("incompatible representation version")
    bind(version)
    wrong = version + "-mismatch"
    try:
        bind(wrong)
    except ValueError:
        return {"mode":"client_binding", "observed":value, "rejected":wrong, "field":rule["field"]}
    raise ValueError("view accepted the wrong binding version")


def timeout_recovery(opener, req, op, cap, case, timeout):
    """Relay the real request; hold its HTTP reply until the caller has timed out."""
    upstream = []
    failures = []
    received, release = Event(), Event()
    class DelayedReply(BaseHTTPRequestHandler):
        def relay(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if self.command != req.get_method() or self.rfile.read(size) != (req.data or b""):
                    raise ValueError("wrong request at the deadline relay")
                receipt(opener, req, op, upstream, timeout)
            except ERRORS as exc:
                failures.append(exc)
            finally:
                received.set()
                release.wait(timeout)
            self.close_connection = True
        do_POST = do_GET = relay
        def log_message(self, *args):
            pass
    with HTTPServer(("127.0.0.1",0), DelayedReply) as server:
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()
        gated = Request(f"http://127.0.0.1:{server.server_port}/", data=req.data, headers=dict(req.header_items()))
        try:
            try:
                read_json(opener, gated, min(timeout,.25))
            except TimeoutError:
                pass
            else:
                raise ValueError("deadline probe received a reply before the timeout")
            if not received.wait(timeout) or failures:
                raise ValueError(f"deadline relay did not receive a provider result: {failures}")
        finally:
            release.set()
            thread.join(timeout + .5)
    attempts = []
    recovered = receipt(opener, req, op, attempts, timeout)
    same_result(cap, case, upstream[-1], recovered)
    return {"operation_id":op, "fault":"withheld_http_reply", "client_result":"timeout",
            "upstream":upstream, "recovery":attempts}


def listening(parts):
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=.3):
            return True
    except OSError:
        return False


@contextmanager
def managed_process(argv, url, timeout):
    parts = address(url)
    if parts.hostname != "localhost" and not ipaddress.ip_address(parts.hostname).is_loopback:
        raise ValueError("a managed provider must listen on loopback")
    if listening(parts):
        raise ValueError("managed provider endpoint already has a listener")
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=sys.stderr, shell=False) as proc:
        try:
            deadline = time.monotonic() + timeout
            while proc.poll() is None and time.monotonic() < deadline:
                if listening(parts):
                    break
                time.sleep(.05)
            else:
                raise ValueError("managed provider failed to listen within its startup budget")
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=timeout)


def run_check(report, name, action, *args):
    try:
        detail = action(*args)
    except ERRORS as exc:
        report["checks"][name] = {"status":"failed", "failure":str(exc)}
    else:
        report["checks"][name] = {"status":"passed", **detail}


def exercise(args, report, registrar):
    address(args.url)
    if not math.isfinite(args.timeout) or not .5 <= args.timeout <= 300:
        raise ValueError("timeout must be between 0.5 and 300 seconds")
    cap, report["manifest_sha256"] = load_cases_or_manifest(args.manifest)
    protocol = validate_manifest(cap)
    if registrar:
        cap = registrar(cap)
    cases, report["cases_sha256"] = load_cases_or_manifest(args.cases)
    suite = cases if isinstance(cases, dict) else {"cases":cases}
    if suite.keys() - {"cases","health","version_check"}:
        raise ValueError("unrecognized conformance fixture fields")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("provide a non-empty list of cases")
    health = suite.get("health", {"path":"/health"})
    if (not isinstance(health, dict) or "path" not in health or health.keys() - {"path","expected"}
            or not isinstance(health.get("expected", {}),dict)):
        raise ValueError("health needs path and optional expected fields")
    get_url(args.url, health["path"])
    view = protocol == "http-get"
    for case in cases:
        if view:
            validate_get(case, args.url)
        else:
            validate_case(case, cap["parameters"])
    positives = [i for i,c in enumerate(cases) if "expected" in c and c.get("http_status",200) < 300]
    negatives = [i for i,c in enumerate(cases) if c.get("http_status",200) >= 400]
    if not positives:
        raise ValueError("at least one successful evidence assertion is required")
    if view and (not negatives or "version_check" not in suite):
        raise ValueError("GET views need a real invalid-request case and an explicit representation version_check")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    report.update(id=cap["id"], version=cap["version"], binding=binding(cap), test_url=args.url, protocol=protocol)
    prefix = f"provider-contract-{report['run_id']}"
    selected = positives[0]
    sample = cases[selected]
    operation_id = f"{prefix}/case/{selected+1}"
    accepted = operation(args.url, cap, sample, operation_id)
    if args.process == []:
        raise ValueError("--process needs an explicit executable and arguments")
    with managed_process(args.process, args.url, args.timeout) if args.process else nullcontext() as process:
        if not registrar:
            run_check(report, "health", check_health, opener, args.url, health, args.timeout)
        for index, case in enumerate(cases, 1):
            op = f"{prefix}/case/{index}"
            item = {"index":index, "operation_id":op, "fixture":case, "attempts":[], "passed":False}
            report["cases"].append(item)
            req = operation(args.url, cap, case, op)
            try:
                first = receipt(opener, req, op, item["attempts"], args.timeout)
                check_result(cap, case, first)
                replay = receipt(opener, req, op, item["attempts"], args.timeout)
                same_result(cap, case, first, replay)
                item["passed"] = True
            except ERRORS as exc:
                item["failure"] = str(exc)
        if registrar:
            # Existing scripts accept all previously valid v1 providers, including providers without /health.
            report["passed"] = all(c["passed"] for c in report["cases"])
            return
        if view:
            run_check(report, "version", check_view_version, opener, args.url, suite["version_check"],
                      cap["version"], args.timeout)
            ok = all(report["cases"][i]["passed"] for i in negatives)
            report["checks"]["invalid_arguments"] = {"status":"passed" if ok else "failed",
                                                      "case_indices":[i+1 for i in negatives]}
        else:
            run_check(report, "version", tool_refusal, opener, args.url, {**cap,"version":cap["version"]+"-mismatch"},
                      sample["arguments"], prefix+"/wrong-version", args.timeout)
            # Non-object arguments are outside every v1 Tool schema, including zero-parameter tools.
            run_check(report, "invalid_arguments", tool_refusal, opener, args.url, cap, [],
                      prefix+"/invalid-arguments", args.timeout)
        op = prefix+"/timeout"
        run_check(report, "timeout", timeout_recovery, opener, operation(args.url, cap, sample, op),
                  op, cap, sample, args.timeout)
        if process is not None:
            if process.poll() is not None:
                raise ValueError("managed provider exited before the crash test")
            first_pid = process.pid
    if args.process:
        def replay_after_kill():
            if listening(address(args.url)):
                raise ValueError("managed provider's endpoint still listens after the forced exit")
            if not report["cases"][selected]["passed"]:
                raise ValueError("initial operation has no verified receipt to reconcile")
            attempts = []
            with managed_process(args.process, args.url, args.timeout) as replacement:
                health_row = check_health(opener, args.url, health, args.timeout)
                row = receipt(opener, accepted, operation_id, attempts, args.timeout)
                same_result(cap, sample, report["cases"][selected]["attempts"][-1], row)
                return {"operation_id":operation_id, "terminated_pid":first_pid, "exit_code":process.returncode,
                        "endpoint_closed":True, "replacement_pid":replacement.pid, "health":health_row,
                        "recovery":attempts}
        run_check(report, "lifecycle", replay_after_kill)
    else:
        report["checks"]["lifecycle"] = {"status":"not_run", "reason":"no locally managed provider process"}
    report["passed"] = (all(item["passed"] for item in report["cases"])
                        and all(check["status"] != "failed" for check in report["checks"].values()))
    report["all_checks_passed"] = report["passed"] and report["checks"]["lifecycle"]["status"] == "passed"


def main(argv=None, registrar=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=registrar is not None)
    parser.add_argument("--timeout", type=float, default=15, help="per-operation and startup budget in seconds")
    parser.add_argument("--process", nargs=argparse.REMAINDER, help="start and crash/restart this local argv (last option)")
    args = parser.parse_args(argv)
    if registrar and args.process is not None:
        parser.error("managed lifecycle checks use python -m edge_contracts.conformance")
    report = {"run_id":uuid.uuid4().hex, "utc":datetime.now(timezone.utc).isoformat(),
              "passed":False, "cases":[], "checks":{}}
    try:
        exercise(args, report, registrar)
    except REPORT_ERRORS as exc:
        report["failure"] = str(exc)
    if not registrar:
        report["complete"] = bool(report.get("all_checks_passed"))
        report["status"] = "failed" if not report["passed"] else ("passed" if report["complete"] else "partial")
    raw = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.out:
        args.out.parent.mkdir(exist_ok=True, parents=True)
        args.out.write_text(raw+"\n", encoding="utf-8")
    sys.stdout.buffer.write((raw+"\n").encode("utf-8"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
