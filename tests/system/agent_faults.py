"""Black-box fault tests for one isolated Agent, Gazebo/ROS core and HTTP providers."""
import argparse
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


def serve_provider():
    """Simulate named provider failures. A completed physical effect lives at the provider."""
    attempts = Counter()
    results = {}
    lock = threading.Lock()
    release = threading.Event()

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_json(self, value, status=200, cut=False):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data[:8] if cut else data)

        def do_GET(self):
            if self.path=="/release":
                release.set()
            with lock:
                self.send_json({"attempts": dict(attempts), "effects": dict(results)})

        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/plan":
                prompt = msg["request"]
                with lock:
                    attempts[prompt] += 1
                    count = attempts[prompt]
                if "retry" in prompt and count == 1:
                    return self.send_json({"detail": "provider starting"}, 503)
                if "invalid" in prompt:
                    return self.send_json({"steps": [{"tool": "unregistered", "args": {}}]})
                if prompt=="Take three instrument readings":
                    return self.send_json({"steps": [{"tool":"fixture","args":{}} for _ in range(3)]})
                if prompt=="Visit (1,0) and read the instrument":
                    return self.send_json({"steps": [{"tool":"fixture","args":{}}]})
                return self.send_json({"steps": [{"tool": "status", "args": {}}]})
            op = msg["operation_id"]
            with lock:
                attempts[op] += 1
                count = attempts[op]
                if op not in results:
                    results[op] = uuid.uuid4().hex
                receipt = {"operation_id": op, "state": "SUCCEEDED", "evidence": {"result": results[op]}}
                if op.startswith("wrong-id/"):
                    receipt["operation_id"] = "other/step/1"
                if op.startswith("bad-measurement/"):
                    receipt["evidence"]["result"] = float("nan")
                if op.startswith("missing-evidence/"):
                    receipt["evidence"] = {}
            if op=="long/step/2":
                release.wait(45)
            if op.startswith("slow-receipt/") and count==1:
                time.sleep(7)
            try:
                self.send_json(receipt, cut=op.startswith("lost-receipt/") and count == 1)
            except (BrokenPipeError,ConnectionResetError):
                pass  # First caller may have timed out or lost its process after execution.

    ThreadingHTTPServer(("0.0.0.0", 8123), Provider).serve_forever()


def require(value, message):
    if not value:
        raise AssertionError(message)


def run(image, out):
    repo = Path(__file__).resolve().parents[2]
    project = "edge-test-agent-" + uuid.uuid4().hex[:10]
    http = build_opener(ProxyHandler({}))
    report = {"utc": datetime.now(timezone.utc).isoformat(), "project": project, "image": image,
              "image_id": subprocess.check_output(["docker", "image", "inspect", image, "-f", "{{.Id}}"], text=True).strip(),
              "source_revision": subprocess.check_output(["docker","image","inspect",image,"-f",
                                  '{{index .Config.Labels "org.opencontainers.image.revision"}}'],text=True).strip(),
              "passed": False, "cases": {}}
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        (directory/"plugins").mkdir()
        (directory/"contracts").mkdir()
        (directory/"plugins"/"fixture.json").write_text(json.dumps({
            "id": "fixture", "effect": "idempotent", "version": "1", "parameters": {},
            "description": "test instrument", "endpoint": "http://provider:8123/tool",
            "outcomes": {"reading": "result"}}), encoding="utf-8")
        env = {**os.environ, "EDGE_IMAGE": image, "EDGE_ROS_DOMAIN_ID": "116", "EDGE_OLLAMA_URL": "",
               "EDGE_PLANNER_URL": "http://provider:8123/plan", "EDGE_EXTRA_PLUGIN_DIR": "",
               "EDGE_PLUGINS_DIR": str(directory/"plugins"), "EDGE_CONTRACTS_DIR": str(directory/"contracts"),
               "EDGE_RETRY_LIMIT": "2", "EDGE_TRACE_CONTENT": "redacted"}
        override = directory/"compose.json"
        override.write_text(json.dumps({"services": {
            "agent": {"ports": ["127.0.0.1::8090"]},
            "provider": {"image":image,"pull_policy":"never","command":["python3","/fixture.py","--fixture"],
                         "volumes":[{"type":"bind","source":str(Path(__file__).resolve()),"target":"/fixture.py","read_only":True}]}
        }}), encoding="utf-8")

        def compose(*args):
            cmd = ["docker","compose","-f",str(repo/"compose.yaml"),"-f",str(override),"-p",project,*args]
            result = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, encoding="utf-8",errors="replace",timeout=90)
            if result.returncode:
                raise RuntimeError(result.stdout[-400:] + result.stderr[-900:])
            return result.stdout.strip()

        def api(path, data=None):
            request = Request(base+path, data=json.dumps(data).encode() if data is not None else None,
                              headers={"Content-Type":"application/json"})
            with http.open(request,timeout=4) as response:
                return json.load(response)

        def terminal(key):
            deadline = time.monotonic()+25
            while time.monotonic()<deadline:
                row = api("/agent/tasks?"+urlencode({"id":key}))
                if row["state"] in {"SUCCEEDED","BLOCKED","RECOVERY_REQUIRED","CANCELED"}:
                    return row
                time.sleep(.15)
            raise TimeoutError(key)

        def submit(key, text):
            row = api("/agent/tasks", {"id":key,"text":text})
            require(row["id"] == key, "task ID mismatch")
            return terminal(key)

        def case(name, check):
            entry = report["cases"][name] = {"passed":False}
            try:
                check(entry)
                entry["passed"] = True
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
            print(name, entry["passed"], flush=True)

        def evidence(entry, row):
            entry.update(state=row["state"], steps=[s["tool"] for s in row["steps"]], detail=row["detail"],
                         codes=[json.loads(e["payload"])["trace"]["code"] for e in row["events"]])

        def physical(entry, key, requested, expected):
            op = key+"/step/1"
            preexisting = json.loads(compose("exec","-T","core","/usr/local/bin/edge-entrypoint","python3",
                      "/opt/edge/client/mission.py","run",op,"0,0","--wait").splitlines()[-1])
            entry["preexisting_evidence"] = preexisting["evidence"]
            row = submit(key, "inspect "+requested)
            evidence(entry, row)
            require(row["state"]==expected, "different goal reused or original goal lost")
            if expected=="BLOCKED":
                require("core.id_conflict" in entry["codes"], "missing payload conflict cause")
            else:
                require(row["steps"][0]["evidence"]["verified_count"]==1, "lost original proof")

        def provider_state(path="/"):
            return json.loads(compose("exec","-T","provider","python3","-c",
                "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8123"+path+"').read().decode())"))

        def tool(entry, key, expected, code=None):
            row = submit(key,"fixture")
            evidence(entry,row)
            counts=provider_state()
            op=key+"/step/1"
            entry.update(attempts=counts["attempts"].get(op), effect_ids=[counts["effects"][op]])
            require(row["state"]==expected,"unexpected terminal state")
            if code:
                require(code in entry["codes"], "missing layered diagnosis "+code)
            if key in ("lost-receipt","slow-receipt"):
                require(entry["attempts"]==2 and row["steps"][0]["evidence"]["result"]==entry["effect_ids"][0],
                        "lost original effect or operation identity")

        def model(entry, key, text, expected, code):
            row = submit(key,text)
            evidence(entry,row)
            entry["attempts"] = provider_state()["attempts"][text]
            require(row["state"]==expected and code in entry["codes"], "wrong planning outcome/diagnosis")
            if expected=="BLOCKED":
                require("agent.dispatch" not in entry["codes"],"invalid plan dispatched")
            else:
                require(entry["attempts"]==2 and row["steps"][0]["evidence"]["source"]=="/odom","retry lost actual observation")

        def concurrent(entry):
            trigger={"kind":"watchdog","source":"test-watch","id":"same-observation"}
            data={"trigger":trigger,"text":"status"}
            with ThreadPoolExecutor(max_workers=6) as pool:
                rows=list(pool.map(lambda _: api("/agent/tasks",data),range(12)))
            ids={r["id"] for r in rows}
            require(len(ids)==1,"concurrent duplicate acceptance")
            row=terminal(ids.pop())
            evidence(entry,row)
            require(entry["codes"].count("agent.dispatch")==1 and row["state"]=="SUCCEEDED","duplicate tool dispatch")
            try:
                api("/agent/tasks", {**data, "text":"inspect (1,0)"})
            except HTTPError as exc:
                entry["conflicting_status"]=exc.code
                require(exc.code==409,"wrong conflict response")
            else:
                raise AssertionError("conflicting intent accepted")

        def long_restart(entry):
            nonlocal base
            api("/agent/tasks",{"id":"long","text":"Take three instrument readings"})
            until=time.monotonic()+15
            while time.monotonic()<until:
                if "long/step/2" in provider_state()["effects"]:
                    break
            else:
                raise TimeoutError("provider did not commit second reading")
            before=api("/agent/tasks?id=long")
            require(before["cursor"]==1 and before["steps"][1]["operation_id"]=="long/step/2","wrong crash window")
            container=compose("ps","-q","agent")
            def started():
                return subprocess.check_output(["docker","inspect","-f","{{.State.StartedAt}}",container],text=True).strip()
            entry["started_before"]=started()
            compose("kill","-s","SIGKILL","agent")
            provider_state("/release")
            compose("start","agent")
            base="http://"+compose("port","agent","8090")
            until=time.monotonic()+25
            while True:
                try:
                    row=terminal("long")
                    break
                except OSError:
                    if time.monotonic()>=until:
                        raise
                    time.sleep(.2)
            entry["started_after"]=started()
            require(entry["started_before"]!=entry["started_after"],"agent restart not observed")
            provider=provider_state()
            entry.update(state=row["state"],cursor=row["cursor"],operations=[s["operation_id"] for s in row["steps"]],
                         attempts=[provider["attempts"][s["operation_id"]] for s in row["steps"]])
            require(row["state"]=="SUCCEEDED" and row["cursor"]==3 and entry["operations"]==[
                "long/step/1","long/step/2","long/step/3"], "task failed to resume its original steps")
            require(entry["attempts"][0]==1 and entry["attempts"][1]>=2 and entry["attempts"][2]==1,
                    "unexpected repeated or skipped step")
            require(all(s["evidence"]["result"]==provider["effects"][s["operation_id"]] for s in row["steps"]),
                    "supervisor did not reconcile the same effects")

        def offline(entry):
            nonlocal base
            env["EDGE_PLANNER_URL"]=""
            compose("up","-d","--no-deps","--no-build","agent")
            base="http://"+compose("port","agent","8090")
            # An independent Gazebo odometry bridge persists across the Agent restart.
            sample_script="""import rclpy, json
from nav_msgs.msg import Odometry
from rclpy.qos import qos_profile_sensor_data
rclpy.init()
node=rclpy.create_node('validation_world_sample')
rows=[]
node.create_subscription(Odometry,'/ground_truth',lambda m: rows.append(
 [m.pose.pose.position.x,m.pose.pose.position.y,m.twist.twist.linear.x]),qos_profile_sensor_data)
for _ in range(20):
    rclpy.spin_once(node,timeout_sec=.3)
    if rows: break
print(json.dumps(rows[-1]))
"""
            def world():
                return json.loads(compose("exec","-T","witness","/usr/local/bin/edge-entrypoint",
                                          "python3","-c",sample_script).splitlines()[-1])
            entry["before"]=world()
            for key,text,code in (("offline","Could you read back the robot heading?","planner.not_configured"),
                                  ("absent","请拍一张巡检照片","planner.capability_missing")):
                deadline=time.monotonic()+15
                while True:
                    try:
                        row=submit(key,text)
                        break
                    except OSError:
                        if time.monotonic()>=deadline:
                            raise
                        time.sleep(.2)
                details=entry[key]={}
                evidence(details,row)
                require(row["state"]=="BLOCKED" and code in details["codes"] and
                        "agent.dispatch" not in details["codes"] and not row["steps"], "offline contract failed")
            entry["after"]=world()
            require(abs(entry["before"][0]-entry["after"][0])<.02 and abs(entry["before"][1]-entry["after"][1])<.02
                    and abs(entry["after"][2])<.01,"unexpected Gazebo movement during refusal")

        def ledger_loss(entry):
            nonlocal base
            key="lost-ledger"
            op=key+"/step/1"
            api("/agent/tasks",{"id":key,"text":"inspect (2.9,0)"})
            until=time.monotonic()+15
            while time.monotonic()<until:
                row=api("/agent/tasks?id="+key)
                if row["state"]=="WAITING" and any(e["kind"]=="ros.response" and
                        json.loads(e["payload"])["trace"]["module"]=="edge_core.submit_mission" for e in row["events"]):
                    break
                time.sleep(.15)
            else:
                raise TimeoutError("did not observe acknowledged in-flight motion")
            compose("stop","agent","core")
            replacement=directory/"fresh-core.json"
            replacement.write_text(json.dumps({"services":{"core":{"command":[
                "ros2","run","edge_core","edge_core_node","--ros-args","-p",
                "db_path:=/var/lib/edge-agent/blank-ledger.db"]}}}),encoding="utf-8")
            compose("-f",str(replacement),"up","-d","--no-deps","--no-build","core")
            compose("start","agent")
            base="http://"+compose("port","agent","8090")
            until=time.monotonic()+30
            while True:
                try:
                    row=terminal(key)
                    break
                except OSError:
                    if time.monotonic()>=until:
                        raise
                    time.sleep(.2)
            evidence(entry,row)
            probe="""import json, sqlite3, sys
def count(file):
    with sqlite3.connect('file:/var/lib/edge-agent/'+file+'?mode=ro',uri=True) as db:
        return db.execute('SELECT count(*) FROM missions WHERE mission_id=?',(sys.argv[1],)).fetchone()[0]
print(json.dumps([count('edge.db'),count('blank-ledger.db')]))
"""
            old,new=json.loads(compose("exec","-T","core","python3","-c",probe,op))
            entry.update(original_missions=old,new_missions=new)
            require(old==1 and new==0 and row["state"]=="RECOVERY_REQUIRED" and
                    "navigation.mission_unaccounted" in entry["codes"],"dispatched an unaccounted original operation")

        try:
            compose("up","-d","--no-build","sim","bridge","witness","gateway","core","agent","provider")
            base="http://"+compose("port","agent","8090")
            deadline=time.monotonic()+40
            while True:
                try:
                    if api("/agent/capabilities"):
                        break
                except (OSError,URLError):
                    pass
                if time.monotonic()>=deadline:
                    raise TimeoutError("Agent failed to start")
                time.sleep(.25)
            case("existing_different_goal", lambda e:physical(e,"different","(1,0)","BLOCKED"))
            case("existing_same_goal", lambda e:physical(e,"same","(0,0)","SUCCEEDED"))
            case("truncated_receipt", lambda e:tool(e,"lost-receipt","SUCCEEDED","transport.incomplete_response"))
            case("socket_timeout", lambda e:tool(e,"slow-receipt","SUCCEEDED","transport.unavailable"))
            case("wrong_receipt_id", lambda e:tool(e,"wrong-id","RECOVERY_REQUIRED","tool.receipt_invalid"))
            case("nonfinite_evidence", lambda e:tool(e,"bad-measurement","RECOVERY_REQUIRED","transport.invalid_json"))
            case("missing_evidence", lambda e:tool(e,"missing-evidence","RECOVERY_REQUIRED","tool.receipt_invalid"))
            case("planner_retry", lambda e:model(e,"retry","retry status please","SUCCEEDED","transport.http_503"))
            case("invalid_model_tool", lambda e:model(e,"invalid","invalid status please","BLOCKED","planner.invalid_proposal"))
            case("omitted_navigation", lambda e:model(e,"skipped","Visit (1,0) and read the instrument",
                                                       "BLOCKED","planner.invalid_proposal"))
            case("concurrent_trigger", concurrent)
            case("multistep_restart",long_restart)
            case("offline_and_missing_capability",offline)
            case("replacement_core_ledger",ledger_loss)
            report["passed"]=all(v["passed"] for v in report["cases"].values())
        finally:
            try:
                compose("down","--volumes","--remove-orphans","--timeout","3")
            finally:
                out.parent.mkdir(parents=True,exist_ok=True)
                out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    require(report["passed"],"See "+str(out))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture",action="store_true")
    parser.add_argument("--image",default=os.getenv("EDGE_IMAGE") or "edge-agent-ros:v0.1")
    parser.add_argument("--out",type=Path,default=Path(".ci-results/system/agent.json"))
    args=parser.parse_args()
    if args.fixture:
        serve_provider()
    else:
        run(args.image,args.out)
