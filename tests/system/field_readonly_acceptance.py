"""Exercise the campus workbench without requesting movement; retain public replay IDs."""
import argparse
import json
import math
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
import uuid


def verify(base):
    http = build_opener(ProxyHandler({}))

    def api(path, body=None):
        raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request = Request(base + path, data=raw, headers={"Content-Type": "application/json"})
        with http.open(request, timeout=5) as response:
            return json.load(response)

    def pose():
        state = api("/api/state")
        assert state["environment"] == "simulation" and state["mode"] == "live"
        truth = state["sources"]["truth"]
        assert truth["fresh"]
        return {"session": state["session_id"], "stamp_s": truth["stamp_s"],
                "xy": [truth["data"]["x"], truth["data"]["y"]]}

    origin = pose()
    catalog = {c["id"]: c for c in api("/agent/capabilities")["capabilities"]}
    for key in ("status", "describe_scene", "preview_route"):
        assert catalog[key]["effect"] == "read"
    assert all("image" not in c.get("outcomes", {}) for c in catalog.values())
    cases = [
        ("status", "查询机器人状态", "status", "SUCCEEDED"),
        ("scene", "查询场景地图", "describe_scene", "SUCCEEDED"),
        ("route", "只规划到教学楼门外的路线", "preview_route", "SUCCEEDED"),
        ("photo", "拍一张照片", None, "BLOCKED"),
    ]
    rows = []
    token = "edge-field-" + uuid.uuid4().hex[:10]
    for name, text, expected_tool, state in cases:
        key = f"{token}-{name}"
        payload = {"id": key, "text": text, "trigger": {"kind":"user","source":"field-readonly-test","id":key}}
        api("/agent/tasks", payload)
        query = urlencode({"id":key})
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            task = api("/agent/tasks?" + query)
            events = api("/api/events?" + urlencode({"task_id":key,"limit":200}))["events"]
            if task["state"] in {"SUCCEEDED","BLOCKED","FAILED","RECOVERY_REQUIRED","CANCELED"} and any(
                    e["kind"] == ("agent.verified" if state=="SUCCEEDED" else "agent.blocked")
                    and e["trace_id"] == task["trace_id"] for e in events):
                break
            time.sleep(.3)
        else:
            raise TimeoutError(f"{key}: task or matching terminal event missing")
        assert task["state"] == state, (text, task["detail"])
        assert [s["tool"] for s in task["steps"]] == ([expected_tool] if expected_tool else [])
        assert events and all(e["kind"] != "mission.created" for e in events)
        rows.append({"task":task,"session_url":base+"/#sessions?"+urlencode({"task":key}),
                     "review_url":base+"/#review?"+urlencode({"task":key}),"events":events})
    after = pose()
    assert origin["session"] == after["session"] and after["stamp_s"] > origin["stamp_s"]
    assert math.dist(origin["xy"],after["xy"]) < .01
    return {"passed":True,"before":origin,"after":after,"tasks":rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    evidence = verify(args.url.rstrip("/"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps([row["task"]["id"] for row in evidence["tasks"]]))
