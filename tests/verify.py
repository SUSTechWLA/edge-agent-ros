"""Black-box verification through the released ROS interfaces and a Gazebo world-pose witness."""
import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import rclpy
from geometry_msgs.msg import Point
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from edge_interfaces.srv import CancelMission, GetMission, SubmitMission


TARGETS = [(0.8, 0.0), (0.8, 0.7)]
TERMINAL = {"SUCCEEDED", "CANCELED", "FAILED", "RECOVERY_REQUIRED"}


def require(ok, reason):
    if not ok:
        raise AssertionError(reason)


def position(row):
    return row["x"], row["y"]


class Probe(Node):
    def __init__(self):
        super().__init__("edge_verifier")
        self.submit_client = self.create_client(SubmitMission, "/edge/submit_mission")
        self.query_client = self.create_client(GetMission, "/edge/get_mission")
        self.cancel_client = self.create_client(CancelMission, "/edge/cancel_mission")
        self.samples = []
        self.report = {}
        self.start_time = time.monotonic()
        self.create_subscription(Odometry, "/ground_truth", self.record, qos_profile_sensor_data)

    def record(self, message):
        pose = message.pose.pose.position
        twist = message.twist.twist
        stamp = message.header.stamp
        self.samples.append({
            "t": round(time.monotonic() - self.start_time, 4),
            "sim_s": round(stamp.sec + stamp.nanosec * 1e-9, 6),
            "x": round(pose.x, 6), "y": round(pose.y, 6),
            "speed": round(math.hypot(twist.linear.x, twist.linear.y), 6),
            "yaw_rate": round(twist.angular.z, 6),
        })

    def spin_for(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            rclpy.spin_once(self, timeout_sec=0.025)

    def wait(self, predicate, limit, reason):
        until = time.monotonic() + limit
        while time.monotonic() < until:
            if predicate():
                return
            rclpy.spin_once(self, timeout_sec=0.05)
        raise TimeoutError(reason)

    def ready(self):
        for client in (self.submit_client, self.query_client, self.cancel_client):
            require(client.wait_for_service(timeout_sec=40), f"service unavailable: {client.srv_name}")
        self.wait(lambda: len(self.samples) >= 3, 40, "no independent Gazebo world-pose samples")

    def call(self, client, request):
        future = client.call_async(request)
        self.wait(future.done, 8, f"no response: {client.srv_name}")
        return future.result()

    def submit(self, mission_id, points):
        return self.call(self.submit_client, SubmitMission.Request(
            mission_id=mission_id, waypoints=[Point(x=float(x), y=float(y)) for x, y in points]))

    def query(self, mission_id):
        return self.call(self.query_client, GetMission.Request(mission_id=mission_id))

    def terminal(self, mission_id, timeout=100):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            state = self.query(mission_id)
            if state.found and state.state in TERMINAL:
                return state
            self.spin_for(0.12)
        raise TimeoutError(f"{mission_id} did not terminate: {state}")

    def stationary(self, duration=0.8):
        start = len(self.samples)
        self.spin_for(duration)
        rows = self.samples[start:]
        require(len(rows) >= 3 and rows[-1]["sim_s"] > rows[0]["sim_s"], "no fresh stop evidence")
        drift = max(math.dist(position(rows[0]), position(r)) for r in rows)
        speed = max(r["speed"] for r in rows)
        turn = max(abs(r["yaw_rate"]) for r in rows)
        require(drift < 0.025 and speed < 0.05 and turn < 0.10,
                f"robot not stationary: drift={drift}, speed={speed}, yaw_rate={turn}")
        return {"duration_s": duration, "samples": len(rows), "drift_m": drift,
                "max_speed_mps": speed, "max_abs_yaw_rate_radps": turn}

    def goal_witnesses(self, rows):
        witnesses = []
        start = 0
        for goal in TARGETS:
            run = 0
            for index in range(start, len(rows)):
                row = rows[index]
                error = math.dist(position(row), goal)
                if error <= 0.18 and row["speed"] < 0.025 and abs(row["yaw_rate"]) < 0.05:
                    run += 1
                else:
                    run = 0
                if (run >= 3 and row["sim_s"] > rows[index - 2]["sim_s"] and
                        math.dist(position(row), position(rows[index - 2])) < 0.003):
                    witnesses.append({"goal": goal, "world": position(row), "error_m": error,
                                      "sim_s": row["sim_s"], "speed_mps": row["speed"],
                                      "yaw_rate_radps": row["yaw_rate"]})
                    start = index + 1
                    break
            else:
                raise AssertionError(f"no ordered stationary world-pose witness for {goal}")
        return witnesses

    def mission(self):
        require(math.dist(position(self.samples[-1]), (0, 0)) < 0.07, "suite requires a fresh world")
        checks = self.report["contracts"] = {}
        for name, points in (("empty", []), ("bounds", [(5, 0)]), ("nonfinite", [(float("nan"), 0)])):
            require(not self.submit(name, points).accepted and not self.query(name).found,
                    f"invalid request entered ledger: {name}")
            checks[name] = "rejected_without_record"
        start = len(self.samples)
        accepted = self.submit("inspect-ab", TARGETS)
        require(accepted.accepted, f"submission rejected: {accepted.detail}")
        repeat = self.submit("inspect-ab", TARGETS)
        require(repeat.accepted, "same request was not idempotent")
        require(not self.submit("inspect-ab", [(1, 1)]).accepted, "ID/content conflict accepted")
        require(not self.submit("competing", [(1, 1)]).accepted, "concurrent base claim accepted")
        checks.update(duplicate="accepted_once", conflict="rejected", busy="rejected")
        state = self.terminal("inspect-ab")
        require(state.state == "SUCCEEDED" and state.verified_count == 2 and len(state.evidence) == 2,
                f"incomplete mission: {state}")
        evidence = []
        for point, goal in zip(state.evidence, TARGETS):
            error = math.dist((point.x, point.y), goal)
            require(error <= 0.12, f"bad mission evidence: {error}")
            evidence.append({"goal": goal, "odom": (point.x, point.y), "error_m": error})
        self.report.update(mission_id="inspect-ab", state=state.state, evidence=evidence)
        self.report["stop"] = self.stationary()
        self.report["world_witnesses"] = self.goal_witnesses(self.samples[start:])
        require(any(abs(s["yaw_rate"]) > 0.15 for s in self.samples[start:]), "no physical turn observed")
        self.report["turn_observed"] = True

    def prime(self, mission_id, target=(-2.8, 2.5)):
        initial = position(self.samples[-1])
        require(self.submit(mission_id, [target]).accepted, "could not start fault mission")
        self.wait(lambda: math.dist(position(self.samples[-1]), initial) > 0.15, 35,
                  "fault was not injected during physical movement")
        require(self.query(mission_id).state == "RUNNING", "mission not running at injection")
        self.report.update(mission_id=mission_id, origin=initial, moving_world=position(self.samples[-1]))

    def cancel(self):
        trials = []
        self.report["trials"] = trials
        for index, target in enumerate([(-2.8,2.5),(2.8,-2.5),(-2.8,-2.5),(2.8,2.5)]):
            key = "cancel-moving" if index==0 else f"cancel-moving-{index+1}"
            self.prime(key,target)
            reply = self.call(self.cancel_client, CancelMission.Request(mission_id=key))
            require(reply.accepted, f"cancel rejected: {reply}")
            state = self.terminal(key,15)
            require(state.state=="CANCELED" and state.verified_count==0,f"bad cancel result: {state}")
            trials.append({"mission_id":key,"origin":self.report["origin"],"moving_world":self.report["moving_world"],
                           "state":state.state,"stop":self.stationary()})
        self.report.update(trials[0])

    def history(self):
        state = self.query("inspect-ab")
        require(state.found and state.state == "SUCCEEDED" and state.verified_count == 2 and
                len(state.evidence) == 2, "completed mission lost after core restart")
        old = self.submit("inspect-ab", TARGETS)
        require(old.accepted and old.state == "SUCCEEDED", "redelivery did not return stored success")
        require(self.query("cancel-moving").state == "CANCELED", "cancel state lost after restart")
        self.report.update(state=old.state, verified_count=state.verified_count, stop=self.stationary())

    def blocked_after_fault(self, mission_id):
        state = self.terminal(mission_id, 25)
        require(state.state == "RECOVERY_REQUIRED" and state.verified_count == 0, f"bad fault result: {state}")
        require(not self.submit("blocked-new", [(0, 0)]).accepted, "uncertain resource was reused")
        self.spin_for(2)
        self.report.update(state=state.state, detail=state.detail, stop=self.stationary())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["mission", "cancel", "history", "prime-crash",
                                         "crash", "prime-bridge", "bridge-loss"])
    parser.add_argument("--out", default="/evidence")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rclpy.init()
    node = Probe()
    ok = False
    try:
        node.ready()
        if args.phase == "prime-crash":
            node.prime("core-crash")
        elif args.phase == "prime-bridge":
            node.prime("control-bridge-loss")
        elif args.phase == "crash":
            node.blocked_after_fault("core-crash")
        elif args.phase == "bridge-loss":
            node.blocked_after_fault("control-bridge-loss")
        else:
            getattr(node, args.phase)()
        ok = True
    except Exception as error:
        node.report["error"] = f"{type(error).__name__}: {error}"
    finally:
        node.report.update(passed=ok, phase=args.phase, utc=datetime.now(timezone.utc).isoformat(),
                           witness_samples=len(node.samples))
        # Expose a compact summary plus the actual independent measurements for audit.
        (out / f"{args.phase}.json").write_text(json.dumps(node.report, indent=2), encoding="utf-8")
        (out / f"{args.phase}.trajectory.json").write_text(json.dumps(node.samples), encoding="utf-8")
        print(json.dumps(node.report, ensure_ascii=False), flush=True)
        node.destroy_node()
        rclpy.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
