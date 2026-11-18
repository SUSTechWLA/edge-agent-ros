"""SDD-007: exercise the module bus through real Agent, Console, publisher and subscriber interfaces."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from derived_tool_acceptance import DEFAULT, fixtures
from lifecycle_acceptance import TERMINAL, check, wait_for
from runtime_plugin_acceptance import HEARTBEAT, SAMPLE, mounted, publish


PLUGIN = "image-check"
MODULE = "runtime.registry"
TRACE = "module-bus-acceptance"
COUNTER_TASK = "bus-control-sample"
REGISTRY = "/etc/edge/plugins"
WATCHER = "demo.watch"


def request(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(req, timeout=4) as response:
        return json.load(response)


def events(base, module):
    return request(base, "/api/events?" + urlencode({"module": module, "after": 0, "limit": 200}))


def transitions(rows, observed):
    check(len(rows) == len(observed), f"expected {len(observed)} transitions; archived {len(rows)}")
    previous = None
    trace = rows[0]["trace_id"]
    identities = set()
    for row, state in zip(rows, observed):
        data = row["data"]
        check(row["module"] == MODULE and row["kind"] == "plugin.registry_observed"
              and row["source"] == "journal:" + MODULE and data["plugin_id"] == PLUGIN,
              f"incorrect producer or event for {row['event_id']}")
        check(row["trace_id"] == trace and row["event_id"] not in identities and not row["task_id"],
              "identity, trace, or task association is incorrect")
        check(row["parent_event_id"] == (previous["event_id"] if previous else None)
              and row["cause_seq"] == (previous["seq"] if previous else None),
              "transition chain is incomplete or out of order")
        check(data["before"] == (previous["data"]["after"] if previous else None),
              "previous state differs from the preceding observation")
        after = data["after"]
        if state is None:
            check(after is None, "removal still has an after state")
        else:
            for key in ("status", "code", "version", "binding"):
                check((after or {}).get(key) == state.get(key), f"{key} disagrees with /agent/plugins")
        check("endpoint" not in json.dumps(data) and "http://" not in json.dumps(data),
              "registry evidence exposes a private endpoint")
        if previous:
            check(previous["source_seq"] < row["source_seq"], "source cursor did not advance")
        identities.add(row["event_id"])
        previous = row


def persistent_rows(rows):
    """Archive-local sequence and observation metadata may differ after rebuilding the archive."""
    fields = ("event_id", "trace_id", "parent_event_id", "kind", "code", "module", "source", "utc",
              "source_seq", "level", "data")
    return [{key: row[key] for key in fields} for row in rows]


def run(image, out):
    project = "edge-module-bus-" + uuid.uuid4().hex[:12]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "EDGE_"))}
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    report = {"passed": False, "project": project, "image": image, "started_at": datetime.now(timezone.utc).isoformat(),
              "stages": {}, "environment": "isolated Docker network, deterministic provider; no Gazebo or model"}
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        plugins, audit, inputs = temp / "plugins", temp / "audit", temp / "inputs"
        runtime = plugins / "runtime"
        for path in (runtime, audit, inputs):
            path.mkdir(parents=True)
        manifest = json.loads((SAMPLE / "manifests" / (PLUGIN + ".json")).read_text(encoding="utf-8"))
        publish(plugins / "heartbeat_fixture.json", {"id": "heartbeat_fixture", "version": "1", "effect": "read",
                "parameters": {}, "description": HEARTBEAT, "endpoint": "http://fixture:8096/heartbeat"})
        publish(inputs / "definition.json", DEFAULT)
        publish(inputs / "cases.json", fixtures(DEFAULT["unit"]))
        common = {"image": image, "pull_policy": "never", "cap_drop": ["ALL"],
                  "security_opt": ["no-new-privileges:true"], "environment": {"ROS_DOMAIN_ID": "147"}}
        console_volumes = [mounted(plugins, REGISTRY), "journal:/var/lib/edge-tasks:ro",
                           "sources:/var/lib/edge-module-sources:ro"]
        def console(volume):
            return {**common, "command": ["python3", "/opt/edge/console/server/main.py"],
                    "volumes": console_volumes + [volume + ":/var/lib/edge-console"],
                    "ports": ["127.0.0.1::8080"]}

        services = {
            "agent": {**common, "command": ["python3", "/opt/edge/agent/service.py"],
                      "environment": {**common["environment"], "EDGE_EXTRA_PLUGIN_DIR": "", "EDGE_OLLAMA_URL": "",
                                      "EDGE_PLANNER_URL": "", "EDGE_RETRY_LIMIT": "0"},
                      "ports": ["127.0.0.1::8090"], "volumes": [mounted(plugins, REGISTRY),
                                                               "journal:/var/lib/edge-tasks"]},
            "console": console("archive"),
            "rebuilt": console("fresh_archive"),
            "fixture": {**common, "read_only": True, "entrypoint": [
                        "python3", "-B", "/opt/edge/tests/system/runtime_plugin_acceptance.py", "--serve-fixture"],
                        "volumes": [mounted(audit, "/audit", read_only=False)]},
            "sink": {**common, "read_only": True, "entrypoint": ["python3", "/opt/edge/client/event_sink.py"],
                     "volumes": ["consumer:/consumer"]},
            "publisher": {**common, "read_only": True, "entrypoint": ["python3", "/opt/edge/agent/author_tool.py"],
                          "volumes": [mounted(plugins, REGISTRY, read_only=False), mounted(inputs, "/inputs")]},
            "pulse": {**common, "read_only": True, "entrypoint": ["python3", "-c"],
                      "environment": {"PYTHONPATH": "/opt/edge/agent"},
                      "volumes": ["sources:/sources"]},
        }
        compose_file = temp / "compose.json"
        publish(compose_file, {"services": services, "volumes": {key: {} for key in
                ("journal", "archive", "fresh_archive", "consumer", "sources")}})

        def compose(*args, fail=False):
            result = subprocess.run(["docker", "compose", "-p", project, "-f", str(compose_file), *args],
                                    capture_output=True, encoding="utf-8", errors="replace", timeout=90, env=env)
            if (result.returncode != 0) != fail:
                raise RuntimeError(f"{args[:5]} exit {result.returncode}: {result.stderr[-1400:]} {result.stdout[-800:]}")
            return result.stdout.strip()

        def base(service, port):
            return "http://" + compose("port", service, str(port))

        def copy_sink(service="console"):
            options = ("--db", "/consumer/copied.sqlite", "--module", MODULE)
            compose("run", "--rm", "-T", "sink", *options, "--url", f"http://{service}:8080", "--once")
            return [json.loads(line) for line in compose("run", "--rm", "-T", "sink", *options, "--export").splitlines()]

        def archived(url, module, count):
            def read():
                rows = events(url, module)["events"]
                return rows if len(rows) >= count else None
            return wait_for(read, f"{url} did not archive {count} rows for {module}")

        try:
            info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
            report.update(image_id=info["Id"], revision=(info["Config"].get("Labels") or {}).get("org.opencontainers.image.revision"))
            compose("up", "-d", "agent", "console", "fixture")
            agent, first = base("agent", 8090), base("console", 8080)
            def catalog():
                return request(agent, "/agent/plugins")["plugins"]
            wait_for(lambda: request(agent, "/agent/plugins"), "Agent startup")
            wait_for(lambda: events(first, MODULE), "Console startup")
            observed = []
            def observe(declaration, status):
                path = runtime / (PLUGIN + ".json")
                if declaration is None:
                    path.unlink()
                else:
                    publish(path, declaration)
                def current():
                    row = next((r for r in catalog() if r["id"] == PLUGIN), None)
                    if status is None:
                        return (True, None) if row is None else None
                    if row and row["status"] == status and (status == "invalid" or row["version"] == declaration["version"]):
                        return True, row
                    return None
                _, row = wait_for(current, f"catalog observation {status}")
                for _ in range(3):
                    check(current(), "catalog did not return the same observation")
                observed.append({k: row[k] for k in ("status", "code", "binding", "version") if k in row}
                                if row is not None else None)
            check(not catalog(), "isolated fixture unexpectedly has runtime plugins")
            observe(manifest, "ready")
            initial = archived(first, MODULE, 1)
            transitions(initial, observed)
            check([r["event_id"] for r in copy_sink()] == [initial[0]["event_id"]],
                  "first subscriber run did not checkpoint the installed plugin")
            request(agent, "/agent/tasks", {"id": COUNTER_TASK, "text": HEARTBEAT})
            def completed():
                task = request(agent, "/agent/tasks?id=" + COUNTER_TASK)
                check(task["state"] not in TERMINAL or task["state"] == "SUCCEEDED",
                      f"control task ended in {task['state']}: {task['detail']}")
                return task if task["state"] == "SUCCEEDED" else None
            task_before = wait_for(completed, "control task did not succeed")
            calls_file = audit / "requests.jsonl"
            calls = calls_file.read_text(encoding="utf-8").splitlines()
            check(len(calls) == 1 and json.loads(calls[0])["path"] == "/heartbeat", "fixture was not called once")
            report["stages"]["catalog"] = {"first_event_id": initial[0]["event_id"], "control_calls": len(calls)}
            compose("stop", "-t", "2", "console")
            observe({**manifest, "draining": True}, "draining")
            observe({**manifest, "version": "2"}, "ready")
            observe({**manifest, "provides": {}}, "invalid")
            observe(manifest, "ready")
            observe(None, None)
            report["stages"]["offline_changes"] = observed
            compose("kill", "--signal", "SIGKILL", "agent")
            compose("start", "agent")
            agent = base("agent", 8090)
            wait_for(lambda: request(agent, "/agent/plugins"), "Agent restart")
            check(not catalog(), "removed plugin returned on restart")
            compose("start", "console")
            first = base("console", 8080)
            rows = archived(first, MODULE, len(observed))
            transitions(rows, observed)
            check(persistent_rows(rows[:1]) == persistent_rows(initial), "initial producer identity changed")
            report["stages"]["recovery"] = persistent_rows(rows)
            copies = copy_sink()
            check([r["event_id"] for r in copies] == [r["event_id"] for r in rows], "subscriber missed offline history")
            check(copy_sink() == copies, "a fresh subscriber process duplicated or mutated prior rows")
            for row in rows:
                replay = request(first, "/api/replay?" + urlencode({"at": row["seq"]}))
                check(replay["mode"] == "replay" and replay["event"]["event_id"] == row["event_id"], "wrong replay frame")
            report["stages"]["consumer_restart"] = {"events": len(copies), "archive": copies[0]["archive_id"]}
            # Run the same source volumes against an independent, empty archive.
            compose("up", "-d", "rebuilt")
            second = base("rebuilt", 8080)
            rebuilt = archived(second, MODULE, len(observed))
            transitions(rebuilt, observed)
            check(persistent_rows(rebuilt) == persistent_rows(rows), "rebuilt archive changed producer facts")
            new_archive = events(second, MODULE)["archive_id"]
            check(new_archive != copies[0]["archive_id"], "archive identity was reused")
            rebuilt_copy = copy_sink("rebuilt")
            check(len(rebuilt_copy) == 2 * len(copies) and
                  [r["event_id"] for r in rebuilt_copy if r["archive_id"] == new_archive] == [r["event_id"] for r in copies],
                  "subscriber lost its archive epochs")
            report["stages"]["archive_rebuild"] = {"new_archive": new_archive, "preserved_events": len(rebuilt)}
            test_publisher(compose, first, archived, report)
            first = test_heartbeat(compose, first, archived, report)
            first = test_journal_coverage(compose, first, report)
            task_after = request(agent, "/agent/tasks?id=" + COUNTER_TASK)
            check(task_before["trace_id"] == task_after["trace_id"] and task_before["events"] == task_after["events"]
                  and calls_file.read_text(encoding="utf-8").splitlines() == calls, "replay changed the task or provider calls")
            all_agent_records = request(first, "/api/events?source=mission_agent%2Ftasks&after=0&limit=500")
            check({r["task_id"] for r in all_agent_records["events"]} == {COUNTER_TASK} and not all_agent_records["has_more"],
                  "missing control task or registry actions generated additional tasks")
            report["stages"]["side_effects"] = {"provider_calls": [json.loads(x) for x in calls],
                                                "control_task": task_after["state"], "task_rows": len(all_agent_records["events"])}
            report["passed"] = True
        except Exception as exc:
            report["failure"] = str(exc)
            report["logs"] = compose("logs", "--no-color", "--tail", "8", "agent", "console", "rebuilt", "fixture")
            raise
        finally:
            try:
                compose("down", "--volumes", "--remove-orphans", "--timeout", "2")
                report["stages"]["cleanup"] = "isolated Compose services and named volumes removed"
            except Exception as exc:
                report.update(passed=False, cleanup_error=str(exc))
                raise
            finally:
                report["finished_at"] = datetime.now(timezone.utc).isoformat()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_publisher(compose, console, archived, report):
    def release(action, expected, *inputs, parent=None, fail=False):
        options = ["--registry", REGISTRY + "/derived", "--expect", expected, "--trace-id", TRACE]
        if parent:
            options.extend(["--parent-event-id", parent])
        result = compose("run", "--rm", "-T", "publisher", action, *inputs, *options, fail=fail)
        return None if fail else json.loads(result)

    args = ("/inputs/definition.json", "--cases", "/inputs/cases.json")
    installed = release("publish", "absent", *args)
    release("publish", "absent", *args, parent=installed["event_id"], fail=True)
    removed = release("remove", installed["version"], DEFAULT["id"], parent=installed["event_id"])
    rows = archived(console, "plugin.publisher", 6)
    labels = [row["kind"].removeprefix("plugin.release.") for row in rows]
    check(labels == ["requested", "applied", "requested", "rejected", "requested", "applied"], "release audit: " + str(labels))
    for request_row, result in zip(rows[::2], rows[1::2]):
        check(result["parent_event_id"] == request_row["event_id"] and result["cause_seq"] == request_row["seq"]
              and result["data"]["change_id"] == request_row["data"]["change_id"]
              and result["trace_id"] == request_row["trace_id"] == TRACE
              and result["data"]["tool_id"] == DEFAULT["id"], "release lost causal identity")
    check(rows[1]["event_id"] == installed["event_id"] and rows[-1]["event_id"] == removed["event_id"]
          and rows[1]["data"]["observed_version"] == rows[3]["data"]["observed_version"] == installed["version"]
          and rows[1]["data"]["case_count"] == len(fixtures(DEFAULT["unit"]))
          and len(rows[1]["data"]["cases_sha256"]) == 64 and rows[-1]["data"]["observed_version"] == "absent",
          "installed version, verification evidence, or deletion outcome was lost")
    check(all(row["parent_event_id"] == installed["event_id"] for row in (rows[2], rows[4]))
          and rows[3]["code"] == "release.version_conflict" and len({r["data"]["change_id"] for r in rows}) == 3,
          "separate operations or explicit caller causes were lost")
    report["stages"]["publisher"] = persistent_rows(rows)


def test_heartbeat(compose, console, archived, report):
    # Third-party plugin uses only the producer SDK. Each invocation exits before the next heartbeat.
    pulse = """
from module_journal import ModuleJournal
import sys
j = ModuleJournal('/sources/demo.watch/module-events.sqlite')
with j.db:
    j.emit(dict(module='demo.watch', kind='plugin.ready', code='plugin.ready',
                trace_id='sample-health', data={'mode':'sample'}), key='ready')
    event = dict(module='demo.watch', kind='sample.notice', code='sample.notice', trace_id='sample-causality')
    if sys.argv[1] == 'opening':
        j.emit(dict(event, event_id='later-child', parent_event_id='later-parent'), key='child')
        j.heartbeat(2)
    else:
        j.emit(dict(event, event_id='later-parent'), key='parent')
        j.heartbeat(60)
j.close()
"""
    def pulses(count):
        def read():
            rows = [r for r in events(console, "journal.watchdog")["events"] if r["data"].get("journal") == WATCHER]
            return rows if len(rows) >= count else None
        return wait_for(read, f"missing watchdog transition {count}")
    compose("run", "--rm", "-T", "pulse", pulse, "opening")
    start = archived(console, WATCHER, 2)
    child = start[1]
    check(child["event_id"] == "later-child" and child["parent_event_id"] == "later-parent" and child["cause_seq"] is None,
          "missing parent identity was lost or prematurely resolved")
    warning = pulses(1)
    check(len(warning) == 1 and warning[0]["kind"] == "module.heartbeat_overdue", "stopped plugin not detected")
    # The monitoring process also dies during the open incident. The unchanged pulse cannot close it.
    compose("kill", "--signal", "SIGKILL", "console")
    compose("start", "console")
    console = "http://" + compose("port", "console", "8080")
    check([r["event_id"] for r in pulses(1)] == [warning[0]["event_id"]], "monitor restart changed the open incident")
    compose("run", "--rm", "-T", "pulse", pulse, "renew")
    state = pulses(2)
    check(state[1]["kind"] == "module.heartbeat_restored" and state[1]["source"] == "edge_console"
          and state[1]["parent_event_id"] == state[0]["event_id"] and state[1]["cause_seq"] == state[0]["seq"],
          "returning plugin did not close the prior watchdog incident")
    final = archived(console, WATCHER, 3)
    check([r["event_id"] for r in final] == [start[0]["event_id"], child["event_id"], "later-parent"]
          and final[1]["seq"] == child["seq"] and final[1]["cause_seq"] == final[2]["seq"],
          "idempotent SDK emission duplicated facts, or late parent was not linked")
    report["stages"]["watchdog"] = persistent_rows(state[:2])
    report["stages"]["late_parent"] = {"child_event_id": child["event_id"], "child_seq": child["seq"],
                                       "initial_cause": child["cause_seq"], "recovered_cause_seq": final[1]["cause_seq"]}
    return console


def test_journal_coverage(compose, console, report):
    """Inject a known prefix gap and an unplug in a dedicated test volume."""
    module = "demo.coverage"
    script = """
from module_journal import ModuleJournal
from pathlib import Path
import sys
folder = Path('/sources/demo.coverage')
parked = folder.with_name('.parked')
if sys.argv[1] == 'publish':
    j = ModuleJournal(folder / 'module-events.sqlite')
    with j.db:
        j.emit(dict(module='demo.coverage', kind='sample.notice', code='sample.notice',
                    trace_id='coverage-sample', event_id='coverage-sample-7'))
        j.db.execute('UPDATE module_bus_events SET seq=7')
    j.close()
elif sys.argv[1] == 'unplug':
    folder.rename(parked)
else:
    parked.rename(folder)
"""
    def read(**filters):
        return request(console, "/api/events?" + urlencode({"after":0, "limit":20, **filters}))["events"]

    def health(count):
        def fetch():
            rows = read(source="edge_console",module="journal.ingress",trace_id="journal.health:"+module)
            return rows if len(rows) >= count else None
        return wait_for(fetch, f"source health history should contain {count} transitions")

    compose("run", "--rm", "-T", "pulse", script, "publish")
    wait_for(lambda: read(event_id="coverage-sample-7"), "producer event after missing prefix")
    gaps = read(source="edge_console",module="journal.ingress",code="module.journal_gap")
    matches = [r for r in gaps if r["data"]["journal"] == module]
    check(len(matches)==1 and matches[0]["data"] == {"journal":module, "missing_from":1,
          "missing_to":6, "next_source_seq":7}, "archive did not identify exactly the missing interval")
    original = read(event_id="coverage-sample-7")[0]
    gap = matches[0]
    check(gap["source_id"] == original["source_id"] and gap["source_seq"] == 6
          and original["source_seq"] == 7, "gap position does not match the producer epoch")
    health(1)
    compose("run", "--rm", "-T", "pulse", script, "unplug")
    health(2)
    compose("kill", "--signal", "SIGKILL", "console")
    compose("start", "console")
    console = "http://" + compose("port", "console", "8080")
    health(2)
    compose("run", "--rm", "-T", "pulse", script, "restore")
    rows = health(3)
    check([r["data"]["status"] for r in rows] == ["available", "absent", "available"],
          "state changed without a source transition")
    check(all(b["parent_event_id"] == a["event_id"] and b["cause_seq"] == a["seq"]
              for a,b in zip(rows,rows[1:])), "missing outage or recovery cause")
    check(read(event_id=original["event_id"]) == [original] and read(event_id=gap["event_id"]) == [gap],
          "replay after restart rewrote or duplicated facts")
    report["stages"]["source_coverage"] = {"gap":gap,"next_source_seq":original["source_seq"],
                                           "health":persistent_rows(rows)}
    return console


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="local Docker image under test")
    parser.add_argument("--out", required=True, type=Path, help="JSON evidence report")
    options = parser.parse_args()
    run(options.image, options.out)
