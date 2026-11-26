"""Unified profile-driven launcher and preflight check for Edge sensor calibration stations."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from calibration.hardware_profile import validate_board_metrics, validate_hardware_profile

MODES = {
    "sim-stationary": {
        "project": "edge-board-cal",
        "compose": [
            "compose.calibration-gazebo.yaml",
            "compose.calibration-board.yaml",
            "compose.calibration-model.yaml",
            "compose.calibration-console.yaml",
        ],
        "default_profile": "calibration/station/gazebo-board.json",
        "port_env": "EDGE_CALIBRATION_CONSOLE_PORT",
        "default_port": 8911,
        "motion_authority": "none (stationary simulation rig; operator moves board)",
    },
    "sim-mobile": {
        "project": "edge-fixed-cal",
        "compose": [
            "compose.calibration-board.yaml",
            "compose.calibration-console.yaml",
            "compose.calibration-model.yaml",
            "compose.calibration-fixed-rover.yaml",
            "compose.calibration-view-motion.yaml",
        ],
        "default_profile": "calibration/station/gazebo-fixed-rover.json",
        "port_env": "EDGE_FIXED_CAL_CONSOLE_PORT",
        "default_port": 8916,
        "motion_authority": "simulation_only (Gazebo domain 197; blocked on hardware by guard_site)",
    },
    "hardware": {
        "project": "edge-hw-cal",
        "compose": [
            "compose.calibration-board.yaml",
            "compose.calibration-console.yaml",
            "compose.calibration-hardware.yaml",
        ],
        "default_profile": None,
        "port_env": "EDGE_CALIBRATION_CONSOLE_PORT",
        "default_port": 8911,
        "motion_authority": "none (read-only sensor station; chassis remains stationary/locked)",
    },
}


def build_plan(mode: str, *, project: str | None = None, profile_path: Path | None = None,
               passive_livox: bool = False, with_clock: bool = False,
               ollama_model: str | None = None, env: dict | None = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"unsupported mode: {mode}")
    if passive_livox and mode != "hardware":
        raise ValueError("--passive-livox applies only to --mode hardware")
    spec = MODES[mode]
    environ = dict(os.environ if env is None else env)
    compose_files = list(spec["compose"])
    if passive_livox:
        compose_files.append("compose.calibration-passive-livox.yaml")
    compose_profiles = ["time-calibration"] if with_clock and mode in ("sim-mobile", "hardware") else []
    resolved_profile = profile_path
    if resolved_profile is None:
        if mode == "hardware":
            raw = environ.get("EDGE_CAL_HW_PROFILE", "").strip()
            resolved_profile = Path(raw) if raw else None
        else:
            resolved_profile = ROOT / spec["default_profile"]
    selected_model = (ollama_model or environ.get("EDGE_CAL_OLLAMA_MODEL", "")).strip() or "qwen3:4b-instruct"
    bind = environ.get("EDGE_CALIBRATION_CONSOLE_BIND", "127.0.0.1")
    port = int(environ.get(spec["port_env"], str(spec["default_port"])))
    proj = project or spec["project"]
    cmd = ["docker", "compose", "-p", proj]
    for cf in compose_files:
        cmd.extend(["-f", cf])
    for cp in compose_profiles:
        cmd.extend(["--profile", cp])
    return {
        "mode": mode,
        "project": proj,
        "compose_files": compose_files,
        "compose_profiles": compose_profiles,
        "profile_path": str(resolved_profile) if resolved_profile else None,
        "ollama_model": selected_model,
        "ollama_model_override": ollama_model.strip() if ollama_model and ollama_model.strip() else None,
        "console_url": f"http://{bind}:{port}/",
        "motion_authority": spec["motion_authority"],
        "compose_base_argv": cmd,
    }


def preflight(plan: dict, *, env: dict | None = None, check_live: bool = False) -> dict:
    environ = dict(os.environ if env is None else env)
    issues: list[str] = []
    warnings: list[str] = []
    derived_env: dict[str, str] = {}
    if plan.get("ollama_model_override"):
        derived_env["EDGE_CAL_OLLAMA_MODEL"] = plan["ollama_model_override"]
    target_model = plan.get("ollama_model") or "qwen3:4b-instruct"
    if "thinking" in target_model.lower():
        warnings.append(
            f"model {target_model!r} is a pure thinking checkpoint and may add 10-40s CoT latency per step; "
            "prefer 'qwen3:4b-instruct' or hybrid 'qwen3:8b' (auto think=false)"
        )
    for cf in plan["compose_files"]:
        if not (ROOT / cf).is_file():
            issues.append(f"missing compose file: {cf}")
    profile = None
    profile_str = plan["profile_path"]
    if not profile_str:
        issues.append("set --profile or EDGE_CAL_HW_PROFILE to an installed-sensors JSON file")
    else:
        p = Path(profile_str)
        if plan["mode"] == "hardware" and not p.is_absolute():
            p = p.resolve()
        if not p.is_file():
            issues.append(f"profile file not found: {p}")
        else:
            try:
                profile = json.loads(p.read_bytes())
                if plan["mode"] == "hardware":
                    validate_hardware_profile(profile, require_clock="time-calibration" in plan["compose_profiles"])
                    derived_env["EDGE_CAL_HW_PROFILE"] = str(p)
                    if not environ.get("EDGE_CAL_ROBOT_ID") and profile.get("robot_id"):
                        derived_env["EDGE_CAL_ROBOT_ID"] = profile["robot_id"]
                else:
                    if profile.get("environment") != "simulation":
                        issues.append("simulation modes require profile.environment == 'simulation'")
                    validate_board_metrics(profile)
            except (KeyError, TypeError, ValueError, OSError) as exc:
                issues.append(f"invalid sensor profile: {exc}")

    if plan["mode"] == "hardware":
        merged = {**environ, **derived_env}
        for required in ("EDGE_CAL_HOST_GATEWAY", "EDGE_CAL_ROS_DOMAIN", "EDGE_CAL_OLLAMA_URL", "EDGE_CAL_ROBOT_ID"):
            if not str(merged.get(required, "")).strip():
                issues.append(f"missing required hardware environment variable: {required}")
        if "compose.calibration-passive-livox.yaml" in plan["compose_files"]:
            for required in ("EDGE_CAL_HW_OBSERVER_IMAGE", "EDGE_CAL_LIVOX_IFACE", "EDGE_CAL_LIVOX_DEST",
                             "EDGE_CAL_LIVOX_IP", "EDGE_CAL_LIVOX_ID", "EDGE_CAL_LIVOX_FRAME"):
                if not str(merged.get(required, "")).strip():
                    issues.append(f"missing passive-livox environment variable: {required}")
        if check_live and not issues and profile is not None:
            ollama = merged["EDGE_CAL_OLLAMA_URL"].rstrip("/") + "/api/tags"
            try:
                with urlopen(ollama, timeout=4) as resp:
                    tags = json.loads(resp.read(65536))
                models = [m.get("name", "") for m in (tags.get("models") or []) if isinstance(m, dict)]
                if not models:
                    issues.append(f"Ollama at {ollama} reported zero installed models")
                else:
                    normalized_available = {m for name in models for m in (name, name.removesuffix(":latest"))}
                    if target_model not in normalized_available:
                        issues.append(
                            f"configured model {target_model!r} not found in Ollama at {ollama} "
                            f"(installed: {', '.join(sorted(filter(None, models)))})"
                        )
            except Exception as exc:
                issues.append(f"Ollama unreachable at {ollama}: {exc}")
            topics = [profile["camera"]["topic"], profile["camera"]["info_topic"],
                      *(s["topic"] for s in profile["lidars"])]
            ros_env = {**merged, "ROS_DOMAIN_ID": str(merged["EDGE_CAL_ROS_DOMAIN"])}
            for topic in topics:
                proc = subprocess.run(["ros2", "topic", "info", topic], env=ros_env,
                                      capture_output=True, text=True, timeout=6, check=False)
                if proc.returncode != 0 or "Publisher count: 0" in proc.stdout:
                    issues.append(f"ROS topic has no active publisher on domain {ros_env['ROS_DOMAIN_ID']}: {topic}")

    return {
        "ok": not issues,
        "mode": plan["mode"],
        "project": plan["project"],
        "ollama_model": target_model,
        "console_url": plan["console_url"],
        "motion_authority": plan["motion_authority"],
        "derived_env": derived_env,
        "warnings": warnings,
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "preflight", "up", "down", "status"))
    parser.add_argument("--mode", choices=tuple(MODES), default="sim-mobile")
    parser.add_argument("--project", help="override Docker Compose project name")
    parser.add_argument("--profile", type=Path, help="sensor profile JSON (required on hardware unless EDGE_CAL_HW_PROFILE is set)")
    parser.add_argument("--ollama-model", help="override calibration planner model tag, e.g. qwen3:4b-instruct or qwen3:8b")
    parser.add_argument("--passive-livox", action="store_true", help="include passive Livox UDP receiver on hardware")
    parser.add_argument("--with-clock", action="store_true", help="enable temporal offset calibration observer")
    parser.add_argument("--check-live", action="store_true", help="probe live Ollama and ROS 2 publishers on hardware")
    args = parser.parse_args(argv)

    plan = build_plan(args.mode, project=args.project, profile_path=args.profile,
                      passive_livox=args.passive_livox, with_clock=args.with_clock,
                      ollama_model=args.ollama_model)
    if args.action == "plan":
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    report = preflight(plan, check_live=args.check_live)
    if args.action == "preflight":
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 2
    if args.action == "up" and not report["ok"]:
        print(json.dumps(report, indent=2, ensure_ascii=False), file=sys.stderr)
        return 2
    run_env = {**os.environ, **report["derived_env"]}
    tail = {"up": ["up", "-d"], "down": ["down"], "status": ["ps", "-a"]}[args.action]
    completed = subprocess.run([*plan["compose_base_argv"], *tail], cwd=ROOT, env=run_env, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
