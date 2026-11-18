"""Send a read-only sensor diagnosis through the campus Agent and read its durable Session."""
import argparse
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler, Request


def verify(base, report):
    http = build_opener(ProxyHandler({}))

    def api(path, body=None):
        request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json"})
        with http.open(request, timeout=8) as response:
            return json.load(response)

    def wait_task(task_id):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            task = api("/agent/tasks?" + urlencode({"id": task_id}))
            if task.get("state") in {"SUCCEEDED", "BLOCKED", "FAILED", "RECOVERY_REQUIRED", "CANCELED"}:
                return task
            time.sleep(.25)
        raise TimeoutError(task_id)

    for key, phrase in (("configured", "检查导航传感器"), ("unknown", "检查隔壁传感器")):
        task_id = "field-health-" + uuid.uuid4().hex[:12]
        body = {"id": task_id, "text": phrase}
        api("/agent/tasks", body)
        row = wait_task(task_id)
        trace = row["trace_id"]
        record = report[key] = {"id": task_id, "trace": trace, "text": phrase, "state": row["state"],
                                "steps": row["steps"], "detail": row.get("detail")}
        if key == "configured":
            assert row["state"] == "SUCCEEDED" and len(row["steps"]) == 1, record
            step = row["steps"][0]
            assert step["tool"] == "diagnose_sensors" and step["evidence"]["scope"] == "current_observation"
            samples = step["evidence"]["observations"]
            assert {"rgb", "depth", "scan", "imu"} <= {sample["input"] for sample in samples}
            assert all(sample["frame"] and sample["stamp_ns"] > 0 and sample["metrics"] for sample in samples)
            api("/agent/tasks", body)
            assert wait_task(task_id)["steps"] == row["steps"], "the repeated request changed the retained measurement"
        else:
            assert row["state"] in {"BLOCKED", "FAILED"} and all(not s.get("evidence") for s in row["steps"]), record
        session = api("/api/session?" + urlencode({"trace_id": trace}))
        deadline = time.monotonic() + 10
        while True:
            events = api("/api/events?" + urlencode({"task_id": task_id, "limit": 100}))["events"]
            if any(event["kind"].startswith("agent.") for event in events):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Agent events missing from archive for " + task_id)
            time.sleep(.2)
        assert session["scope"]["task_id"] == task_id and session["markdown"] and events
        record.update(session_url=base + "/#sessions?task=" + task_id, headline=session["headline"],
                      event_seqs=[event["seq"] for event in events])
    report["passed"] = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", default="http://127.0.0.1:8766")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = {"passed": False}
    try:
        verify(args.console.rstrip("/"), result)
    finally:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
