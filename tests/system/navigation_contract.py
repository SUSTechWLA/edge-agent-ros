"""Exercise the production core against a NavigateTo fixture in a network-isolated Linux container."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time

from ament_index_python.packages import get_package_prefix
from edge_interfaces.action import NavigateTo
from edge_interfaces.srv import CancelMission, GetMission, SubmitMission
from geometry_msgs.msg import Point
from nav_msgs.msg import Odometry
import rclpy
from rclpy.action import ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.task import Future
from std_msgs.msg import String


def check(condition, message):
    if not condition:
        raise AssertionError(message)


MAX_GAP_NS = 250_000_000
STOP_WINDOW_NS = 300_000_000


def stop_witness(anchor_stamp, start_ns, samples, decision_ns):
    """Use the latest uninterrupted run, on two clocks in this synthetic fixture."""
    last_stamp, last_receipt = anchor_stamp, start_ns
    quiet = []
    for row in samples:
        stamp, receipt = row["stamp_ns"], row["received_ns"]
        if stamp - last_stamp > MAX_GAP_NS or receipt - last_receipt > MAX_GAP_NS:
            quiet.clear()
        if (stamp > last_stamp and receipt >= last_receipt and stamp >= start_ns
                and 0 <= receipt - stamp <= MAX_GAP_NS and row["frame"] == "odom"
                and row["speed"] == 0. and row["yaw_rate"] == 0.):
            quiet.append(row)
        else:
            quiet.clear()
        last_stamp, last_receipt = max(stamp, last_stamp), receipt
    fresh = bool(quiet) and 0 <= decision_ns - quiet[-1]["received_ns"] <= MAX_GAP_NS
    source_span = quiet[-1]["stamp_ns"] - quiet[0]["stamp_ns"] if quiet else 0
    receipt_span = quiet[-1]["received_ns"] - quiet[0]["received_ns"] if quiet else 0
    return {"confirmed": fresh and len(quiet) >= 3 and min(source_span, receipt_span) >= STOP_WINDOW_NS,
            "anchor_stamp_ns": anchor_stamp, "start_ns": start_ns, "decision_ns": decision_ns,
            "continuous_samples": len(quiet), "source_span_ns": source_span, "receipt_span_ns": receipt_span}


class Contract(Node):
    def __init__(self, binary, directory, report):
        super().__init__("navigation_contract")
        self.binary, self.db, self.report = binary, directory/"edge.db", report
        self.proc = None
        self.goals, self.results, self.cancel_times = {}, {}, {}
        self.action_receipts = {}
        self.pulses, self.feedback, self.odometry = [], [], []
        self.action = ActionServer(self, NavigateTo, "/edge/navigate_to", self.execute,
                                   handle_accepted_callback=self.accepted, cancel_callback=self.cancel_goal,
                                   callback_group=ReentrantCallbackGroup())
        self.submit = self.create_client(SubmitMission, "/edge/submit_mission")
        self.query = self.create_client(GetMission, "/edge/get_mission")
        self.cancel = self.create_client(CancelMission, "/edge/cancel_mission")
        self.odom = self.create_publisher(Odometry, "/odom", qos_profile_sensor_data)
        self.create_subscription(Odometry, "/odom", self.observed, qos_profile_sensor_data)
        self.create_subscription(String, "/edge/active_operation",
                                 lambda msg:self.pulses.append((time.monotonic_ns(),msg.data)), 10)
        self.create_subscription(NavigateTo.Impl.FeedbackMessage, "/edge/navigate_to/_action/feedback",
                                 lambda msg:self.feedback.append((bytes(msg.goal_id.uuid).hex(),
                                                                  msg.feedback.distance_remaining)), 10)

    def accepted(self, handle):
        req = handle.request
        self.report["goals"].append({"operation_id":req.operation_id, "uuid":bytes(handle.goal_id.uuid).hex(),
                                     "x":req.x, "y":req.y, "accepted_ns":time.monotonic_ns()})
        check(req.operation_id not in self.goals, "action received duplicate operation "+req.operation_id)
        self.goals[req.operation_id] = handle
        self.results[req.operation_id] = Future()
        handle.execute()

    async def execute(self, handle):
        state, result = await self.results[handle.request.operation_id]
        if state == "succeeded":
            handle.succeed()
        else:
            check(handle.is_cancel_requested, "fixture finished cancellation without a ROS cancel request")
            handle.canceled()
        self.action_receipts[handle.request.operation_id] = {
            "status": state, "reached": result.reached, "final_x": result.final_x,
            "final_y": result.final_y, "detail": result.detail, "returned_ns": time.monotonic_ns(),
        }
        return result

    def cancel_goal(self, handle):
        self.cancel_times[handle.request.operation_id] = time.monotonic_ns()
        return CancelResponse.ACCEPT

    def observed(self, msg):
        t = msg.header.stamp
        self.odometry.append({"stamp_ns":t.sec*10**9+t.nanosec, "received_ns":time.monotonic_ns(),
                              "x":msg.pose.pose.position.x, "y":msg.pose.pose.position.y,
                              "speed":math.hypot(msg.twist.twist.linear.x, msg.twist.twist.linear.y),
                              "yaw_rate":msg.twist.twist.angular.z, "frame":msg.header.frame_id})

    def wait(self, predicate, reason, timeout=6):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"core exited early with {self.proc.returncode}")
            value = predicate()
            if value:
                return value
            rclpy.spin_once(self, timeout_sec=.02)
        raise TimeoutError(reason)

    def call(self, client, request):
        fut = client.call_async(request)
        self.wait(fut.done, client.srv_name)
        return fut.result()

    def start_core(self):
        self.proc = subprocess.Popen([str(self.binary),"--ros-args","-p","db_path:="+str(self.db)])
        self.wait(lambda:all(c.service_is_ready() for c in (self.submit,self.query,self.cancel))
                  and self.odom.get_subscription_count() >= 2, "core services and odometry subscription", 15)

    def stop_core(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
            raise
        finally:
            self.proc = None

    def sample(self, x, y, stamp=None, *, speed=0.):
        stamp = time.monotonic_ns() if stamp is None else stamp
        msg = Odometry()
        msg.header.stamp.sec, msg.header.stamp.nanosec = divmod(stamp,10**9)
        msg.header.frame_id, msg.child_frame_id = "odom", "base_link"
        msg.pose.pose.orientation.w = 1.
        msg.pose.pose.position = Point(x=x,y=y)
        msg.twist.twist.linear.x = speed
        count = len(self.odometry)
        self.odom.publish(msg)
        self.wait(lambda:len(self.odometry)>count, "fixture odometry receipt")
        check(self.odometry[-1]["stamp_ns"]==stamp, "unexpected odometry source")
        return stamp

    def submit_to(self, mission, x=.35):
        return self.call(self.submit, SubmitMission.Request(mission_id=mission,waypoints=[Point(x=x,y=.1)]))

    def status(self, mission):
        reply = self.call(self.query, GetMission.Request(mission_id=mission))
        check(reply.found, "lost mission "+mission)
        return {"state":reply.state, "detail":reply.detail, "verified_count":reply.verified_count,
                "evidence":[[p.x,p.y] for p in reply.evidence]}

    def state(self, mission, expected, detail=None):
        def matches():
            row = self.status(mission)
            return row if row["state"]==expected and (not detail or detail in row["detail"]) else None
        return self.wait(matches, mission+" did not reach "+expected)

    def witness(self, case, *, stale, interrupted):
        anchor = self.odometry[-1]["stamp_ns"]
        start = time.monotonic_ns()
        first = len(self.odometry)
        # Stationary runs on either side of a motion sample cannot be added together.
        speeds = [0., 0., .08, 0., 0., 0., 0., 0.] if interrupted else [0.] * 5
        for index, speed in enumerate(speeds):
            if index:
                until = time.monotonic() + .1
                self.wait(lambda: time.monotonic() >= until, "sample spacing")
            self.sample(.35, .1, anchor if stale else None, speed=speed)
            if interrupted and index == 4:
                partial = stop_witness(anchor, start, self.odometry[first:], time.monotonic_ns())
                check(not partial["confirmed"] and partial["continuous_samples"] == 2,
                      "intervening motion incorrectly confirmed a stop")
                case["interrupted_window"] = {**partial, "samples_checked": 5}
        rows = self.odometry[first:]
        proof = stop_witness(anchor, start, rows, time.monotonic_ns())
        case["stop_witness"] = {**proof, "samples": rows}
        check(proof["confirmed"] == (not stale), "stop decision disagrees with injected samples")
        return proof["confirmed"]

    def run_case(self, name, *, cancel=False, stale_stop=False, stale_arrival=False):
        mission = "nav-contract-"+name
        op = mission+"/waypoint/1"
        case = self.report["cases"][name] = {"mission_id":mission,"operation_id":op}
        self.sample(0.,0.)
        initial = self.submit_to(mission)
        check(initial.accepted and initial.state=="RUNNING", "valid mission admission")
        case["accepted"] = initial.state
        # DDS discovery can outlast the core's 500 ms odometry freshness budget.
        # Keep the sensor live up to dispatch, then drive it explicitly for each witness.
        next_sample = 0.
        def discover_goal():
            nonlocal next_sample
            if op not in self.goals and time.monotonic() >= next_sample:
                self.sample(0., 0.)
                next_sample = time.monotonic() + .1
            return self.goals.get(op)
        goal = self.wait(discover_goal, "missing original goal", 15)
        check((goal.request.x,goal.request.y)==(.35,.1), "action target changed")
        duplicate = self.submit_to(mission)
        conflict = self.submit_to(mission,.6)
        check(duplicate.accepted and duplicate.state=="RUNNING" and duplicate.detail.startswith("duplicate: ")
              and not conflict.accepted and "core.id_conflict" in conflict.detail, "ID binding during dispatch")
        goal_id = bytes(goal.goal_id.uuid).hex()
        for distance in (.3,0.):
            goal.publish_feedback(NavigateTo.Feedback(distance_remaining=distance))
            self.wait(lambda:(goal_id,distance) in self.feedback, "feedback not observed on the action wire")
        self.wait(lambda:sum(value==op for _,value in self.pulses)>=3, "missing repeated lease")
        case["feedback"] = [v for key,v in self.feedback if key==goal_id]
        check(case["feedback"]==[.3,0.], "feedback order or UUID changed")
        pulses = [t for t,value in self.pulses if value==op]
        case["active_heartbeat_count"] = len(pulses)
        case["active_heartbeat_ns"] = pulses
        case["heartbeat_span_s"] = round((pulses[-1]-pulses[0])/1e9, 4)
        check(self.status(mission)["verified_count"]==0, "feedback granted completion")
        if cancel:
            self.sample(.34, .1, speed=.08)
            case["moving_before_cancel"] = self.odometry[-1]
            check(case["moving_before_cancel"]["speed"] > 0., "pre-cancel synthetic motion not observed")
            start = len(self.pulses)
            requested = time.monotonic_ns()
            reply = self.call(self.cancel, CancelMission.Request(mission_id=mission))
            check(reply.accepted and reply.state=="CANCELING", "cancel acceptance")
            self.wait(lambda:op in self.cancel_times and goal.is_cancel_requested, "cancel did not reach original goal")
            self.wait(lambda:any(not value for _,value in self.pulses[start:]), "lease withdrawal")
            withdrawn = next(t for t, value in self.pulses[start:] if not value)
            check(case["moving_before_cancel"]["received_ns"] < requested <= min(withdrawn, self.cancel_times[op]),
                  "cancel or lease withdrawal predates the request")
            case["cancel"] = {"operation_id": op, "uuid": goal_id, "requested_ns": requested,
                              "action_cancel_received_ns": self.cancel_times[op],
                              "lease_withdrawn_ns": withdrawn, "state": reply.state}
        confirmed = self.witness(case, stale=stale_stop, interrupted=cancel and not stale_stop)
        if cancel:
            pending = self.status(mission)
            check(pending["state"] == "CANCELING" and not pending["evidence"],
                  "core closed the cancellation before receiving the adapter's stop decision")
            case["before_cancel_result"] = pending
        result = NavigateTo.Result(reached=confirmed and not cancel, final_x=.35, final_y=.1,
                                   detail="fixture: stationary samples" if confirmed else "UNKNOWN: no fresh stop samples")
        self.results[op].set_result(("canceled" if cancel else "succeeded",result))
        if cancel:
            terminal = self.state(mission, "CANCELED" if confirmed else "RECOVERY_REQUIRED")
            check(not terminal["evidence"] and op in terminal["detail"], "cancel evidence or operation mismatch")
        else:
            self.state(mission, "RUNNING", "verifying: "+op)
            verifying_ns = time.monotonic_ns()
            stamp = self.sample(.37, .1, self.odometry[-1]["stamp_ns"] if stale_arrival else None)
            case["core_arrival_check"] = {"verifying_observed_ns": verifying_ns, "sample": self.odometry[-1]}
            if stale_arrival:
                terminal = self.state(mission,"FAILED","core.verification_timeout")
                check(not terminal["evidence"], "stale odometry accepted")
            else:
                terminal = self.state(mission,"SUCCEEDED")
                check(terminal["evidence"]==[[.37,.1]] and terminal["verified_count"]==1,
                      "core did not verify the post-result independent sample")
                case["arrival_stamp_ns"] = stamp
        terminal_ns = time.monotonic_ns()
        case["terminal"], case["terminal_observed_ns"] = terminal, terminal_ns
        receipt = case["fixture_action_result"] = self.action_receipts[op]
        proof = case["stop_witness"]
        check(proof["decision_ns"] <= receipt["returned_ns"] <= terminal_ns,
              "terminal receipt predates stop decision")
        if cancel:
            check(max(case["cancel"]["action_cancel_received_ns"], withdrawn) <= proof["start_ns"],
                  "stop sampling predated cancellation or lease withdrawal")
            reason = "stop confirmed:" if confirmed else "UNKNOWN:"
            check(reason in terminal["detail"], "core did not distinguish confirmed and uncertain stop")
            check(all(not value for t, value in self.pulses if withdrawn < t <= terminal_ns),
                  "operation heartbeat resumed after withdrawal")
        else:
            check(receipt["returned_ns"] <= verifying_ns <= case["core_arrival_check"]["sample"]["received_ns"],
                  "arrival sample predates the core verification phase")
        if stale_stop:
            occupied = self.submit_to("nav-contract-occupied")
            check(not occupied.accepted and "core.base_occupied" in occupied.detail, "uncertain stop released base")
            case["next_admission"] = occupied.detail

    def reconcile(self):
        first_pid = self.proc.pid
        self.stop_core()
        self.start_core()
        self.report["core_restart"] = {"original_pid": first_pid, "restarted_pid": self.proc.pid,
                                       "same_private_db": str(self.db)}
        for case in self.report["cases"].values():
            mission = case["mission_id"]
            row = self.status(mission)
            check(row==case["terminal"], "mission changed across core restart")
            duplicate = self.submit_to(mission)
            check(duplicate.accepted and duplicate.state==row["state"] and duplicate.detail.startswith("duplicate: "),
                  "duplicate did not reconcile original mission")
            with closing(sqlite3.connect(self.db.as_uri()+"?mode=ro",uri=True)) as db:
                op, result, stamp = db.execute("SELECT operation_id,action_result,odom_stamp_ns FROM steps WHERE mission_id=?",
                                               (mission,)).fetchone()
                events = db.execute("SELECT kind,operation_id FROM mission_events WHERE mission_id=? ORDER BY seq",
                                    (mission,)).fetchall()
            check(op==case["operation_id"] and events.count(("step.dispatch",op))==1
                  and result==case["fixture_action_result"]["detail"],
                  "original durable action identity or result lost")
            check(events.count(("step.verified",op))==row["verified_count"] and stamp==case.get("arrival_stamp_ns"),
                  "durable verifier evidence differs from public mission")
            case["reconciliation"] = {"state":duplicate.state,"operation_id":op,"dispatch_count":1,
                                      "action_result":result,"odom_stamp_ns":stamp, "events":[k for k,_ in events]}
        occupied = self.submit_to("nav-contract-after-restart")
        check(not occupied.accepted and "core.base_occupied" in occupied.detail,
              "restart released the unresolved operation")
        self.report["core_restart"]["next_admission"] = occupied.detail
        # Give a faulty redispatch a discoverable server and fresh odometry across several core ticks.
        for _ in range(15):
            self.sample(0., 0.)
            until = time.monotonic() + .1
            self.wait(lambda: time.monotonic() >= until, "post-restart observation")
        calls = Counter(row["operation_id"] for row in self.report["goals"])
        check(calls=={case["operation_id"]:1 for case in self.report["cases"].values()}, "action re-dispatched")
        self.report["action_goals_per_operation"] = dict(calls)


def run(report):
    domain = os.getenv("ROS_DOMAIN_ID","")
    check(domain.isdigit() and 1<=int(domain)<=232 and {p.name for p in Path("/sys/class/net").iterdir()}=={"lo"},
          "run in a Linux container with --network none and a dedicated nonzero ROS_DOMAIN_ID")
    os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"
    binary = Path(get_package_prefix("edge_core"))/"lib/edge_core/edge_core_node"
    report.update(domain=int(domain), core=str(binary), core_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
    with tempfile.TemporaryDirectory() as workspace:
        os.environ.setdefault("ROS_LOG_DIR",str(Path(workspace)/"ros"))
        rclpy.init()
        fixture = Contract(binary,Path(workspace),report)
        try:
            fixture.start_core()
            fixture.run_case("arrival")
            fixture.run_case("stale-arrival",stale_arrival=True)
            fixture.run_case("cancel",cancel=True)
            fixture.run_case("unknown-stop",cancel=True,stale_stop=True)
            fixture.reconcile()
        finally:
            fixture.stop_core()
            fixture.action.destroy()
            fixture.destroy_node()
            rclpy.try_shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,required=True)
    report_path = parser.parse_args().out
    report = {"passed": False, "scope": "production edge_core, ROS NavigateTo fixture and synthetic odometry",
              "clock": "Linux monotonic nanoseconds for fixture events and synthetic odometry stamps",
              "roles": {"adapter_fixture": "Evaluates continuous fresh stationary odometry and returns stop or UNKNOWN.",
                        "core": "Owns dispatch, active heartbeat, cancel, result routing and post-result position verification.",
                        "feedback_observer": "Independently subscribes to the action feedback topic and checks the goal UUID."},
              "stop_window": {"minimum_ns": STOP_WINDOW_NS, "maximum_gap_ns": MAX_GAP_NS, "frame": "odom"},
              "goals": [], "cases": {}}
    try:
        run(report)
        report["passed"] = True
    except Exception as exc:
        report["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report_path.parent.mkdir(parents=True,exist_ok=True)
        report_path.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
