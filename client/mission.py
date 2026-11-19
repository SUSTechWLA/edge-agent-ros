"""ROS 2 CLI for submitting, inspecting and canceling Edge missions.

Exit codes: 0 success/accepted, 2 invalid/rejected/not found, 3 failed or
unexpected terminal state, 4 discovery/request/mission wait timeout.
A local timeout does not cancel a submitted mission. Query status with its ID.
"""
import argparse
import json
import math
import sys
import time

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Point
from edge_interfaces.srv import CancelMission, GetMission, SubmitMission


TERMINAL = {"SUCCEEDED", "CANCELED", "FAILED", "RECOVERY_REQUIRED"}


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "status", "cancel"):
        part = sub.add_parser(command)
        part.add_argument("mission_id")
        part.add_argument("--timeout", type=float, default=180,
                          help="overall timeout in seconds (default: 180)")
        if command != "status":
            part.add_argument("--wait", action="store_true", help="wait for terminal state")
        if command == "run":
            part.epilog = "Append one to eight x,y waypoints in metres. Example: run demo -0.8,0 0,1 --wait"
    # Negative-coordinate pairs (e.g. -0.8,0) resemble option names to argparse.
    # Collect all waypoint tokens in their original order, then validate each.
    args, points = parser.parse_known_args(argv)
    args.points = []
    if not args.mission_id.strip() or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("provide a nonempty mission ID and a positive finite timeout")
    if args.command == "run":
        if not 1 <= len(points) <= 8:
            parser.error("run requires one to eight x,y waypoints in metres")
        for text in points:
            try:
                x, y = map(float, text.split(","))
            except ValueError:
                parser.error(f"invalid waypoint: {text!r} (expected x,y)")
            if not all(math.isfinite(v) and abs(v) <= 4 for v in (x, y)):
                parser.error(f"waypoint out of bounds [-4,4]: {text!r}")
            args.points.append(Point(x=x, y=y))
    elif points:
        parser.error(f"unexpected arguments: {points}")
    return args


class MissionClient(Node):
    def __init__(self):
        super().__init__("edge_mission_cli")
        self.submit = self.create_client(SubmitMission, "/edge/submit_mission")
        self.query = self.create_client(GetMission, "/edge/get_mission")
        self.cancel = self.create_client(CancelMission, "/edge/cancel_mission")

    def call(self, client, request, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not client.wait_for_service(timeout_sec=min(remaining, 30.0)):
            raise TimeoutError(f"service discovery timeout: {client.srv_name}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("client deadline expired before request")
        future = client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=min(remaining, 10.0))
        if not future.done():
            client.remove_pending_request(future)
            raise TimeoutError(f"response timeout: {client.srv_name}; query status by mission_id")
        return future.result()

    def execute(self, args, report):
        deadline = time.monotonic() + args.timeout
        if args.command == "run":
            reply = self.call(self.submit, SubmitMission.Request(
                mission_id=args.mission_id, waypoints=args.points), deadline)
        elif args.command == "cancel":
            reply = self.call(self.cancel, CancelMission.Request(mission_id=args.mission_id), deadline)
        else:
            reply = None

        if reply is not None:
            report.update(accepted=reply.accepted, state=reply.state, request_detail=reply.detail)
            if not reply.accepted:
                return 2

        while True:
            status = self.call(self.query, GetMission.Request(mission_id=args.mission_id), deadline)
            report.update(found=status.found, state=status.state, detail=status.detail,
                          verified_count=status.verified_count,
                          evidence=[{"x": point.x, "y": point.y} for point in status.evidence])
            if not status.found:
                return 2
            if status.state in TERMINAL:
                if status.state in {"FAILED", "RECOVERY_REQUIRED"}:
                    return 3
                return 3 if args.command == "run" and status.state == "CANCELED" else 0
            if args.command == "status" or not args.wait:
                return 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("mission wait expired; query status by mission_id")
            time.sleep(min(remaining, 0.25))


def main(argv):
    args = arguments(argv)
    report = {"command": args.command, "mission_id": args.mission_id}
    rclpy.init(args=[])
    node = MissionClient()
    try:
        code = node.execute(args, report)
    except TimeoutError as exc:
        code = 4
        report["error"] = str(exc)
    except Exception as exc:
        code = 3
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    report["exit_code"] = code
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
