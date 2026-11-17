"""Check a trusted read-only HTTP tool against independent fixtures in a disposable test deployment."""
import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException, IncompleteRead
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"agent"))
from planning import CODE, binding, finite_number, registry

MAX_BYTES = 2_000_000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def decode(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("JSON exceeds the runtime response limit")
    return json.loads(raw.decode("utf-8-sig"), parse_float=finite_number, parse_constant=finite_number)


def load_cases_or_manifest(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES+1)
    return decode(raw), hashlib.sha256(raw).hexdigest()


def registered(manifest):
    if not isinstance(manifest,dict) or manifest.get("effect") != "read" or not manifest.get("endpoint"):
        raise ValueError("a read-only HTTP tool manifest is required")
    # Isolate discovery from the host's other deployments and exercise the actual loader.
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder)/"provider.json").write_text(json.dumps(manifest,allow_nan=False),encoding="utf-8")
        with patch.dict(os.environ, {"EDGE_PLUGIN_DIR":folder, "EDGE_EXTRA_PLUGIN_DIR":""}), redirect_stdout(sys.stderr):
            cap = registry().get(manifest.get("id"))
    if cap is None or cap != manifest:
        raise ValueError("the production registry did not register this manifest in full")
    return cap


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


def receipt(opener, url, request_body, attempts):
    until = time.monotonic()+15
    request = Request(url,data=json.dumps(request_body,allow_nan=False).encode(),headers={"Content-Type":"application/json"})
    while time.monotonic() < until:
        try:
            response = opener.open(request,timeout=max(.1, until-time.monotonic()))
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(MAX_BYTES+1)
            declared = response.headers.get("Content-Length")
            if declared is not None and len(raw) != int(declared):
                raise IncompleteRead(raw)
            row = {"http_status":response.status, "receipt":decode(raw)}
        attempts.append(row)
        data = row["receipt"]
        if not isinstance(data,dict) or data.get("operation_id") != request_body["operation_id"]:
            raise ValueError("receipt did not identify the original operation")
        if 300 <= row["http_status"] < 400 or row["http_status"] == 429 or row["http_status"] >= 500:
            raise ValueError(f'HTTP {row["http_status"]} is not a terminal provider receipt')
        if data.get("state") != "RUNNING":
            return row
        if row["http_status"] >= 400:
            raise ValueError("error HTTP status with a running receipt")
        time.sleep(.2)
    raise TimeoutError("operation did not reach a terminal state within 15 seconds")


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


def exercise(args, report):
    address = urlsplit(args.url)
    if address.scheme not in ("http","https") or not address.hostname or address.username or address.password:
        raise ValueError("provide the HTTP(S) invocation URL of the isolated provider")
    manifest, report["manifest_sha256"] = load_cases_or_manifest(args.manifest)
    cap = registered(manifest)
    report.update(id=cap["id"], version=cap["version"], binding=binding(cap), test_url=args.url)
    cases, report["cases_sha256"] = load_cases_or_manifest(args.cases)
    if not isinstance(cases,list) or not cases:
        raise ValueError("provide a non-empty list of cases")
    for case in cases:
        validate_case(case, cap["parameters"])
    if not any("expected" in case for case in cases):
        raise ValueError("at least one successful evidence assertion is required")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for index, case in enumerate(cases,1):
        op = f"provider-contract-{report['run_id']}/case/{index}"
        body = {"arguments":case["arguments"], "version":cap["version"], "operation_id":op}
        item = {"index":index, "operation_id":op, "fixture":case, "attempts":[], "passed":False}
        report["cases"].append(item)
        try:
            first = receipt(opener,args.url,body,item["attempts"])
            verify(case,cap,first)
            replay = receipt(opener,args.url,body,item["attempts"])
            verify(case,cap,replay)
            if not equal_value(first,replay):
                raise ValueError("replaying the operation changed its terminal receipt")
            item["passed"] = True
        except (OSError, ValueError, HTTPException, RecursionError) as exc:
            item["failure"] = str(exc)
    report["passed"] = all(row["passed"] for row in report["cases"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest",type=Path,required=True)
    parser.add_argument("--cases",type=Path,required=True)
    parser.add_argument("--url",required=True)
    parser.add_argument("--out",type=Path,required=True)
    args = parser.parse_args()
    report = {"run_id":uuid.uuid4().hex, "utc":datetime.now(timezone.utc).isoformat(), "passed":False, "cases":[]}
    try:
        exercise(args,report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as exc:
        report["failure"] = str(exc)
    raw = json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)
    args.out.parent.mkdir(exist_ok=True,parents=True)
    args.out.write_text(raw+"\n",encoding="utf-8")
    print(raw)
    return 0 if report["passed"] else 1


if __name__=="__main__":
    sys.exit(main())
