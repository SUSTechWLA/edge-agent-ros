"""Run the Body Runtime handoff in a fresh, isolated Gazebo project."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


SCRIPT = Path(__file__).resolve()
MISSION = "body-lost-gateway"


def run_probe(directory, results):
    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data
    from std_msgs.msg import String

    sys.path.insert(0, str(SCRIPT.parents[1]))
    from verify import Probe, position, require

    rclpy.init()
    node = Probe()
    states = []
    state_arrivals = {}
    state_samples = 0
    drives = {}
    odom_stamps = []

    def state(message):
        nonlocal state_samples
        state_samples += 1
        version, boot, epoch, phase, owner = message.data.split()
        require(version == "v1", "unknown body protocol")
        row = (boot, int(epoch), phase, owner)
        if not states or states[-1] != row:
            states.append(row)
            state_arrivals[row] = time.monotonic() - node.start_time

    def command(message):
        fields = message.data.split()
        if len(fields) == 8 and fields[4] == "drive" and abs(float(fields[6])) > .05:
            drives[tuple(fields[:4])] = message.data

    def odom(message):
        stamp = message.header.stamp
        odom_stamps.append((stamp.sec, stamp.nanosec))

    node.create_subscription(String, "/edge/body/state", state, qos_profile_sensor_data)
    node.create_subscription(String, "/edge/body/command", command, qos_profile_sensor_data)
    node.create_subscription(Odometry, "/odom", odom, qos_profile_sensor_data)
    writer = node.create_publisher(String, "/edge/body/command", 10)

    def record_window(rows):
        require(len(rows) >= 3, "insufficient Gazebo world samples")
        intervals = [b["sim_s"] - a["sim_s"] for a, b in zip(rows, rows[1:])]
        wall_gaps = [b["t"] - a["t"] for a, b in zip(rows, rows[1:])]
        require(min(intervals) >= 0 and max(intervals + wall_gaps) < .3, "gap in world witness")
        require(rows[-1]["sim_s"] - rows[0]["sim_s"] >= .35, "world witness did not advance")
        drift = max(math.dist(position(rows[0]), position(s)) for s in rows)
        speed = max(s["speed"] for s in rows)
        turn = max(abs(s["yaw_rate"]) for s in rows)
        require(drift < .025 and speed < .05 and turn < .10, f"world moved: {drift}, {speed}, {turn}")
        return {"samples": len(rows), "drift_m": drift, "max_speed_mps": speed, "max_yaw_rps": turn}

    def challenge(packets):
        first = len(node.samples)
        odom_start = len(odom_stamps)
        state_count = len(states)
        heartbeat_count = state_samples
        require(states[-1][2] == "ready", "endpoint did not advertise availability")
        node.wait(lambda: writer.get_subscription_count() >= 2, 10, "no ROS command bridge")
        for _ in range(14):
            for packet in packets:
                writer.publish(String(data=packet))
            node.spin_for(.08)
        require(len(states) == state_count, f"stale input changed endpoint state: {states[state_count:]}")
        require(state_samples - heartbeat_count >= 3, "endpoint state stopped during replay")
        require(len(set(odom_stamps[odom_start:])) >= 3, "wheel odometry did not advance")
        return record_window(node.samples[first:])

    def request_fault(name):
        (directory / (name + ".request")).touch()

    def completed(name, seconds):
        node.wait(lambda: (directory / (name + ".done")).exists(), seconds, name)

    try:
        node.ready()
        node.wait(lambda: states and states[-1][2] == "ready", 35, "no initial stationary offer")
        start = len(node.samples)
        require(node.submit("body-handoff", [(.6, 0.)]).accepted, "first mission refused")
        final = node.terminal("body-handoff", 65)
        require(final.state == "SUCCEEDED" and final.verified_count == 1, f"first mission: {final}")
        held = next(s for s in states if s[2] == "held")
        node.wait(lambda: states[-1][2] == "ready" and states[-1][1] > held[1], 8, "first handoff")
        travelled = math.dist(position(node.samples[start]), position(node.samples[-1]))
        require(travelled > .35, "mission receipt without world displacement")
        results["handoff"] = {"old": held, "next": states[-1], "distance_m": travelled,
                              "state": final.state, "stationary": node.stationary()}

        require(node.submit(MISSION, [(2.7, 0.)]).accepted, "second mission refused")
        node.wait(lambda: states[-1][2] == "held" and node.samples[-1]["speed"] > .10, 20, "moving claim")
        boot, epoch, _, owner = states[-1]
        prefix = ("v1", boot, str(epoch), owner)
        node.wait(lambda: prefix in drives, 3, "no nonzero wire command to delay")
        drive = drives[prefix]
        old_open = " ".join((*prefix, "open"))
        moving = node.samples[-1]
        requested = time.monotonic() - node.start_time
        request_fault("kill-gateway")
        completed("kill-gateway", 10)
        confirmed_exit = time.monotonic() - node.start_time
        node.wait(lambda: states[-1][0] == boot and states[-1][2] == "ready" and states[-1][1] > epoch,
                  5, "no expiry and handoff")
        ready = states[-1]
        quiet = (boot, ready[1], "quiet", "-")
        require(quiet in state_arrivals, "missing observed quiet transition during command loss")
        quiet_seen, ready_seen = state_arrivals[quiet], state_arrivals[ready]
        require(requested <= quiet_seen <= ready_seen, "invalid handoff observation order")
        stopped = node.stationary()
        times = {"kill_requested": requested, "gateway_exit_confirmed": confirmed_exit,
                 "quiet_observed": quiet_seen, "ready_observed": ready_seen}
        events = {}
        for event, stamp in times.items():
            before = next(s for s in reversed(node.samples) if s["t"] <= stamp)
            after = next(s for s in node.samples if s["t"] >= stamp)
            require(after["t"] - before["t"] < .30, f"gap in world truth around {event}")
            events[event] = {"probe_s": round(stamp, 6), "world_before": before, "world_after": after}
        timing = {"clock": "seconds since probe start on its monotonic clock; world.t uses this same origin",
                  "semantics": "The host issues SIGKILL and checks container exit between kill_requested and "
                               "gateway_exit_confirmed. State times mark receipt at the probe. Actual process death "
                               "and endpoint revocation times are not directly measured. world.sim_s is simulation time.",
                  "events": events, "request_to_quiet_s": round(quiet_seen - requested, 6),
                  "request_to_ready_s": round(ready_seen - requested, 6)}
        results["command_loss"] = {"held": [boot, epoch, owner], "next": states[-1],
                                   "moving_mps": moving["speed"], "stopped": stopped, "timing": timing}
        results["delayed_commands"] = {"packets": [old_open, drive], "world": challenge([old_open, drive])}

        request_fault("restart-sim")
        completed("restart-sim", 45)
        node.wait(lambda: states[-1][0] != boot and states[-1][2] == "ready", 45, "no new boot and witness")
        current = states[-1]
        # Also challenge the old boot with a matching advertised generation.
        cross_boot = list(drive.split())
        cross_boot[2] = str(current[1])
        packets = [old_open, drive, f"v1 {boot} {current[1]} {owner} open", " ".join(cross_boot)]
        results["restart"] = {"new": current, "packets": packets, "world": challenge(packets)}

        request_fault("recover-host")
        completed("recover-host", 75)
        stopped = node.stationary()
        require(states[states.index(current):] == [current], "interrupted operation reclaimed motion")
        results["recovery"] = json.loads((directory / "recovery.json").read_text())
        results["recovery"]["world"] = stopped
    finally:
        results["states"] = states
        (directory / "measurements.json").write_text(json.dumps(results), encoding="utf8")
        node.destroy_node()
        rclpy.shutdown()


def query_recovery():
    # Match a newly started client to the recovered Core's DDS participant.
    import rclpy
    sys.path.insert(0, str(SCRIPT.parents[1]))
    from verify import Probe, require
    rclpy.init()
    node = Probe()
    try:
        node.ready()
        prior = node.terminal(MISSION, 25)
        require(prior.state == "RECOVERY_REQUIRED" and prior.verified_count == 0, f"old mission: {prior}")
        repeat = node.submit(MISSION, [(2.7, 0.)])
        require(repeat.accepted and repeat.state == prior.state, f"unexpected resubmission: {repeat}")
        recovery = {"mission": MISSION, "state": prior.state, "verified_count": prior.verified_count}
        Path("/evidence/recovery.json").write_text(json.dumps(recovery), encoding="utf8")
    finally:
        node.destroy_node()
        rclpy.shutdown()


def run(image, output):
    if output.exists():
        raise FileExistsError(f"use a new report path: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    root = SCRIPT.parents[2]
    project = "edge-test-body-" + uuid.uuid4().hex[:12]
    compose = ["docker", "compose", "-p", project, "-f", "compose.yaml", "-f", "compose.body-runtime.yaml"]
    report = {"passed": False, "project": project, "image": image, "scope": "isolated Gazebo simulation"}
    with tempfile.TemporaryDirectory(prefix=project) as temp:
        work = Path(temp)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("EDGE_", "COMPOSE_"))}
        env.update(COMPOSE_DISABLE_ENV_FILE="1", EDGE_IMAGE=image, EDGE_ROS_DOMAIN_ID="151",
                   EDGE_EVIDENCE_DIR=str(work), EDGE_SCENE_FILE="")

        def dc(*args, timeout=70):
            subprocess.run([*compose, *args], check=True, cwd=root, env=env, timeout=timeout)

        try:
            inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]
            report["image_id"] = inspected["Id"]
            report["image_revision"] = (inspected["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
            dc("up", "-d", "--no-build", "sim", "bridge", "witness", "body-bridge", "gateway", "core")
            command = [*compose, "run", "--rm", "--no-deps", "-T", "-v",
                       f"{SCRIPT}:/opt/edge/tests/system/body_runtime_acceptance.py:ro", "probe",
                       "python3", "-B", "/opt/edge/tests/system/body_runtime_acceptance.py", "--inside"]
            with (work / "probe.log").open("w", encoding="utf8") as log:
                process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    actions = [("kill-gateway", ("kill", "-s", "SIGKILL", "gateway")),
                               ("restart-sim", ("restart", "sim")),
                               ("recover-host", ("up", "-d", "--no-build", "--force-recreate", "--no-deps", "gateway", "core"))]
                    for name, args in actions:
                        started = time.monotonic()
                        while not (work / (name + ".request")).exists():
                            if process.poll() is not None or time.monotonic() - started > 160:
                                raise RuntimeError(f"probe did not request {name}")
                            time.sleep(.05)
                        dc(*args)
                        if name == "kill-gateway":
                            gateway = subprocess.check_output([*compose, "ps", "--all", "--quiet", "gateway"],
                                                              text=True, cwd=root, env=env).strip()
                            status = subprocess.check_output(["docker", "inspect", "--format", "{{json .State}}", gateway],
                                                             text=True)
                            exit_state = json.loads(status)
                            if exit_state["Running"] or exit_state["ExitCode"] != 137:
                                raise RuntimeError("gateway did not exit after SIGKILL")
                        elif name == "recover-host":
                            subprocess.run([*command[:-1], "--query-recovery"], cwd=root, env=env,
                                           check=True, timeout=65)
                        (work / (name + ".done")).touch()
                    if process.wait(timeout=55):
                        raise RuntimeError("probe failed")
                    report["passed"] = True
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
        except Exception as exc:
            report["error"] = str(exc)
            report["services"] = subprocess.run([*compose, "logs", "--no-color", "--tail", "12", "core", "gateway"],
                                                cwd=root, env=env, capture_output=True, text=True).stdout[-5000:]
        finally:
            for path, key in [("probe.log", "probe_log"), ("measurements.json", "measurements")]:
                if (work / path).exists():
                    value = (work / path).read_text(encoding="utf8", errors="replace")
                    report[key] = json.loads(value) if path.endswith(".json") else value[-4000:]
            try:
                dc("down", "--volumes", "--remove-orphans", "--timeout", "3")
            finally:
                output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    if not report["passed"]:
        raise RuntimeError(f"{report.get('error')}; see {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--query-recovery", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.inside:
        run_probe(Path("/evidence"), {})
    elif options.query_recovery:
        query_recovery()
    elif options.image and options.out:
        run(options.image, options.out)
    else:
        parser.error("--image and --out are required")
