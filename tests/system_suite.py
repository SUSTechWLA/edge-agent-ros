"""Single-robot system verification. Run from any directory with host Python 3, pwsh and Docker."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from test_selection import STAGES, changed_paths, selection

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def utc():
    return datetime.now(timezone.utc).isoformat()


def output(cmd, env):
    return subprocess.check_output(cmd, cwd=ROOT, env=env, encoding="utf-8", errors="replace").strip()


def run(args, selected, reasons):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    directory = (args.out_root / run_id).resolve()
    directory.mkdir(parents=True)
    report_file = directory / "summary.json"
    env = dict(os.environ)
    revision = output(["git","rev-parse","HEAD"], env)
    dirty = bool(output(["git","status","--porcelain"], env))
    # Never import the developer's planner URL, plugin directories, port or project name.
    env = {k:v for k,v in env.items() if not k.startswith(("EDGE_", "COMPOSE_"))}
    image = "edge-agent-ros:system-" + run_id.lower()
    env.update(COMPOSE_DISABLE_ENV_FILE="1", EDGE_IMAGE=image, EDGE_ROS_DOMAIN_ID="103",
               EDGE_CONSOLE_PORT="0", EDGE_REVISION=revision+("-dirty" if dirty else ""),
               EDGE_EVIDENCE_DIR=str(directory), EDGE_VERIFY_ROOT=str(directory/"core"),
               EDGE_RESILIENCE_REPORT=str(directory/"resilience.json"), PYTHONUTF8="1")
    if args.base_image:
        env["EDGE_BASE_IMAGE"] = args.base_image
    report = {"run_id":run_id, "started_at":utc(), "revision":revision, "dirty":dirty,
              "selected":selected, "selection":reasons, "platform":"Docker Linux containers",
              "stages":[], "status":"running"}
    def save():
        temp = report_file.with_suffix(".tmp")
        temp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        temp.replace(report_file)
    def skip(name, why, status="skipped"):
        report["stages"].append({"name":name,"status":status,"reason":why})
        save()
    def stage(name, commands):
        log_path=directory/(name+".log")
        row={"name":name,"started_at":utc(),"log":log_path.name,"status":"running"}
        report["stages"].append(row)
        save()
        print(name, flush=True)
        started=time.monotonic()
        try:
            with log_path.open("w",encoding="utf-8") as log:
                for cmd in commands:
                    print(subprocess.list2cmdline([str(s) for s in cmd]), file=log, flush=True)
                    subprocess.run(cmd,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
            row["status"]="passed"
        except (OSError,subprocess.CalledProcessError) as ex:
            row.update(status="failed",reason=str(ex))
            print(log_path.read_text(encoding="utf-8", errors="replace")[-1200:],flush=True)
        row["duration_s"]=round(time.monotonic()-started,2)
        save()
        return row["status"]=="passed"

    compose=["docker","compose","-p","edge-test-"+run_id.lower(),"-f",str(ROOT/"compose.yaml")]
    project=compose[3]
    pwsh=shutil.which("pwsh") or shutil.which("powershell") or "pwsh"
    def script(name,*args):
        return [pwsh,"-NoProfile","-File",str(ROOT/"scripts"/name),*args]
    def test(name,*args):
        return [PY,str(ROOT/"tests"/name),*args]
    def in_image(*cmd):
        return ["docker","run","--rm","--network","none","-v",f"{directory}:/evidence",image,*cmd]
    docker_used = any(name not in ("repository","planner") for name in selected)
    effective_model = "model" in selected and bool(args.ollama_cache_volume)
    public = [name for name in selected if name in
              ("console-missions","places","scene-binding","scene-outage","clock-reset","ui") or name=="model" and effective_model]
    started_sim=False
    board_image = "edge-agent-ros/hardware:system-" + run_id.lower()
    board_built = False
    try:
        if "repository" in selected:
            stage("repository",[[PY,"scripts/check_sdd.py"],test("repository_contract.py","-v"),
                                test("document_contract.py","-v"),
                                test("test_selection.py","-v"),["docker","compose","config","--quiet"],
                                ["docker","compose","-f","compose.hardware.yaml","config","--quiet"]])
        if "planner" in selected:
            stage("planner",[test("planning_contract.py","-v")])
        if "board" in selected:
            env["EDGE_HARDWARE_IMAGE"]=board_image
            board_built=stage("board-build",[["docker","compose","-f","compose.hardware.yaml","build","core"]])
            if board_built:
                stage("board",[test("system/board_smoke.py","--image",board_image,"--out",directory/"board.json")])
            else:
                skip("board","hardware image build failed","blocked")
        sim_needed = any(name not in ("repository","planner","board") for name in selected)
        if sim_needed:
            if not stage("build",[script("build.ps1")]):
                for name in selected:
                    if name not in ("repository","planner","board"):
                        skip(name,"candidate image build failed","blocked")
            else:
                report["image_id"]=output(["docker","image","inspect",image,"--format","{{.Id}}"],env)
                report["image_revision"]=output(["docker","image","inspect",image,"--format",
                                                  '{{index .Config.Labels "org.opencontainers.image.revision"}}'],env)
                report["docker"]=output(["docker","version","--format","{{.Server.Version}}"],env)
                report["compose"]=output(["docker","compose","version","--short"],env)
                checks={
                    "agent-contracts":[in_image("python3","/opt/edge/tests/agent_trace_contract.py","-v")],
                    "observations":[in_image("python3","-m","unittest","discover","-v","-s","/opt/edge/console/server")],
                    "scene-contracts":[in_image("python3","-m","unittest","discover","-v","-s","/opt/edge/tests","-p","test_scene*.py")],
                    "core":[script("verify.ps1","-SkipBuild")],
                    "motion-sensors":[in_image("python3","/opt/edge/tests/system/motion_faults.py","--out","/evidence/motion-sensors.json")],
                    "agent-faults":[test("system/agent_faults.py","--image",image,"--out",directory/"agent-faults.json")],
                    "resilience":[test("console_resilience.py")],
                }
                for name, commands in checks.items():
                    if name in selected:
                        stage(name,commands)
                if "motion-world" in selected:
                    override=directory/"motion.override.json"
                    override.write_text(json.dumps({"services":{"gateway":{"command":[
                        "ros2","run","edge_gateway","edge_gateway_node","--ros-args","-p",
                        "require_scan:=true","-r","/scan:=/motion_test/scan"]}}}),encoding="utf-8")
                    motion=[*compose[:3],project+"-motion",*compose[4:],"-f",str(override)]
                    try:
                        stage("motion-world",[[*motion,"up","-d","--no-build","sim","bridge","witness","gateway"],
                              [*motion,"run","--rm","-T","--no-deps","probe","python3","/opt/edge/tests/system/motion_faults.py",
                               "--gazebo","--out","/evidence/motion-world.json"]])
                    finally:
                        stage("motion-cleanup",[[*motion,"down","--volumes","--remove-orphans","--timeout","3"]])
                if "model" in selected and not effective_model:
                    skip("model","real Ollama not requested; pass --ollama-cache-volume to exercise inference")
                if public:
                    services=["sim","bridge","witness","gateway","core","perception","scene","agent","console"]
                    if effective_model:
                        override=directory/"model.override.json"
                        override.write_text(json.dumps({"services":{"model":{"environment":{"OLLAMA_MODELS":"/cache/models"},
                            "volumes":["test-model-cache:/cache:ro"]}},
                            "volumes":{"test-model-cache":{"external":True,"name":args.ollama_cache_volume}}}),encoding="utf-8")
                        env["EDGE_OLLAMA_URL"]="http://model:11434"
                        compose.extend(["-f",str(override),"--profile","model"])
                        services.append("model")
                    started_sim=True
                    ready=stage("simulation-start",[[*compose,"up","-d","--no-build",*services],
                                [*compose,"exec","-T","core","/usr/local/bin/edge-entrypoint","python3","/opt/edge/client/ready.py"]])
                    if ready:
                        url="http://"+output([*compose,"port","console","8080"],env)
                        commands={
                          "console-missions":test("console_acceptance.py","--url",url,"--out",directory/"console"),
                          "places":test("semantic_acceptance.py","--url",url,"--out",directory/"places.json"),
                          "scene-binding":test("system/scene_binding_acceptance.py","--project",project,"--out",directory/"binding.json"),
                          "scene-outage":test("scene_recovery_acceptance.py","--project",project,"--out",directory/"outage.json"),
                          "clock-reset":test("system/clock_reset_acceptance.py","--project",project,"--out",directory/"clock-reset.json"),
                          "model":test("model_trace_acceptance.py","--url",url,"--out",directory/"model.json"),
                          "ui":["node",str(ROOT/"tests/system/console_browser.mjs"),url,str(directory/"ui.json")],
                        }
                        for name in public:
                            if name in ("ui","model","places","console-missions"):
                                current="http://"+output([*compose,"port","console","8080"],env)
                                commands[name]=[current if arg==url else arg for arg in commands[name]]
                            stage(name,[commands[name]])
                    else:
                        for name in public:
                            skip(name,"simulator did not become ready","blocked")
    except BaseException as ex:
        report["interrupted"]=str(ex) or type(ex).__name__
        raise
    finally:
        try:
            if started_sim:
                if any(row["status"]=="failed" for row in report["stages"]):
                    stage("simulation-logs",[[*compose,"logs","--no-color","--tail","200"]])
                stage("simulation-cleanup",[[*compose,"down","--volumes","--remove-orphans","--timeout","3"]])
            if docker_used and report.get("image_id"):
                stage("image-cleanup",[["docker","image","rm",image]])
            if board_built:
                stage("board-cleanup",[["docker","image","rm",board_image]])
        finally:
            report["finished_at"]=utc()
            report["status"]="failed" if report.get("interrupted") or any(
                row["status"] in ("failed","blocked","running") for row in report["stages"]) else "passed"
            if report["status"]=="passed" and any(row["status"]=="skipped" for row in report["stages"]):
                report["status"]="passed_with_skips"
            save()
            print(f'{report["status"]}: {report_file}',flush=True)
    return report["status"] in ("passed","passed_with_skips")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed-since",help="compare with the merge base including uncommitted files; default runs full suite")
    parser.add_argument("--plan",action="store_true",help="print selected stages and reasons without running")
    parser.add_argument("--out-root",type=Path,default=ROOT/".ci-results/system/runs")
    parser.add_argument("--base-image",help="preinstalled ROS/Gazebo SDK image")
    parser.add_argument("--ollama-cache-volume",help="existing Ollama home volume; mounts the model cache read-only in the isolated project")
    args=parser.parse_args()
    stages,why=selection(changed_paths(args.changed_since,ROOT) if args.changed_since else None)
    if args.plan:
        print(json.dumps({"selected":stages,"selection":why},ensure_ascii=False,indent=2))
    else:
        sys.exit(0 if run(args,stages,why) else 1)
