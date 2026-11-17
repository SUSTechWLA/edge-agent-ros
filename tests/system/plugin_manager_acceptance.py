"""SDD-008: verify automatic plugin recovery and managed service changes in a fresh Docker project."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlencode

from author_bus_acceptance import CLIENT, author_model, ros
from derived_tool_acceptance import DEFAULT, PUBLISHER, fixtures, write_json
from lifecycle_acceptance import check, wait_for
from runtime_plugin_acceptance import SAMPLE, mounted


ROOT = Path(__file__).resolve().parents[2]
ALLOWED = DEFAULT["description"]
UNAUTHORIZED = "按实验室未批准的流程，用千米每小时报告本机前向速度"
SENSOR_REQUIRED = "将里程计前向线速度换算为每小时千米并读取当前测量"
SUPERVISOR = "plugin.supervisor"
MODEL_IMAGE = "ollama/ollama@sha256:910d0d8107b538fb0894e63163eb195de0cc7aefcbc666dc62d3b18f71124592"
MANIFEST = json.loads((SAMPLE / "manifests/image-check.json").read_text(encoding="utf-8"))
POLICY = ROOT / "policies/plugin-recovery.json"


def compact(row):
    return {k: row[k] for k in ("seq", "event_id", "parent_event_id", "cause_seq", "trace_id", "kind", "module", "code")}


def stack_config(image, workspace, cache):
    plugins, proposals, audit, inputs = [workspace / name for name in ("plugins", "proposals", "audit", "inputs")]
    for path in (plugins, proposals, audit, inputs):
        path.mkdir()
    policy = workspace / "policy.json"
    rules = json.loads(POLICY.read_text(encoding="utf-8"))
    default, = [r for r in rules["rules"] if r["text"] == ALLOWED]
    if not cache:
        rules["rules"].append({**default, "text":SENSOR_REQUIRED})
    write_json(policy, rules)
    common = {"image": image, "pull_policy": "never", "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"]}
    model_url = "http://model:11434" if cache else "http://fixture:11434"
    services = {
        "agent": {**common, "command":["python3","/opt/edge/agent/service.py","--ros-args","-r","/odom:=/fixture/odom"],
                  "environment": {**ros("odometry"), "EDGE_OLLAMA_URL":"", "EDGE_PLANNER_URL":"",
                                  "EDGE_EXTRA_PLUGIN_DIR":"", "EDGE_RETRY_LIMIT":"2"},
                  "volumes": ["tasks:/var/lib/edge-tasks", mounted(plugins, "/etc/edge/plugins")]},
        "console": {**common, "command": ["python3","/opt/edge/console/server/main.py"], "environment": ros("agent"),
                    "volumes": ["tasks:/var/lib/edge-tasks:ro", "archive:/var/lib/edge-console",
                                "author_events:/var/lib/edge-author-events:ro", "supervisor:/var/lib/edge-supervisor:ro",
                                mounted(plugins, "/etc/edge/plugins")]},
        "author": {**common, "read_only":True, "tmpfs":["/tmp"], "entrypoint":["python3","/opt/edge/client/plugin_author_watch.py"],
                   "command":["--url","http://console:8080","--ollama-url",model_url,"--model","qwen2.5:1.5b",
                              "--out-dir","/proposals"],"environment":{"EDGE_AUTHOR_EVENT_DB":"/events/module-events.sqlite"},
                   "volumes": ["author_events:/events", mounted(proposals, "/proposals", read_only=False)]},
        "supervisor": {**common, "read_only": True, "tmpfs":["/tmp"],
                       "entrypoint":["python3","/opt/edge/client/plugin_manager_watch.py"],
                       "command":["--url","http://console:8080","--policy","/etc/edge-policy/plugin-recovery.json",
                                  "--proposals","/proposals","--registry","/plugins/derived","--state","/state"],
                       "volumes":[mounted(proposals,"/proposals"),mounted(policy,"/etc/edge-policy/plugin-recovery.json"),
                                  mounted(plugins/"derived","/plugins/derived",read_only=False),"supervisor:/state"]},
        "manager": {**common, "read_only":True, "entrypoint":["python3","/opt/edge/agent/plugin_manager.py"],
                    "volumes": [mounted(plugins,"/plugins",read_only=False), mounted(inputs,"/inputs")]},
        "odometry": {**common, "command":["python3","-c",PUBLISHER], "environment":ros("agent")},
        # Always available for public protocol probes, including when a real model is separately deployed.
        "fixture": {**common, "read_only":True, "entrypoint":["python3","-c",author_model(ALLOWED,UNAUTHORIZED,SENSOR_REQUIRED)],
                    "volumes":[mounted(audit,"/audit",read_only=False)]},
    }
    (plugins / "derived").mkdir()
    volumes = {name:{} for name in ("tasks","archive","author_events","supervisor")}
    if cache:
        volumes.update(model_seed={"external": True, "name": cache}, model_home={})
        services["model"] = {"image": MODEL_IMAGE, "pull_policy":"never", "volumes":["model_home:/root/.ollama"]}
        services["seed"] = {"image": MODEL_IMAGE, "pull_policy":"never", "entrypoint":["/bin/sh","-ec"],
                            "command":["test -d /cache/models/blobs && mkdir -p /root/.ollama/models && cp -a /cache/models/. /root/.ollama/models/"],
                            "volumes":["model_seed:/cache:ro", "model_home:/root/.ollama"]}
    return {"services":services,"volumes":volumes,"networks":{"default":{"internal":True}}}


def run(image, out, cache=None):
    project = "edge-plugin-manager-" + uuid.uuid4().hex[:12]
    env = {k:v for k,v in os.environ.items() if not k.startswith(("EDGE_", "COMPOSE_"))}
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    report = {"project":project,"passed":False,"started_at":datetime.now(timezone.utc).isoformat(),
              "image":image,"author_model":"qwen2.5:1.5b" if cache else "deterministic Ollama protocol fixture",
              "stages":{}}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="edge-plugin-manager-") as temp:
        folder = Path(temp)
        stack = folder / "compose.json"
        config = stack_config(image, folder, cache)
        write_json(stack, config)
        def compose(*args, stdin=None, fail=False):
            result = subprocess.run(["docker","compose","-p",project,"-f",str(stack),*args], input=stdin, env=env,
                                    capture_output=True, encoding="utf-8", errors="replace", timeout=180)
            check((result.returncode != 0) == fail, f"docker compose {args[:5]}: {result.stderr[-1800:]} {result.stdout[-800:]}")
            return result.stdout.strip()
        def api(path, body=None):
            doc = json.loads(compose("exec","-T","fixture","python3","-c",CLIENT,stdin=json.dumps([path,body])))
            if "api_error" in doc:
                raise ConnectionError(doc["api_error"])
            return doc
        def events(**filters):
            page = api("/api/events?" + urlencode({"after":0,"limit":500,**filters}))
            check(not page["has_more"], "test trace exceeded page size")
            return page["events"]
        def fact(kind, **filters):
            rows = wait_for(lambda: events(kind=kind, **filters), f"missing {kind}, {filters}", 150 if cache else 55)
            check(len(rows) == 1, f"expected one {kind} for {filters}")
            return rows[0]
        def get_task(key):
            return api("/agent/tasks?" + urlencode({"id":key,"limit":500}))
        try:
            info = json.loads(subprocess.check_output(["docker","image","inspect",image]))[0]
            report.update(image_id=info["Id"], revision=(info["Config"].get("Labels") or {}).get("org.opencontainers.image.revision"))
            services = ["agent","console","fixture","odometry","author","supervisor"]
            if cache:
                compose("run","--no-deps","--rm","-T","seed")
                services.append("model")
            compose("up","-d","--no-build",*services)
            wait_for(lambda: api("/agent/capabilities")["capabilities"], "agent startup", 60)
            if cache:
                check("qwen2.5:1.5b" in compose("exec","-T","model","ollama","list"), "seed lacks qwen2.5:1.5b")

            trigger = {"kind":"user","source":"operator-console","id":"user-request-171"}
            original = api("/agent/tasks",{"id":"speed-request","text":ALLOWED,"trigger":trigger})
            # Everything between this single request and the observed verification is read-only.
            trace = original["trace_id"]
            verified = fact("plugin.supervisor.verified", trace_id=trace)
            original = get_task(original["id"])
            blocked = fact("agent.blocked", task_id=original["id"])
            proposal = fact("plugin.author.proposed",trace_id=trace)
            approved = fact("plugin.supervisor.approved",trace_id=trace)
            release = fact("plugin.release.applied",module="plugin.publisher",trace_id=trace)
            published = fact("plugin.supervisor.published",trace_id=trace)
            submitted = fact("plugin.supervisor.submitted",trace_id=trace)
            key = submitted["data"]["followup_task_id"]
            native = fact("agent.verified",task_id=key)
            def linked():
                r, = events(event_id=verified["event_id"])
                check(r["parent_event_id"] == r["data"]["verified_event_id"] == native["event_id"],
                      "supervisor verification does not name the concrete agent execution event")
                return r if r["cause_seq"] == native["seq"] else None
            verified = wait_for(linked, "late native verifier cause was not linked")
            followup = get_task(key)
            receipt = followup["steps"][0]["evidence"]
            tool, version = proposal["data"]["tool_id"], proposal["data"]["candidate_version"]
            check(original["state"] == "BLOCKED" and original["steps"] == [] and original["trigger"] == trigger,
                  "autonomous recovery rewrote the original user task or its request")
            check(proposal["parent_event_id"] == blocked["event_id"] and
                  proposal["source"] == "journal:plugin.author" and
                  all(r["source"] == "journal:plugin.supervisor" for r in (approved,published,submitted,verified)),
                  "recovery did not use the author and supervisor production journals")
            raw = (folder / "proposals" / "decisions" / (proposal["data"]["receipt_id"] + ".json")).read_bytes()
            check(proposal["data"]["receipt_sha256"] == hashlib.sha256(raw).hexdigest(), "private author receipt bytes changed")
            decision = json.loads(raw)
            check(decision["model"]["model"] == "qwen2.5:1.5b" and decision["model"]["reply"]["field"] == "speed"
                  and decision["model"]["reply"]["unit"] == "km/h" and decision["definition"]["id"] == tool,
                  "private receipt does not support the claimed model-generated semantics")
            check(verified["data"]["followup_task_id"] == key and followup["state"] == "SUCCEEDED"
                  and len(followup["steps"]) == 1 and followup["trace_id"] == trace and followup["text"] == ALLOWED
                  and followup["trigger"]["parent_task_id"] == original["id"]
                  and followup["trigger"]["parent_event_id"] == release["event_id"]
                  and followup["trigger"]["read_only_tool"] == {"id":tool,"version":version},
                  "follow-up task does not inherit the user trace and exact approved tool")
            check(followup["steps"][0]["tool"] == tool and
                  followup["steps"][0]["version"] == receipt["definition_version"] == release["data"]["observed_version"] == version
                  and release["data"]["case_count"] == len(fixtures("km/h")) and receipt["source"] == "/fixture/odom"
                  and receipt["frame"] == "odom" and receipt["stamp_ns"] > 0
                  and (receipt["raw_value"],receipt["raw_unit"],receipt["value"],receipt["unit"]) == (-0.5,"m/s",-1.8,"km/h"),
                  "release, actual ROS odometry measurement or independently expected conversion differs")
            report["stages"]["autonomous_recovery"] = {"source_task":original["id"],"followup_task":key,
                "events":[compact(r) for r in (blocked,proposal,approved,release,published,submitted,native,verified)],
                "policy_path":"policies/plugin-recovery.json", "native_verified_event_id":native["event_id"],
                "tool":tool,"version":version,"receipt":receipt,"cases":release["data"]["case_count"],
                "user_mutating_calls":1,"model_reply":decision["model"]["reply"],
                "response_sha256":decision["model"]["response_sha256"]}
            # Confirm no duplicate work after restarting both daemons. A fresh unrelated blocked task below
            # serves as the restarted supervisor's externally observed progress barrier.
            compose("kill","--signal","SIGKILL","supervisor","author")
            compose("start","supervisor","author")
            if not cache:
                rejected = api("/agent/tasks", {"id":"requires-owner","text":UNAUTHORIZED})
                review = fact("plugin.supervisor.review",trace_id=rejected["trace_id"])
                declined = fact("plugin.author.proposed",trace_id=rejected["trace_id"])
                check(review["code"] == "supervisor.policy_denied" and bool(review["data"]["action"].strip())
                      and get_task(rejected["id"])["state"] == "BLOCKED"
                      and not events(kind="plugin.supervisor.submitted",trace_id=rejected["trace_id"])
                      and not events(kind="plugin.release.applied",trace_id=rejected["trace_id"])
                      and declined["data"]["tool_id"] not in [v["id"] for v in api("/agent/capabilities")["capabilities"]],
                      "unapproved request installed a capability or lacks an actionable review event")
                report["stages"]["manual_review"] = {**compact(review),"required_action":review["data"]["action"]}
                check((folder/"audit"/"calls").read_text().splitlines() == ["author-draft","author-draft"],
                      "restarting changed the number of author model requests")
            else:
                # Use a one-pass sweep with persisted state as the observable recovery barrier.
                compose("stop","-t","2","supervisor")
                cmd = config["services"]["supervisor"]["command"]
                compose("run","--rm","--no-deps","-T","supervisor",*cmd,"--once")
            positives = events(trace_id=trace)
            for kind in ("plugin.author.proposed","plugin.release.applied","plugin.supervisor.verified"):
                check(sum(r["kind"] == kind for r in positives) == 1, "duplicate autonomous outcome: " + kind)
            accepted = [r["task_id"] for r in positives if r["kind"] == "agent.accepted"]
            check(len(accepted) == 2 and set(accepted) == {original["id"], key} and get_task(key)["events"] == followup["events"],
                  "restarting the supervisor changed execution history")
            report["stages"]["restart"] = {"accepted_tasks":accepted,"positive_verifications":1}
            if not cache:
                # The Agent accepts recently received odometry for two seconds. Let that window expire.
                compose("stop","-t","2","odometry")
                time.sleep(2.2)
                denied = api("/agent/tasks",{"id":"sensor-unavailable","text":SENSOR_REQUIRED})
                sensor_review = fact("plugin.supervisor.review",trace_id=denied["trace_id"])
                attempted = fact("plugin.supervisor.submitted",trace_id=denied["trace_id"])
                failed = get_task(attempted["data"]["followup_task_id"])
                reason = json.loads(failed["events"][-1]["payload"])["diagnostic"]["code"]
                check(sensor_review["code"] == "supervisor.followup_failed" and len(sensor_review["data"]["action"]) > 0
                      and failed["state"] in ("BLOCKED","RECOVERY_REQUIRED") and reason == "tool.odom_stale"
                      and not events(kind="plugin.supervisor.verified",trace_id=denied["trace_id"]),
                      "sensor loss produced success, an unrelated explanation, or no actionable review")
                report["stages"]["sensor_outage"] = {**compact(sensor_review), "diagnosis":reason,
                            "required_action":sensor_review["data"]["action"], "followup_task":failed["id"]}
                # Already-installed capabilities still work without either maintenance process.
                compose("stop","-t","2","author","supervisor")
                compose("start","odometry")
                independent = api("/agent/tasks",{"id":"manager-detached","text":ALLOWED})
                finished = wait_for(lambda: t if (t:=get_task(independent["id"]))["state"]=="SUCCEEDED" else None,
                                    "existing plugin could not execute after unplugging manager", 40)
                check(finished["steps"][0]["version"] == version and finished["steps"][0]["evidence"]["value"] == -1.8,
                      "unplugged manager changed execution contract")
                report["stages"]["manager_detached"] = {"task":finished["id"], "version":version, "value":-1.8}
                test_runtime(compose, api, events, folder/"inputs", report)
            report["passed"] = True
        except Exception as exc:
            report["failure"] = f"{type(exc).__name__}: {exc}"
            report["logs"] = compose("logs","--no-color","--tail","16","agent","console","supervisor","author")
            raise
        finally:
            try:
                compose("down","--volumes","--remove-orphans","--timeout","2")
                report["cleaned_up"] = True
            except Exception as exc:
                report.update(passed=False,cleanup_error=str(exc))
                raise
            finally:
                report["finished_at"] = datetime.now(timezone.utc).isoformat()
                write_json(out, report)

def test_runtime(compose, api, events, inputs, report):
    def manage(action, name, expected, *, key=None, fail=False, target="runtime", extra=()):
        args = [action, name, "--kind", target, "--root", "/plugins", "--expect", expected, *extra]
        if action != "check":
            args.extend(["--trace-id","runtime-maintenance"])
        if key:
            args.extend(["--request-id",key])
        output = compose("run","--no-deps","--rm","-T","manager",*args,fail=fail)
        return None if fail else json.loads(output)
    def catalog():
        return api("/agent/plugins")["plugins"]
    def stage_manifest(obj):
        path = inputs/(obj["id"]+".json")
        write_json(path,obj)
        return "/inputs/"+path.name

    check(not catalog(), "isolated runtime catalog has unexpected entries")
    name = MANIFEST["id"]
    declared = stage_manifest(MANIFEST)
    checked = manage("check",declared,"absent")
    one = manage("publish",declared,"absent",key="install-image-check")
    check(checked["version"] == one["version"] and manage("publish",declared,"absent",key="install-image-check") == one,
          "retry of same management intent was not idempotent")
    def binding(version):
        rows = catalog()
        check(len(rows)==1 and rows[0]["status"]=="ready" and rows[0]["version"]==version,
              "unexpected /agent/plugins contents")
        service = api("/agent/services?name=verify.image")
        check(service["binding"]==rows[0]["binding"] and service["plugin_id"]==name
              and service["plugin_version"]==version,"managed catalog and service binding disagree")
        return service["binding"]
    old_binding = binding(MANIFEST["version"])
    other = stage_manifest({**MANIFEST,"id":"conflict"})
    manage("publish",other,"absent",fail=True)
    missing = stage_manifest({**MANIFEST,"id":"needs-provider","provides":["verify.needs"],"requires":["verify.missing"]})
    manage("publish",missing,"absent",fail=True)
    check(binding(MANIFEST["version"])==old_binding, "invalid dependency or collision changed active plugin")
    stage_manifest({**MANIFEST,"version":"2"})
    two = manage("publish",declared,one["version"],key="upgrade-image-check")
    check(binding("2") != old_binding and one["version"] != two["version"],"upgrade retained old revision")
    manage("remove",name,one["version"],fail=True)
    check(binding("2"),"stale removal damaged the current service")
    # A retry from v1 returns the original receipt and must leave v2 active.
    stage_manifest(MANIFEST)
    check(manage("publish",declared,"absent",key="install-image-check") == one and binding("2"),
          "replayed stale intent regressed another release")
    three = manage("remove",name,two["version"],key="remove-image-check")
    check(not catalog(),"removal did not withdraw the service")
    outcomes = wait_for(lambda: rows if len(rows:=events(module="plugin.manager")) >= 12 else None,"manager event archive")
    check(len(outcomes)==12 and all(r["source"]=="journal:plugin.manager" for r in outcomes),"bad manager event count or ownership")
    results = outcomes[1::2]
    for request_row,result in zip(outcomes[::2],results):
        check(request_row["kind"]=="plugin.release.requested" and result["parent_event_id"]==request_row["event_id"]
              and result["cause_seq"]==request_row["seq"] and result["data"]["change_id"]==request_row["data"]["change_id"],
              "management request/outcome lost causality")
    check([r["code"] for r in results] == ["release.applied","release.service_conflict","release.dependency_unavailable",
                                          "release.applied","release.version_conflict","release.applied"]
          and [r["event_id"] for r in results if r["code"]=="release.applied"] == [x["event_id"] for x in (one,two,three)],
          "manager audit does not corroborate the returned applied and rejected changes")
    report["stages"]["managed_runtime"] = {"revisions":[one["version"],two["version"]],"old_binding":old_binding,
                                          "outcomes":[compact(r) for r in results]}
    # The same public CLI manages derived tools, which appear in a separate model-facing catalog.
    write_json(inputs/"derived.json",DEFAULT)
    write_json(inputs/"cases.json",fixtures("km/h"))
    def derived(version, expectation):
        return manage("publish","/inputs/derived.json",expectation,target="derived",extra=("--cases","/inputs/cases.json"),
                      key="cli-speed-"+version)
    d1 = derived("v1","absent")
    def tool_version():
        rows = [r for r in api("/agent/capabilities")["capabilities"] if r["id"]==DEFAULT["id"]]
        return rows[0]["version"] if rows else None
    check(tool_version()==d1["version"],"derived tool is missing from capability catalog")
    write_json(inputs/"derived.json",{**DEFAULT,"unit":"m/s"})
    write_json(inputs/"cases.json",fixtures("m/s"))
    d2 = derived("v2",d1["version"])
    check(tool_version()==d2["version"]!=d1["version"],"derived tool revision was not upgraded")
    removed = manage("remove",DEFAULT["id"],d2["version"],target="derived")
    check(tool_version() is None, "derived tool remained installed")
    report["stages"]["managed_derived"] = {"revisions":[d1["version"],d2["version"]],"removed":removed["event_id"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="edge-agent-ros:v0.1", help="local image under test")
    parser.add_argument("--out", type=Path, default=ROOT/".ci-results/system/plugin-manager.json")
    parser.add_argument("--ollama-cache-volume", help="existing Ollama home volume to seed read-only into an isolated model")
    args = parser.parse_args()
    run(args.image, args.out, args.ollama_cache_volume)
