"""Exercise a scene-provider restart while a dedicated simulation console is offline."""
import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener


def run(project):
    compose = ["docker", "compose", "-p", project]
    def command(*args, source=None):
        return subprocess.run([*compose, *args], input=source, capture_output=True, check=True,
                              encoding="utf-8").stdout.strip()

    def endpoint():
        address = command("port", "console", "8080")
        if not address.startswith("127.0.0.1:"):
            raise ValueError("use a dedicated simulation console bound to loopback")
        return "http://" + address
    if project == "edge-agent-ros":
        raise ValueError("use an isolated acceptance Compose project")
    base = endpoint()
    http = build_opener(ProxyHandler({}))
    def get(path):
        with http.open(base + path, timeout=3) as response:
            return json.load(response)
    if get("/api/state")["environment"] != "simulation":
        raise ValueError("the target must be a Gazebo simulation")

    operation = "outage-" + uuid.uuid4().hex
    request = {"version":"1", "operation_id":operation, "arguments":{"place":"unmapped-outage-probe"},
               "trace":{"trace_id":"parent-" + operation, "task_id":operation,
                        "trigger":{"kind":"agent","source":"recovery-acceptance","id":operation}}}
    submit = f"""import json, time
from urllib.request import Request, urlopen
from urllib.error import URLError
body = {json.dumps(request).encode()!r}
until = time.monotonic() + 15
while True:
    try:
        with urlopen(Request("http://localhost:8092/navigate", data=body), timeout=2) as r:
            print(r.read().decode())
        break
    except URLError:
        if time.monotonic() > until:
            raise
        time.sleep(.2)
"""
    def in_scene(script):
        return json.loads(command("exec","-T","scene","python3","-",source=script))

    read = f"""import json, sqlite3
db = sqlite3.connect("/var/lib/edge-scene/routes.db")
row = db.execute("SELECT event_id,delivered,body FROM decisions WHERE op=?", ({operation!r},)).fetchone()
print(json.dumps(row))
"""
    try:
        command("stop", "console")
        refusal = in_scene(submit)
        assert refusal["error"]["code"] == "scene.place_unknown"
        pending = in_scene(read)
        assert pending[1] == 0
        command("restart", "scene")
        assert in_scene(submit) == refusal
        assert in_scene(read) == pending
    finally:
        command("start", "console")

    base = endpoint()
    until = time.monotonic() + 50
    while time.monotonic() < until:
        try:
            stored = in_scene(read)
            events = get("/api/events?" + urlencode({"event_id":pending[0]}))["events"]
            if stored[1] == 1 and events:
                break
        except OSError:
            pass
        time.sleep(.3)
    else:
        raise RuntimeError("restored console did not acknowledge the decision")
    assert len(events) == 1 and events[0]["module"] == "scene.navigation"
    assert events[0]["source"] == "ros:/edge/events" and events[0]["trace_id"] == request["trace"]["trace_id"]
    assert events[0]["utc"] == json.loads(pending[2])["utc"] and stored[2] == pending[2]
    assert not get("/api/events?" + urlencode({"mission_id":operation,"kind":"mission.created"}))["events"]
    return {"operation_id":operation,"event_id":pending[0],"trace_id":events[0]["trace_id"],
            "module":events[0]["module"],"code":events[0]["code"],"source":events[0]["source"],
            "utc":events[0]["utc"],"pending_before_and_after_restart":True,"same_receipt_on_retry":True,
            "source_payload_unchanged":True,"archive_rows_after_reconnect":len(events),"delivery_acknowledged":True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="isolated project whose console may briefly stop")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    evidence = json.dumps(run(args.project), ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(exist_ok=True, parents=True)
        args.out.write_text(evidence, encoding="utf-8")
    print(evidence)
