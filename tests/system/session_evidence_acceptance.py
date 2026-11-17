"""Exercise session -> evidence plugin -> cited source using an isolated Gazebo project."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client"))
from register_evidence import register


def run(base, project, registry, out):
    http = build_opener(ProxyHandler({}))
    def request(url, body=None):
        req = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type":"application/json"})
        with http.open(req, timeout=12) as response:
            return json.load(response)
    def api(path, body=None):
        return request(base.rstrip("/") + path, body)
    def compose(*args):
        return subprocess.check_output(["docker","compose","-p",project,"-f",str(ROOT/"compose.yaml"),*args],
                                       cwd=ROOT, text=True).strip()
    def wait_for(call, ready, timeout=35):
        deadline=time.monotonic()+timeout
        while True:
            value=call()
            if ready(value):
                return value
            if time.monotonic()>deadline:
                raise TimeoutError(str(value)[:500])
            time.sleep(.25)
    if api("/api/state")["environment"]!="simulation":
        raise ValueError("Only run this task-writing acceptance in an isolated simulation")
    endpoint="http://"+compose("port","evidence","8125")
    issues=api("/api/review/tasks?state=issues&limit=100")["tasks"]
    blocked=next((t for t in issues if "navigation.path_blocked" in t["detail"]),None)
    if not blocked:
        raise AssertionError("Run semantic acceptance first to create a real blocked navigation")
    task_id=blocked["id"]
    session=api("/api/session?"+urlencode({"task_id":task_id}))
    raw=api("/api/events?"+urlencode({"task_id":task_id,"after":0,"limit":500}))["events"]
    braking=next(row for row in raw if row["kind"]=="action.braking")
    run_id="evidence-"+uuid.uuid4().hex[:10]
    def invoke(tool, arguments, suffix):
        return request(endpoint+"/invoke?"+urlencode({"tool":tool}),
                       {"operation_id":run_id+"/"+suffix,"version":"1","arguments":arguments,
                        "trace":{"trace_id":run_id,"task_id":run_id}})
    summary=invoke("read_session",{"task_id":task_id},"session")
    assert summary["state"]=="SUCCEEDED", summary
    # A background event can advance the global archive cutoff between GETs.
    assert summary["evidence"]["scope"]==session["scope"]
    for message in session["messages"]:
        assert message["text"] in summary["evidence"]["markdown"]
    schema=invoke("evidence_schema",{},"schema")
    assert schema["state"]=="SUCCEEDED", schema
    sql="SELECT seq, kind, json_extract(data_json, '$.front_range_m') AS distance_m FROM events WHERE kind='action.braking'"
    arguments={"task_id":task_id,"sql":sql,"after_seq":0,"limit":5,"archive_id":""}
    result=invoke("query_evidence",arguments,"distance")
    assert result["state"]=="SUCCEEDED", result
    evidence=result["evidence"]
    row=evidence["rows"][0]
    distance=row["distance_m"] if isinstance(row,dict) else row[evidence["columns"].index("distance_m")]
    seq=row["seq"] if isinstance(row,dict) else row[evidence["columns"].index("seq")]
    assert distance==braking["data"]["front_range_m"] and seq==braking["seq"]
    assert invoke("query_evidence",arguments,"distance")==result
    refused=invoke("query_evidence",{**arguments,"sql":"DELETE FROM events"},"write")
    assert refused["state"]=="BLOCKED", refused
    assert api("/agent/tasks?"+urlencode({"id":task_id}))["state"]==blocked["state"]
    # Install only into the runner-owned directory and let the normal Agent discover it.
    registry=Path(registry).resolve()
    if not registry.is_relative_to((ROOT/".ci-results").resolve()):
        raise ValueError("Test registry must be inside the project's .ci-results directory")
    register(registry,"http://evidence:8125")
    try:
        caps=api("/agent/capabilities")
        assert "read_session" in json.dumps(caps)
        accepted=api("/agent/tasks",{"id":run_id,"text":"复盘任务 "+task_id})
        completed=wait_for(lambda:api("/agent/tasks?"+urlencode({"id":accepted["id"]})),
                          lambda t:t["state"] in ("SUCCEEDED","BLOCKED","RECOVERY_REQUIRED"))
        assert completed["state"]=="SUCCEEDED", completed["detail"]
        assert completed["steps"][0]["tool"]=="read_session"
        reviewed=completed["steps"][0]["evidence"]
        assert reviewed["scope"]["task_id"]==task_id
        # The global archive cutoff advances while the diagnostic task is running.
        for message in session["messages"]:
            assert message["text"] in reviewed["markdown"]
        assert session["diagnosis"]["code"] in reviewed["markdown"]
    finally:
        register(registry,"http://evidence:8125",remove=True)
    assert "read_session" not in json.dumps(api("/agent/capabilities"))
    # Audit facts must reach the public bus, independently of the task's original archive.
    audit=wait_for(lambda:api("/api/events?"+urlencode({"module":"evidence.query","trace_id":run_id,"limit":100})),
                   lambda page:len(page["events"])>=6)
    compact=len(session["markdown"].encode())
    full=len(json.dumps(raw,ensure_ascii=False).encode())
    assert compact<full, (compact,full)
    report={"passed":True,"task_id":task_id,"source_seq":seq,"distance_m":distance,
            "source_matches":True,"session_markdown_bytes":compact,"raw_task_event_bytes":full,
            "byte_reduction_fraction":round(1-compact/full,4),"tool_registered_then_removed":True,
            "natural_language_review":"SUCCEEDED","duplicate_receipt_equal":True,
            "write_query_rejected":True,"audit_rows":len(audit["events"])}
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",required=True)
    parser.add_argument("--project",required=True)
    parser.add_argument("--registry",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    run(args.url,args.project,args.registry,args.out)
