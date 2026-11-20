"""Single-robot system verification. Run from any directory with host Python 3, pwsh and Docker."""
import argparse
import json
import os
import re
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


def parse_run_id(value):
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", value):
        raise argparse.ArgumentTypeError("expected YYYYMMDDTHHMMSSZ-xxxxxxxx (lowercase hex)")
    try:
        datetime.strptime(value[:16], "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid UTC timestamp in run ID") from exc
    return value


def run(args, selected, reasons):
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    directory = args.out_root.resolve() / run_id
    directory.mkdir(parents=True)
    report_file = directory / "summary.json"
    env = dict(os.environ)
    revision = output(["git","rev-parse","HEAD"], env)
    dirty = bool(output(["git","status","--porcelain"], env))
    # Never import the developer's planner URL, plugin directories, port or project name.
    env = {k:v for k,v in env.items() if not k.startswith(("EDGE_", "COMPOSE_"))}
    image = "edge-agent-ros:system-" + run_id.lower()
    env.update(COMPOSE_DISABLE_ENV_FILE="1", EDGE_IMAGE=image, EDGE_ROS_DOMAIN_ID="103",
               EDGE_CONSOLE_PORT="0", EDGE_EVIDENCE_PORT="0", EDGE_REVISION=revision+("-dirty" if dirty else ""),
               EDGE_EVIDENCE_DIR=str(directory), EDGE_VERIFY_ROOT=str(directory/"core"),
               EDGE_RESILIENCE_REPORT=str(directory/"resilience.json"), PYTHONUTF8="1")
    if args.base_image:
        env["EDGE_BASE_IMAGE"] = args.base_image
    # The operator's active planner can route work to a billed external API.
    # Keep deployment-specific profiles outside all ephemeral regression projects.
    contracts_dir = directory / "contracts"
    shutil.copytree(ROOT / "contracts", contracts_dir,
                    ignore=shutil.ignore_patterns("planner-profile.json", ".planner-history"))
    env["EDGE_CONTRACTS_DIR"] = str(contracts_dir)
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
                                test("document_contract.py","-v"),test("test_package_boundaries.py","-v"),
                                test("test_kernel.py","-v"),test("test_offline_analysis.py","-v"),
                                test("test_conformance.py","-v"),
                                test("test_selection.py","-v"),test("test_robot_release.py","-v"),
                                test("test_mission_recording.py","-v"),
                                test("test_task_recorder.py","-v"),test("test_recorder_cli.py","-v"),
                                test("test_task_recording_source.py","-v"),test("test_recording_trace_view.py","-v"),
                                test("test_recording_global_map.py","-v"),test("test_live_exploration_map.py","-v"),
                                test("test_recording_map_view.py","-v"),
                                test("test_campus_revisit.py","-v"),
                                test("test_campus_survey_worker.py","-v"),
                                ["docker","compose","-f","compose.yaml","-f","compose.task-recorder.yaml","config","--quiet"],
                                ["docker","compose","-f","compose.yaml","-f","compose.task-recorder.yaml",
                                 "-f","compose.task-recorder-campus.yaml","config","--quiet"],
                                ["docker","compose","-f","compose.hardware.yaml","-f","compose.task-recorder.yaml","config","--quiet"],
                                ["docker","compose","config","--quiet"],
                                ["docker","compose","-f","compose.hardware.yaml","config","--quiet"],
                                ["docker","compose","-f","compose.yaml","-f","compose.camera.yaml","-f","compose.rgbd.yaml",
                                 "-f","compose.rgbd-sensor.yaml","-f","compose.model-provider.yaml","config","--quiet"],
                                ["docker","compose","-f","compose.hardware.yaml","-f","compose.camera.yaml",
                                 "-f","compose.camera-hardware.yaml","-f","compose.rgbd.yaml","-f","compose.rgbd-hardware.yaml",
                                 "-f","compose.model-provider.yaml","-f","compose.model-provider-hardware.yaml","config","--quiet"]])
        if "planner" in selected:
            stage("planner",[test("planning_contract.py","-v"),test("test_derived_tools.py","-v"),
                             test("test_planner_model_selection.py","-v"),
                             [PY,str(ROOT/"model_provider/test_provider.py"),"-v"],
                             test("test_map3d.py","-v"),test("test_rgbd.py","-v"),
                             [PY,str(ROOT/"perception/camera/test_frames.py"),"-v"],
                             test("test_series_tools.py","-v"),test("test_plugin_design.py","-v"),
                             test("test_plugin_design_watch.py","-v"),
                             test("test_software_author_watch.py","-v"),
                             test("test_software_provider.py","-v"),test("test_watchdog.py","-v"),
                             test("test_runtime_plugins.py","-v"),
                             test("test_soft_runtime.py","-v"),test("test_plugin_descriptor.py","-v"),
                             test("test_v2_registry.py","-v"),test("test_soft_host.py","-v"),
                             test("test_physical_ledger.py","-v"),test("test_plugin_tracks.py","-v"),
                             test("test_plugin_tracks_metrics.py","-v"),test("test_model_readiness.py","-v"),
                             test("test_module_journal.py","-v"),test("test_author_eventbus.py","-v"),
                             test("test_plugin_manager.py","-v"),test("test_plugin_manager_watch.py","-v"),
                             [PY,str(ROOT/"examples/result-verifiers/test_verifiers.py"),"-v"],
                             test("test_author_tool.py","-v"),test("test_plugin_author_watch.py","-v"),
                             test("system/verify_sample_provider.py","--out",directory/"provider-contract.json")])
        if "board" in selected:
            env["EDGE_HARDWARE_IMAGE"]=board_image
            board_built=stage("board-build",[["docker","compose","-f","compose.hardware.yaml","build","core"]])
            if board_built:
                # The deployable board image ships plugins without development fixtures.
                # Bind tests only; imports and subprocesses must use the image's runtime.
                board_container = ["docker","run","--rm","--network","none","--read-only","--tmpfs","/tmp",
                              "-e","PYTHONDONTWRITEBYTECODE=1","-e","ROS_LOG_DIR=/tmp/ros",
                              "-e","ROS_DOMAIN_ID=184","-e","ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST",
                              "-e",f"FACTORY_TEST_IMAGE={board_image}",
                              "--mount",f"type=bind,source={ROOT/'tests'},target=/opt/edge/tests,readonly",
                              "--mount",f"type=bind,source={directory},target=/evidence"]
                board_python = [board_image,"python3","-B"]
                board_test = [*board_container,*board_python]
                stage("board",[test("test_board_preflight.py","-v"),test("test_site_pack.py","-v"),
                               test("test_board_plugins.py","-v"),
                               [*board_test,"/opt/edge/tests/system/navigation_contract.py",
                                "--out","/evidence/navigation-contract.json"],
                               [*board_container,"--mount",f"type=bind,source={ROOT/'task_recorder'},target=/opt/edge/task_recorder,readonly",
                                *board_python,"/opt/edge/tests/test_plugin_conformance.py","--out","/evidence/plugin-conformance.json"],
                               test("system/board_pack_acceptance.py","--image",board_image,"--root",ROOT,
                                    "--out",directory/"board-pack.json"),
                               [*board_test,"/opt/edge/tests/test_factory_targets.py","-v"],
                               [*board_test,"/opt/edge/tests/system/factory_ros_capture.py",
                                "--out","/evidence/factory-ros-capture.json"],
                               test("system/recording_board_smoke.py","--image",board_image,
                                    "--out",directory/"board-recording.json"),
                               test("system/board_smoke.py","--image",board_image,"--out",directory/"board.json"),
                               test("system/realsense_board.py","--image",board_image,"--out",directory/"realsense-board.json")])
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
                    "rover-commissioning":[in_image("python3","-B","/opt/edge/tests/test_factory_calibration.py","-v"),
                                           in_image("python3","-B","/opt/edge/tests/test_exploration_frontier.py","-v"),
                                           test("system/commission_exploration.py","--packaged","--image",image,
                                                "--out-dir",directory/"rover-commissioning")],
                    "plugin-isolation":[test("system/soft_runtime_acceptance.py","--image",image,
                                             "--out",directory/"soft-runtime.json"),
                                        in_image("python3","-B","/opt/edge/tests/test_physical_camera.py","-v"),
                                        test("system/physical_camera_acceptance.py","--image",image,
                                             "--out",directory/"physical-camera.json"),
                                        test("test_map_texture_pack.py","-v"),
                                        test("test_sensor_incidents.py","-v"),
                                        ["docker","run","--rm","--network","none","--read-only","--tmpfs","/tmp",
                                         "-e","ROS_LOCALHOST_ONLY=1","-e","ROS_LOG_DIR=/tmp/ros",
                                         "--mount",f"type=bind,source={directory},target=/proof",image,"python3","-B",
                                         "/opt/edge/tests/system/sensor_health_acceptance.py","--out","/proof/sensor-health.json"],
                                        test("system/sensor_health_restart.py","--image",image,
                                             "--out",directory/"sensor-incident-crash.json"),
                                        ["node","--test",str(ROOT/"tests/map_planar.test.mjs"),str(ROOT/"tests/map_spatial.test.mjs")],
                                        test("system/map_model_isolation.py","--image",image,
                                             "--out",directory/"map-model-isolation.json"),
                                        test("system/camera_acceptance.py","--image",image,"--out",directory/"camera.json"),
                                        test("system/sensor_plugin_isolation.py","--image",image,"--out",directory/"camera-lidar.json"),
                                        test("system/rgbd_acceptance.py","--image",image,"--out",directory/"rgbd.json")],
                    "agent-contracts":[in_image("python3","/opt/edge/tests/test_status_plugin.py","-v"),
                                       in_image("python3","/opt/edge/tests/test_capability_catalog.py","--http","-v"),
                                       in_image("python3","/opt/edge/tests/agent_trace_contract.py","-v"),
                                       in_image("python3","/opt/edge/tests/agent_verification_contract.py","-v"),
                                       ["docker","run","--rm","--network","none","--read-only","--tmpfs","/tmp",
                                        "--cap-drop","ALL","--user","65534:65534","--mount",
                                        f"type=bind,source={ROOT/'examples/result-verifiers'},target=/sample,readonly",
                                        "--entrypoint","python3",image,"-B","/sample/test_verifiers.py","-v"]],
                    "observations":[in_image("python3","-m","unittest","discover","-v","-s","/opt/edge/console/server"),
                                    in_image("python3","-B","/opt/edge/tests/test_console_facts.py","-v"),
                                    in_image("python3","-B","/opt/edge/tests/test_map3d_ros.py","-v"),
                                    in_image("python3","-B","/opt/edge/tests/test_lidar_inputs_ros.py","-v"),
                                    in_image("python3","-m","unittest","discover","-v","-s","/opt/edge/evidence"),
                                    in_image("python3","-m","unittest","discover","-v","-s","/opt/edge/sessions_plugin"),
                                    test("system/module_bus_acceptance.py","--image",image,"--out",directory/"module-bus.json"),
                                    test("system/event_backlog_probe.py","--image",image,"--out",directory/"event-backlog.json"),
                                    test("system/watchdog_acceptance.py","--image",image,"--out",directory/"watchdog.json"),
                                    test("system/author_bus_acceptance.py","--image",image,"--out",directory/"author-bus.json")],
                    "scene-contracts":[in_image("python3","-m","unittest","discover","-v","-s","/opt/edge/tests","-p","test_scene*.py")],
                    "core":[script("verify.ps1","-SkipBuild")],
                    "motion-sensors":[in_image("python3","/opt/edge/tests/system/motion_faults.py","--out","/evidence/motion-sensors.json")],
                    "agent-faults":[test("system/agent_faults.py","--image",image,"--out",directory/"agent-faults.json"),
                                    test("system/software_provider_acceptance.py","--automatic","--image",image,
                                         "--out",directory/"software-author-auto.json"),
                                    test("system/software_provider_acceptance.py","--automatic","--scenario","word-count-correction",
                                         "--image",image,"--out",directory/"software-author-correction.json"),
                                    test("system/plugin_design_acceptance.py","--image",image,"--out",directory/"plugin-design.json"),
                                    test("system/software_provider_acceptance.py","--image",image,"--out",directory/"software-provider.json"),
                                    test("system/plugin_manager_acceptance.py","--image",image,"--out",directory/"plugin-manager.json"),
                                    test("system/lifecycle_acceptance.py","--image",image,"--out-root",directory/"lifecycle"),
                                    test("system/derived_tool_acceptance.py","--image",image,"--out",directory/"derived-tool.json"),
                                    test("system/verify_sample_provider.py","--image",image,"--out",directory/"provider-hotplug.json"),
                                    test("system/runtime_plugin_acceptance.py","--image",image,"--out",directory/"runtime-plugins.json")],
                    "resilience":[test("console_resilience.py")],
                }
                if board_built:
                    checks["observations"].append(test("system/evolution_mounts.py","--image",image,
                        "--hardware-image",board_image,"--out",directory/"evolution-mounts.json"))
                    checks["observations"].append(test("system/watchdog_mounts.py","--image",image,
                        "--hardware-image",board_image,"--out",directory/"watchdog-mounts.json"))
                if effective_model:
                    checks["agent-faults"].append(test("system/plugin_design_acceptance.py","--image",image,
                        "--cache",args.ollama_cache_volume,"--out",directory/"plugin-design-model.json"))
                    checks["agent-faults"].append(test("system/software_provider_acceptance.py","--image",image,
                        "--model-cache",args.ollama_cache_volume,"--out",directory/"software-provider-model.json"))
                    checks["agent-faults"].append(test("system/series_author_acceptance.py","--image",image,
                        "--cache",args.ollama_cache_volume,"--out",directory/"authored-series.json"))
                    checks["agent-faults"].append(test("system/plugin_manager_acceptance.py","--image",image,
                        "--ollama-cache-volume",args.ollama_cache_volume,"--out",directory/"plugin-manager-model.json"))
                    checks["agent-faults"].append(test("system/software_provider_acceptance.py","--automatic","--image",image,
                        "--model-cache",args.ollama_cache_volume,"--scenario","uppercase",
                        "--out",directory/"software-author-auto-model.json"))
                if "campus-world" in selected:
                    assets = args.campus_assets
                    feedback_layout = directory / "feedback-layout.json"
                    feedback_layout.write_text('{"revision":"invalid-feedback-fixture"}\n', encoding="utf-8")
                    checks["campus-world"] = [in_image("python3","-B","/opt/edge/tests/test_campus_texture.py","-v"),
                                              in_image("python3","-B","/opt/edge/tests/test_campus_mesh.py","-v"),
                                              in_image("python3","-B","/opt/edge/tests/test_campus_robot_feedback.py","-v"),
                                              ["docker","run","--rm","--network","none","--tmpfs","/state",
                                               "-e","GZ_PARTITION=edge_invalid_feedback_fixture","-e","GZ_IP=127.0.0.1",
                                               "-e","ROS_DOMAIN_ID=171","-v",f"{feedback_layout}:/campus/layout.json:ro",
                                               image,"python3","-B","/opt/edge/tests/system/campus_invalid_feedback.py"],
                                              test("test_campus_facilities.py","-v"),
                                              test("test_campus_real_site.py","-v"),
                                              test("test_campus_route_overlay.py","-v"),
                                              test("test_campus_robot_assets.py","-v"),
                                              test("test_campus_assets.py","-v"),test("test_campus_deployment.py","-v"),
                                              test("test_campus_capture.py","-v")]
                    if assets or (ROOT / "artifacts/sim-assets/models/Pine Tree/model.sdf").is_file():
                        checks["campus-world"].append(test("system/campus_acceptance.py","--image",image,"--assets",
                                                       str((assets or ROOT/"artifacts/sim-assets").resolve()),
                                                       "--verify-reload","--verify-texture","--verify-onboard","--out",directory/"campus-world.json"))
                        checks["campus-world"].append(test("system/record_unknown_campus.py","--image",image,"--assets",
                                                       str((assets or ROOT/"artifacts/sim-assets").resolve()),
                                                       "--out",directory/"unknown-campus"))
                        for scenario,flags in (("camera-free-fault",["--no-cameras"]),("camera-free-continuous",["--no-cameras","--continuous"])):
                            checks["campus-world"].append(test("system/record_unknown_campus.py","--image",image,"--assets",
                                str((assets or ROOT/"artifacts/sim-assets").resolve()),*flags,"--out",directory/scenario))
                        checks["campus-world"].append(test("system/recorder_campus_binding.py","--image",image,
                                                           "--out",directory/"recording-camera-roles.json"))
                    else:
                        skip("campus-world-assets","optional campus assets not installed; use scene/campus_large/fetch_assets.py")
                for name, commands in checks.items():
                    if name in selected:
                        stage(name,commands)
                if "motion-world" in selected:
                    stage("body-runtime", [
                        in_image("ctest", "--test-dir", "/opt/edge/build/edge_gateway", "-R", "^body_authority_contract$",
                                 "--output-on-failure", "--no-tests=error"),
                        in_image("ctest", "--test-dir", "/tmp/edge-sim-build", "-R", "^body_runtime_endpoint$",
                                 "--output-on-failure", "--no-tests=error"),
                        test("system/body_runtime_acceptance.py", "--image", image, "--out", directory/"body-runtime.json"),
                    ])
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
                    services=["sim","bridge","witness","gateway","core","perception","scene","agent","console","session","evidence"]
                    # Public plugin tests write only to this run's capability directory.
                    plugin_dir=directory/"plugins"
                    plugin_dir.mkdir()
                    env["EDGE_PLUGINS_DIR"]=str(plugin_dir)
                    stage("session-register",[[*compose,"run","--rm","-T","--no-deps","session-admin"]])
                    started_sim=True
                    if effective_model:
                        override=directory/"model.override.json"
                        override.write_text(json.dumps({"services":{"model":{"volumes":["test-model-cache:/cache:ro"]}},
                            "volumes":{"test-model-cache":{"external":True,"name":args.ollama_cache_volume}}}),encoding="utf-8")
                        compose.extend(["-f",str(override),"--profile","model"])
                        # Ollama rebuilds its manifests on startup. Seed only model data into the
                        # isolated writable volume and keep the developer's identity and cache untouched.
                        seeded=stage("model-cache",[[*compose,"run","--rm","-T","--no-deps","--entrypoint","/bin/sh","model",
                            "-ec","test -d /cache/models/blobs && mkdir -p /root/.ollama/models && cp -a /cache/models/. /root/.ollama/models/"]])
                        if seeded:
                            env["EDGE_OLLAMA_URL"]="http://model:11434"
                            env["EDGE_OLLAMA_MODEL"]="qwen2.5:1.5b"
                            services.append("model")
                        else:
                            skip("model","could not initialize isolated model data; see model-cache.log","blocked")
                            public.remove("model")
                    ready=stage("simulation-start",[[*compose,"up","-d","--no-build",*services],
                                [*compose,"exec","-T","core","/usr/local/bin/edge-entrypoint","python3","/opt/edge/client/ready.py"]])
                    if ready:
                        if "model" in public and not stage("model-ready",[[*compose,"exec","-T","model","/bin/sh","-ec",
                                'for attempt in $(seq 1 20); do ollama show "$1" >/dev/null && exit 0; sleep 1; done; exit 1',
                                "model-ready",env["EDGE_OLLAMA_MODEL"]]]):
                            skip("model","model service or required weights unavailable; see model-ready.log","blocked")
                            public.remove("model")
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
                            if name=="ui":
                                stage("session-evidence",[test("system/session_evidence_acceptance.py","--url",current,
                                      "--project",project,"--registry",plugin_dir,"--out",directory/"session-evidence.json")])
                                stage("session-plugin",[test("system/session_plugin_acceptance.py","--url",current,
                                      "--project",project,"--registry",plugin_dir,"--image",image,
                                      "--out",directory/"session-plugin.json")])
                    else:
                        for name in public:
                            skip(name,"simulator did not become ready","blocked")
        if {"places","resilience","agent-faults"} <= set(selected):
            stage("plugin-frozen-missions",[test("system/plugin_tracks_metrics.py","--run",directory,
                                                "--out",directory/"plugin-frozen-missions.json")])
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
    parser.add_argument("--run-id",type=parse_run_id,help="use this UTC run ID for a new report directory")
    parser.add_argument("--base-image",help="preinstalled ROS/Gazebo SDK image")
    parser.add_argument("--campus-assets",type=Path,help="installed Gazebo Fuel campus cache for the offline world test")
    parser.add_argument("--ollama-cache-volume",help="existing Ollama home volume; mounts the model cache read-only in the isolated project")
    args=parser.parse_args()
    stages,why=selection(changed_paths(args.changed_since,ROOT) if args.changed_since else None)
    if args.plan:
        print(json.dumps({"selected":stages,"selection":why},ensure_ascii=False,indent=2))
    else:
        sys.exit(0 if run(args,stages,why) else 1)
