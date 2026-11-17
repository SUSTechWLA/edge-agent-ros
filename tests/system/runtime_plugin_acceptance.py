"""SDD-006: exercise a running Agent and independent result verifiers over their public protocols."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "examples" / "result-verifiers"
PHOTO = "读取一张测试照片"
HEARTBEAT = "读取测试心跳"
FRAME = b"P5\n2 2\n255\n\x00\x01\x02\xff"
TERMINAL = {"SUCCEEDED", "BLOCKED", "RECOVERY_REQUIRED", "CANCELED"}


def check(value, message):
    if not value:
        raise AssertionError(message)


def publish(path, obj):
    draft = path.with_suffix(".tmp")
    draft.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    draft.replace(path)


class CameraFixture(BaseHTTPRequestHandler):
    """A deterministic acquisition provider whose HTTP calls are counted outside the Agent."""
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        operation = body["operation_id"]
        if self.path == "/review":
            receipt = {field: body[field] for field in ("operation_id", "plugin_id", "plugin_version", "service")}
            receipt.update(verdict="pass", evidence={"reviewed": True})
            mode = body["task_id"]
            if mode == "wrong-identity":
                receipt["operation_id"] += "-other"
            elif mode == "wrong-version":
                receipt["plugin_version"] = "stale"
            elif mode == "unknown":
                receipt["verdict"] = "unknown"
            elif mode == "timeout":
                time.sleep(3.5)
            elif mode == "crash-window":
                marker = Path("/audit/review-entered")
                if not marker.exists():
                    marker.touch()
                    # The host kills the Agent while this read-only call has no reply.
                    time.sleep(4)
            elif mode == "concurrent":
                # Only return a positive verdict once an unrelated public task has finished.
                # Keeping the verifier call open detects an execution queue blocked on verification.
                url = "http://agent:8090/agent/tasks"
                req = Request(url, data=json.dumps({"text":HEARTBEAT, "id":"during-check"}).encode(),
                              headers={"Content-Type":"application/json"})
                direct = build_opener(ProxyHandler({}))
                with direct.open(req, timeout=3) as reply:
                    json.load(reply)
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    with direct.open(url+"?id=during-check", timeout=3) as reply:
                        state = json.load(reply)["state"]
                    if state == "SUCCEEDED":
                        receipt["evidence"] = {"observed_unrelated_task":"during-check", "state":state}
                        break
                    time.sleep(.05)
                else:
                    receipt["verdict"] = "unknown"
            self.reply(receipt)
            return
        with Path("/audit/requests.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"path": self.path, "operation_id": operation}) + "\n")
        if self.path == "/heartbeat":
            evidence = {"alive": True}
        elif self.path == "/capture":
            key = hashlib.sha256(operation.encode()).hexdigest()
            folder = Path("/artifacts") / key
            folder.mkdir(exist_ok=True)
            frame = folder / "frame.pgm"
            pixels = FRAME[:-1] if operation.startswith("truncated/") else FRAME
            if not operation.startswith("missing/") and not frame.exists():
                frame.write_bytes(pixels)
            if operation.startswith("foreign/"):
                key = hashlib.sha256(b"valid/step/1").hexdigest()
            digest = hashlib.sha256(pixels).hexdigest()
            if operation.startswith("wrong-digest/"):
                digest = "0" * 64
            evidence = {"image_uri": "artifact://" + key + "/frame.pgm", "sha256": digest}
            if operation.startswith("baseline-uri/"):
                evidence["image_uri"] = "file:///tmp/edge-artifact-never-created.jpg"
        else:
            self.send_error(404)
            return
        self.reply({"operation_id": operation, "state": "SUCCEEDED", "evidence": evidence})

    def reply(self, receipt):
        raw = json.dumps(receipt).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass  # Expected when deliberately returning after the client's timeout.


def mounted(source, target, *, read_only=True):
    return {"type": "bind", "source": str(source), "target": target, "read_only": read_only}


def run(image, out):
    name = "edge-verifiers-" + uuid.uuid4().hex[:12]
    start = datetime.now(timezone.utc).isoformat()
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    report = {"passed": False, "started_at": start, "project": name, "image_id": info["Id"],
              "revision": info["Config"]["Labels"].get("org.opencontainers.image.revision"),
              "environment": "isolated Docker network, fixture acquisition, local deterministic planner",
              "tasks": {}, "catalogs": {}}
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "EDGE_"))}
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        plugins, contracts, audit = temp/"plugins", temp/"contracts", temp/"audit"
        runtime = plugins / "runtime"
        runtime.mkdir(parents=True)
        contracts.mkdir()
        audit.mkdir()
        manifests = {key: json.loads((SAMPLE/"manifests"/(key+".json")).read_text(encoding="utf-8"))
                     for key in ("image-check", "digest-check")}
        camera = {"id": "camera_fixture", "version": "1", "effect": "idempotent", "parameters": {},
                  "description": PHOTO, "outcomes": {"image": "image_uri"}, "endpoint": "http://fixture:8096/capture"}
        publish(plugins/"camera_fixture.json", camera)
        publish(plugins/"heartbeat_fixture.json", {"id": "heartbeat_fixture", "version": "1", "effect": "read",
                "parameters": {}, "description": HEARTBEAT, "endpoint": "http://fixture:8096/heartbeat"})
        defaults = {"image": image, "pull_policy": "never", "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"]}
        services = {
            "agent": {**defaults, "command": ["python3", "/opt/edge/agent/service.py"],
                      "environment": {"ROS_DOMAIN_ID": "151", "EDGE_PLUGIN_DIR": "/plugins", "EDGE_EXTRA_PLUGIN_DIR": "",
                                      "EDGE_CONTRACT_DIR": "/contracts", "EDGE_RETRY_LIMIT": "0",
                                      "EDGE_OLLAMA_URL": "", "EDGE_PLANNER_URL": ""},
                      "volumes": [mounted(plugins, "/plugins"), mounted(contracts, "/contracts"), "journal:/var/lib/edge-tasks"]},
            "fixture": {**defaults, "read_only": True, "entrypoint": ["python3", "-B", "/lab/probes/fixture.py", "--serve-fixture"],
                        "volumes": [mounted(Path(__file__).resolve(), "/lab/probes/fixture.py"), mounted(audit, "/audit", read_only=False),
                                    "capture:/artifacts"]}}
        for key in manifests:
            services[key] = {**defaults, "read_only": True, "entrypoint": ["python3", "-B", "/sample/verifier.py",
                             "--plugin", key, "--root", "/artifacts"], "volumes": [mounted(SAMPLE, "/sample"), "capture:/artifacts:ro"]}
        compose_file = temp / "compose.json"
        publish(compose_file, {"services": services, "networks": {"default": {"internal": True}},
                               "volumes": {"capture": {}, "journal": {}}})
        def compose(*args, stdin=None):
            cmd = ["docker", "compose", "-p", name, "-f", str(compose_file), *args]
            result = subprocess.run(cmd, input=stdin, env=env, capture_output=True, timeout=90,
                                    encoding="utf-8", errors="replace")
            if result.returncode:
                raise RuntimeError(f"{args}: {result.stderr.strip()}")
            return result.stdout.strip()

        # No host port forwarding. The fixture uses the API exactly as a client on this test network.
        client = """
import json,sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler,Request,build_opener
path,body=json.load(sys.stdin)
req=Request('http://agent:8090'+path,data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json'})
try:
    response=build_opener(ProxyHandler({})).open(req,timeout=5)
except HTTPError as error:
    response=error
with response:
    print(json.dumps([response.status,json.load(response)]))
"""
        def api(path, body=None, *, expected_status=None):
            status, obj = json.loads(compose("exec", "-T", "fixture", "python3", "-c", client, stdin=json.dumps([path, body])))
            check(status == expected_status if expected_status is not None else 200 <= status < 300,
                  f"{path} HTTP {status}: {obj}")
            return obj

        def catalog():
            deadline = time.monotonic() + 45
            while True:
                try:
                    return api("/agent/plugins")
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.25)

        def await_task(key, expected):
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                task = api("/agent/tasks?" + urlencode({"id":key, "limit":500}))
                if task["state"] in TERMINAL:
                    check(task["state"] == expected, f"{key}: expected {expected}, got {task['state']}: {task['detail']}")
                    return task
                time.sleep(.15)
            raise TimeoutError(f"{key} did not reach {expected}")

        def record(label, task):
            step = task["steps"][0]
            events = task["events"]
            for row in events:
                trace = json.loads(row["payload"])["trace"]
                check(trace["trace_id"] == task["trace_id"], "journal trace changed")
            report["tasks"][label] = {"id":task["id"], "state":task["state"], "trace_id":task["trace_id"],
                "steps":task["steps"], "events":[{"kind":r["kind"], "operation_id":r["operation_id"],
                        "trace":{k:v for k,v in json.loads(r["payload"])["trace"].items()
                                 if k in ("event_id","parent_event_id","code","module","call_id")}} for r in events]}
            return step

        def submit(key, expected, text=PHOTO):
            check(api("/agent/tasks", {"id":key, "text":text})["id"] == key, "unexpected task ID")
            return record(key, await_task(key, expected))

        def attempts(key):
            log = audit / "requests.jsonl"
            if not log.exists():
                return 0
            return sum(json.loads(line)["operation_id"] == key+"/step/1" for line in log.read_text(encoding="utf-8").splitlines())

        def resume(key, label, expected):
            api("/agent/retry", {"id":key})
            return record(label, await_task(key, expected))

        def agent_started_at():
            cid = compose("ps", "-q", "agent")
            return subprocess.check_output(["docker", "inspect", "--format", "{{.State.StartedAt}}", cid], text=True).strip()

        def plugin_state(label, plugin, status, code=None):
            catalog_state = catalog()
            report["catalogs"][label] = catalog_state
            entry = next(p for p in catalog_state["plugins"] if p["id"] == plugin)
            check(entry["status"] == status and (code is None or entry.get("code") == code), str(entry))
            return entry

        image_manifest = manifests["image-check"]
        image_path = runtime / "image-check.json"
        digest_path = runtime / "digest-check.json"
        dep = {"schema_version":1, "id":"fixture-context", "version":"1", "endpoint":"http://fixture:8096/context",
               "provides":["fixture.context"], "requires":[]}
        dep_path = runtime / "fixture-context.json"
        try:
            compose("up", "-d", "--no-build", "agent", "fixture", "image-check")
            report["catalogs"]["initial"] = catalog()
            identity = agent_started_at()
            submit("before-install", "RECOVERY_REQUIRED")
            check(attempts("before-install") == 0, "missing required verifier allowed a capture")
            # Exercise missing, cyclic, and ambiguous service graphs through the public catalog.
            publish(image_path, {**image_manifest, "requires":["fixture.context"]})
            plugin_state("missing-dependency", "image-check", "blocked", "plugin.service_missing")
            query = "/agent/services?" + urlencode({"name":"verify.image"})
            check(api(query, expected_status=409)["code"] == "plugin.service_missing", "missing dependency resolved")
            publish(dep_path, {**dep, "requires":["verify.image"]})
            plugin_state("cycle", "image-check", "blocked", "plugin.dependency_cycle")
            publish(dep_path, dep)
            duplicate = runtime/"duplicate.json"
            publish(duplicate, {**dep, "id":"duplicate"})
            plugin_state("conflict", "image-check", "blocked", "plugin.service_conflict")
            check(api(query, expected_status=409)["code"] == "plugin.service_conflict", "conflicting dependency resolved")
            duplicate.unlink()
            bound = plugin_state("installed", "image-check", "ready")["binding"]
            old_resolution = api(query)
            check(old_resolution["binding"] == bound and old_resolution["dependencies"], "resolver dropped dependency binding")
            pinned_query = query + "&" + urlencode({"binding":bound})
            check(api(pinned_query) == old_resolution, "cannot resolve current exact binding")
            report["service_resolution"] = {"initial":old_resolution}
            resumed = resume("before-install", "after-install", "SUCCEEDED")
            check(len(resumed["verification"]["decisions"]) == 1, "missing independent decision")
            good = submit("valid", "SUCCEEDED")
            decision = good["verification"]["decisions"][0]
            decoded = decision["evidence"]
            check(decision["verdict"] == "pass" and decoded["format"] == "P5" and
                  (decoded["width"], decoded["height"], decoded["pixels"], decoded["pixel_min"], decoded["pixel_max"]) ==
                  (2, 2, 4, 0, 255) and decoded["sha256"] == good["evidence"]["sha256"] == hashlib.sha256(FRAME).hexdigest(),
                  "independent verifier did not decode the expected image")
            for key, code in (("baseline-uri","artifact.invalid_reference"), ("missing","artifact.missing"),
                              ("truncated","image.invalid_raster"), ("foreign","artifact.wrong_operation")):
                step = submit(key, "BLOCKED")
                decision = step["verification"]["decisions"][-1]
                check(decision["verdict"] == "fail" and decision["evidence"]["code"] == code, key+" lacked independent refutation")
            # The process is offline while its declaration is still mounted: the tool runs, then verification waits.
            compose("stop", "-t", "1", "image-check")
            held = submit("interrupted", "RECOVERY_REQUIRED")
            check(held["verification"]["candidate"] and attempts("interrupted") == 1, "missing durable candidate")
            original_pin = held["verification"]["pins"][0]["binding"]
            digest_policy = {"schema_version":1,"tools":{"camera_fixture":["verify.digest"]},"outcomes":{}}
            publish(contracts/"verification-policy.json", digest_policy)
            submit("new-policy", "RECOVERY_REQUIRED")
            check(attempts("new-policy") == 0, "new task bypassed extra dependency")
            dep_path.unlink()
            report["catalogs"]["removed"] = catalog()
            check(submit("unrelated", "SUCCEEDED", HEARTBEAT)["evidence"]["alive"], "unrelated task blocked")
            check(agent_started_at() == identity, "hot operations restarted the Agent")
            compose("restart", "-t", "1", "agent")
            catalog()
            check(agent_started_at() != identity, "injected Agent restart did not take place")
            restore = await_task("interrupted", "RECOVERY_REQUIRED")["steps"][0]
            check(restore["verification"] == held["verification"], "candidate changed across restart")
            # Only the dependency version changes; the verifier's own declaration remains untouched.
            publish(dep_path, {**dep, "version":"2"})
            compose("start", "image-check")
            conflict = resume("interrupted", "changed-binding", "RECOVERY_REQUIRED")
            check(conflict["verification"]["pins"][0]["binding"] == original_pin and attempts("interrupted") == 1,
                  "new binding replaced in-flight verifier or repeated capture")
            check(plugin_state("changed", "image-check", "ready")["binding"] != bound, "dependency upgrade kept original binding")
            report["service_resolution"]["changed"] = api(query)
            report["service_resolution"]["stale_binding"] = api(pinned_query, expected_status=409)
            # Restore the original dependency in draining mode: a committed operation can finish,
            # while a fresh photo waits before capture. Its only policy is the built-in image rule.
            (contracts/"verification-policy.json").unlink()
            publish(dep_path, {**dep, "draining":True})
            plug = plugin_state("draining", "image-check", "draining")
            check(plug["binding"] == bound, "closing admission reset the original binding")
            drain = report["service_resolution"]["drain"] = {
                "new_lookup":api(query, expected_status=409), "bound_lookup":api(pinned_query)}
            check(drain["new_lookup"]["code"] == "plugin.draining" and drain["bound_lookup"] == old_resolution,
                  "drain failed to separate existing claims from new lookups")
            submit("during-drain", "RECOVERY_REQUIRED")
            drain["new_capture_count_before_reopen"] = attempts("during-drain")
            check(drain["new_capture_count_before_reopen"] == 0 and
                  report["tasks"]["during-drain"]["events"][-1]["trace"]["code"] == "plugin.draining",
                  "draining service admitted a fresh capture or reported a different fault")
            check(submit("drain-health", "SUCCEEDED", HEARTBEAT)["evidence"]["alive"], "drain blocked unrelated work")
            done = resume("interrupted", "restored", "SUCCEEDED")
            check(done["operation_id"] == held["operation_id"] and done["verification"]["required"] == ["verify.image"] and
                  done["evidence"] == held["verification"]["candidate"] and attempts("interrupted") == 1, "restore lost old semantics")
            dep_path.unlink()
            drain["after_removal"] = api(pinned_query, expected_status=409)
            check(drain["after_removal"]["code"] == "plugin.service_missing", "removed dependency still served pinned lookups")
            publish(dep_path, dep)
            fresh = resume("during-drain", "reopened", "SUCCEEDED")
            check(fresh["verification"]["pins"][0]["binding"] == bound and attempts("during-drain") == 1,
                  "reopening did not restore new work")
            publish(contracts/"verification-policy.json", digest_policy)
            compose("up", "-d", "--no-build", "digest-check")
            publish(digest_path, manifests["digest-check"])
            two = resume("new-policy", "second-plugin", "SUCCEEDED")
            check({d["service"] for d in two["verification"]["decisions"]} == {"verify.digest","verify.image"},
                  "second service did not contribute")
            bad = submit("wrong-digest", "BLOCKED")
            check(any(d["service"] == "verify.digest" and d["verdict"] == "fail" for d in bad["verification"]["decisions"]),
                  "bad digest was not independently detected")
            digest_path.unlink()
            check(submit("after-unmount", "SUCCEEDED", HEARTBEAT)["evidence"]["alive"], "unmount affected unrelated work")
            publish(runtime/"review.json", {"schema_version":1, "id":"review", "version":"1",
                    "provides":["verify.review"], "requires":[], "endpoint":"http://fixture:8096/review"})
            publish(contracts/"verification-policy.json", {"schema_version":1, "tools":{"camera_fixture":["verify.review"]}, "outcomes":{}})
            con = submit("concurrent", "SUCCEEDED")
            check(any(d["evidence"].get("observed_unrelated_task") == "during-check" for d in con["verification"]["decisions"]),
                  "no unrelated task finished during the verifier call")
            record("during-check", await_task("during-check", "SUCCEEDED"))
            for key, code in (("wrong-identity","plugin.receipt_invalid"), ("wrong-version","plugin.receipt_invalid"),
                              ("unknown","plugin.verdict_unknown"), ("timeout","plugin.transport")):
                fault = submit(key, "RECOVERY_REQUIRED")
                check(fault["verification"]["candidate"] and attempts(key) == 1 and
                      report["tasks"][key]["events"][-1]["trace"]["code"] == code and
                      all(d["service"] != "verify.review" for d in fault["verification"]["decisions"]),
                      f"{key} lost its candidate or recorded an unproven decision")
            api("/agent/tasks", {"id":"crash-window", "text":PHOTO})
            end = time.monotonic() + 15
            while not (audit/"review-entered").exists():
                check(time.monotonic() < end, "verifier did not enter the current operation")
                time.sleep(.03)
            compose("kill", "--signal", "SIGKILL", "agent")
            compose("start", "agent")
            catalog()
            crashed = await_task("crash-window", "SUCCEEDED")
            record("crash-window", crashed)
            verification_calls = Counter(json.loads(row["payload"])["trace"]["module"] for row in crashed["events"]
                                         if row["kind"] == "verification.request")
            check(verification_calls == {"image-check":1, "review":2} and attempts("crash-window") == 1,
                  "crash recovery lost a decision or replayed acquisition")
            report["crash_window_verification_calls"] = dict(verification_calls)
            check(api("/agent/tasks", {"id":"valid", "text":PHOTO})["state"] == "SUCCEEDED" and attempts("valid") == 1,
                  "replaying a completed task recaptured the image")
            kinds = [row["kind"] for row in report["tasks"]["restored"]["events"]]
            check(kinds.count("verification_pending") == kinds.count("verified") == kinds.count("dispatch") == 1,
                  "resumption dispatched or committed more than once")
            all_calls = Counter(json.loads(row)["operation_id"] for row in (audit/"requests.jsonl").read_text().splitlines())
            check(all(v == 1 for v in all_calls.values()), "provider received duplicate operations")
            report.update(passed=True, provider_calls=dict(all_calls), initial_agent_started_at=identity,
                          restarted_agent_started_at=agent_started_at())
        except Exception as exc:
            report["failure"] = str(exc)
            report["logs"] = compose("logs", "--no-color", "--tail", "12")
            raise
        finally:
            try:
                compose("down", "--volumes", "--remove-orphans", "--timeout", "2")
            except RuntimeError as exc:
                report.update(passed=False, cleanup_failure=str(exc))
                raise
            finally:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--image")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.serve_fixture:
        ThreadingHTTPServer(("0.0.0.0",8096), CameraFixture).serve_forever()
    elif args.image and args.out:
        run(args.image, args.out)
    else:
        parser.error("supply --image and --out")
