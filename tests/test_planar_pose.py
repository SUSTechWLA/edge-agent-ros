"""Relative pose Tool interoperability and independent wheel-odometry replay."""
from http.server import ThreadingHTTPServer
import json
import math
from pathlib import Path
import random
import sys
from threading import Thread
import unittest
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from edge_contracts.invoke import candidate, request
from navigation.planar_pose import plan_pose
from navigation.pose_provider import Handler, invoke


def goal(x,y,yaw):
    return {"frame":"base_at_start", "x_m":x, "y_m":y, "yaw_rad":yaw}


def replay_encoders(segments, start=(0.0,0.0,0.0)):
    """A separate differential wheel model integrates left/right ticks into a world pose."""
    x,y,heading = start
    track = 0.54
    for segment in segments:
        if segment["kind"] == "pivot":
            right = track * segment["angle_rad"] / 2
            left = -right
        else:
            assert segment["kind"] == "drive_forward" and segment["distance_m"] > 0
            left = right = segment["distance_m"]
        delta = (right - left) / track
        travel = (right + left) / 2
        x += travel * math.cos(heading + delta / 2)
        y += travel * math.sin(heading + delta / 2)
        heading += delta
    return x,y,math.atan2(math.sin(heading), math.cos(heading))


class RelativePose(unittest.TestCase):
    def test_single_axis_yaw_and_noop(self):
        for pose, expected in (
            (goal(0,0,0), []),
            (goal(0,0,2*math.pi), []),
            (goal(0,0,3*math.pi), [{"kind":"pivot","angle_rad":math.pi}]),
            (goal(0,0,-math.pi/2), [{"kind":"pivot","angle_rad":-math.pi/2}]),
            (goal(2,0,0), [{"kind":"drive_forward","distance_m":2.0}]),
            (goal(-1,0,0), [{"kind":"pivot","angle_rad":math.pi},{"kind":"drive_forward","distance_m":1},
                             {"kind":"pivot","angle_rad":math.pi}]),
        ):
            with self.subTest(pose=pose):
                evidence = plan_pose(pose)
                self.assertEqual(evidence["segments"],expected)
                self.assertFalse(evidence["clearance_checked"])
                self.assertFalse(evidence["motion_authorized"])

    def test_body_frame_goals_end_at_expected_position_and_heading(self):
        rng = random.Random(25)
        for _ in range(60):
            x,y,yaw = (rng.uniform(-8,8) for _ in range(3))
            start = (4.2,-1.3,rng.uniform(-math.pi,math.pi))
            plan = plan_pose(goal(x,y,yaw))
            for s in plan["segments"]:
                if s["kind"] == "pivot":
                    self.assertLessEqual(abs(s["angle_rad"]),math.pi)
            X,Y,a = replay_encoders(plan["segments"],start)
            self.assertAlmostEqual(X,start[0]+math.cos(start[2])*x-math.sin(start[2])*y)
            self.assertAlmostEqual(Y,start[1]+math.sin(start[2])*x+math.cos(start[2])*y)
            self.assertAlmostEqual(math.remainder(a-start[2]-yaw,math.tau),0.0)

    def test_refuses_unresolved_frames_nonfinite_and_unknown_fields(self):
        for args in (dict(goal(0,0,0),frame="map"), dict(goal(0,0,0),speed=.5), goal(True,0,0),
                     goal(0,float("nan"),0), goal(0,0,float("inf")), goal(10**450,0,0)):
            with self.assertRaises(ValueError):
                plan_pose(args)
            wire = {"version":"1","operation_id":"test/step/1","arguments":args}
            self.assertEqual(invoke(wire)["state"],"BLOCKED")

    def test_dsh_tool_over_http(self):
        cap = json.loads((ROOT/"calibration/station/tools/plan_rover_pose.json").read_bytes())
        op = "pose-test/step/2"
        args = goal(0,.5,0)
        step = {"version":cap["version"],"args":args}
        wire = request(op,step,{"task_id":"pose-test","trace_id":"pose-test"})
        self.assertEqual(set(args),set(cap["parameters"]))
        with ThreadingHTTPServer(("127.0.0.1",0),Handler) as server:
            thread = Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                base = "http://127.0.0.1:" + str(server.server_port)
                with urlopen(base+"/health",timeout=2) as response:
                    self.assertEqual(json.load(response)["id"],cap["id"])
                req = Request(base+"/plan",data=json.dumps(wire).encode(),headers={"Content-Type":"application/json"})
                with urlopen(req,timeout=2) as response:
                    self.assertEqual(candidate(json.load(response),op,step,cap),plan_pose(args))
            finally:
                server.shutdown()
                thread.join(2)
        self.assertEqual(invoke(dict(wire,version="2"))["state"],"BLOCKED")


if __name__ == "__main__":
    unittest.main()
