"""Deployment-owned DSH motion endpoint for the isolated fixed-target campus fixture."""
import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

from physical_controller import Controller, deployment_owner, serve
from physical_ledger import InvalidPhysicalArguments, PhysicalLedger, digest, require
from edge_contracts.descriptor import binding
from navigation.planar_pose import wrap_yaw


PROFILE = Path("/opt/edge/calibration/station/tools/plan_capture_views.json")
SENSORS = Path("/opt/edge/calibration/station/gazebo-fixed-rover.json")
CONTROL_FILE = Path(__file__).resolve().parents[1]/"scripts/lingao_control/lingao-chassis-control.json"
CONTROL = json.loads(CONTROL_FILE.read_bytes())
TOOL = "move_to_pose"
GUARDS = {"ROS_DOMAIN_ID":"197", "GZ_PARTITION":"edge_fixed_rover_197", "EDGE_MOTION_SITE":"fixed_calibration/lingao"}
FIELDS = ("x_m","y_m","yaw_rad")


def guard_site():
    if any(os.getenv(key) != value for key,value in GUARDS.items()):
        raise RuntimeError("deployment context mismatch")


def deploy_profile():
    files = [Path(__file__), Path(__file__).with_name("view_motion_actors.py"),
             Path(__file__).with_name("target_tracking.py"),
             Path(__file__).with_name("wheel_gyro_odometry.py"),
             Path("/opt/edge/agent/physical_ledger.py"), Path("/opt/edge/agent/physical_controller.py")]
    revision = digest({f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in files})
    chassis_sha = hashlib.sha256(CONTROL_FILE.read_bytes()).hexdigest()
    pin = {"class":"physical","plugin_id":"navigation.pose_motion","name":TOOL,"version":"1.4.5",
           "resource_id":"rover.base","provider_id":"navigation.pose_executor","verifier_id":"navigation.pose_witness",
           "content_digest":revision,"binding":digest([revision,GUARDS,chassis_sha,hashlib.sha256(SENSORS.read_bytes()).hexdigest()])}
    return {"pin":pin,"lease_ms":420000,"health_ttl_ms":6500,"min_quiet_ns":int(CONTROL["stationary_seconds"]*1e9),
            "chassis_profile_sha256":chassis_sha}


def resolve_goal(task, index, ref, owner, expected_binding, planner_sha, tracking_target=None, reverse_allowed=False,
                 *, require_spatial=False):
    require(owner.get("id") == task and owner.get("state") == "PLANNING" and len(owner.get("steps", [])) == index-1,
            "navigation.goal_unbound","expected current task and pending plan index")
    for i in range(index-2, -1, -1):
        step = owner["steps"][i]
        require(step.get("status") == "SUCCEEDED" and not step.get("physical_pin"),
                "navigation.goal_stale","a physical operation intervened after view planning")
        if step["tool"] != "plan_capture_views":
            continue
        e = step.get("evidence") or {}
        source = e.get("source", {})
        op = f"{task}/step/{i+1}"
        require(step.get("binding") == expected_binding and step.get("operation_id") == op
                and source.get("task_id") == task and source.get("operation_id") == op
                and e.get("outcome") == "candidate_views" and e.get("base_goal_available") is True
                and e.get("goal_frame") == "base_link" and source.get("planner_sha256") == planner_sha
                and re.fullmatch(r"[0-9a-f]{64}", str(source.get("archive_sha256")))
                and re.fullmatch(r"[1-9][0-9]{0,18}", str(source.get("stamp_ns"))),
                "navigation.goal_unbound","view observation lacks its original source and mount")
        matches = [row for row in e.get("candidates",[]) if row.get("candidate_ref") == ref]
        require(len(matches) == 1,"navigation.goal_unbound","view reference is absent from the latest plan")
        goal = matches[0].get("base_goal",{})
        require(goal.get("reference_frame") == "base_link" and goal.get("mount_reference")
                and all(type(goal.get(k)) in (float,int) and math.isfinite(goal[k]) for k in FIELDS)
                and .025 <= math.hypot(goal["x_m"],goal["y_m"]) <= 3.0 and abs(goal["yaw_rad"]) <= math.pi,
                "navigation.goal_outside_envelope","candidate exceeds this deployment envelope")
        resolved = {"goal_ref":ref,"translation_heading":[goal[k] for k in FIELDS],
                    "stamp_ns":int(source["stamp_ns"]),"source_operation":op,"archive_sha256":source["archive_sha256"]}
        if tracking_target:
            track = goal.get("tracking") or {}
            margin = track.get("min_margin_px")
            require(track.get("target") == tracking_target and type(margin) in (int,float)
                    and math.isfinite(margin) and margin > 0 and (goal["x_m"] > 0 or reverse_allowed and goal["x_m"] < 0),
                    "navigation.goal_unbound","current view lacks an authorized target-tracking plan")
            resolved["tracking_target"] = tracking_target
            resolved["reverse"] = goal["x_m"] < 0
        if require_spatial:
            epoch = source.get("origin_epoch")
            clear = matches[0].get("predicted_camera",{}).get("clearance_margin_m")
            require(e.get("reachability_checked") is True and re.fullmatch(r"[a-f0-9]{32}:[1-9][0-9]*", str(epoch))
                    and type(clear) in (int,float) and math.isfinite(clear) and clear > 0,
                    "navigation.goal_unbound","view has no source-bound clearance and boot origin")
            resolved["origin_epoch"] = epoch
        return resolved
    raise InvalidPhysicalArguments("navigation.goal_unbound","no view planning receipt for this task")


def resolve_live(task, index, ref):
    client = build_opener(ProxyHandler({}))
    with client.open("http://agent:8090/agent/tasks?"+urlencode({"id":task,"limit":1}),timeout=3) as response:
        owner = json.loads(response.read(2_000_000))
    planner = Path("/opt/edge/calibration/view_planner.py")
    profile = json.loads(SENSORS.read_bytes())
    tracking = profile.get("view_tracking",{})
    return resolve_goal(task,index,ref,owner,binding(json.loads(PROFILE.read_bytes())),
                        hashlib.sha256(planner.read_bytes()).hexdigest(),
                        tracking.get("target"), reverse_limit(tracking) > 0,
                        require_spatial=bool(profile.get("capture_navigation")))


def reverse_limit(tracking):
    limit = tracking.get("reverse_limit_mps",0)
    if type(limit) not in (int,float) or not 0 <= limit <= CONTROL["command_limits"]["forward_mps"]:
        raise ValueError("invalid_reverse_limit")
    return limit


class ViewLedger(PhysicalLedger):
    claim_kind = "execute"
    # ViewController.scope() requires the binding each actor computes from its own source files.
    actors_bind_code = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args,**kwargs)
        self.db.execute("CREATE TABLE IF NOT EXISTS view_goals(op TEXT PRIMARY KEY,goal TEXT NOT NULL)")

    def check_arguments(self, device, arguments):
        if (not isinstance(arguments,dict) or set(arguments) != {"goal_ref"}
                or not re.fullmatch(r"view\.[0-9a-f]{20}", str(arguments["goal_ref"]))):
            raise InvalidPhysicalArguments("navigation.goal_unbound","select one original view reference")

    def _reserve_step(self, task, index, step):
        op = super()._reserve_step(task,index,step)
        goal = resolve_live(task,index,step["args"]["goal_ref"])
        self.db.execute("INSERT INTO view_goals VALUES(?,?)",(op,json.dumps(goal)))
        return op

    def goal(self, op):
        return json.loads(self.db.execute("SELECT goal FROM view_goals WHERE op=?", (op,)).fetchone()[0])

    def candidate(self, lease, measurement):
        op = lease["operation_id"]
        with self.write():
            self.assert_active(lease)
            expected = self.goal(op)
            require(measurement == {"goal_ref":expected["goal_ref"],"outcome":"arrived"},
                    "tool.receipt_invalid","expected original goal")
            evidence = dict(measurement, observe_after_ns=self.clock(), source_operation=expected["source_operation"])
            self.db.execute("UPDATE physical_ops SET state='CANDIDATE',candidate=? WHERE op=?", (json.dumps(evidence),op))
            self.event(lease["resource_id"],"execute.candidate",op)
        return self.status(op)

    def verify(self, proof, *, source):
        op = proof["operation_id"]
        with self.write():
            row = self.status(op)
            pin, lease = row["pin"],row["lease"]
            device = self._match(pin)
            if row["state"] in ("SUCCEEDED","FAILED"):
                derived = {"target_pose","position_error_m","heading_error_rad","measured_translation_m","chassis_profile_sha256"}
                original = {k:v for k,v in row["proof"].items() if k not in derived}
                require(proof == original, "plugin.verifier_invalid", "changed independent witness")
                return row
            require(row["state"] == "CANDIDATE" and source == pin["verifier_id"]
                    and device["camera_instance"] != device["verifier_instance"]
                    and device["camera_instance"] and device["verifier_instance"]
                    and proof["epoch"] == device["epoch"] == lease["epoch"] and proof["boot_id"] == lease["boot_id"]
                    and proof["goal_ref"] == self.goal(op)["goal_ref"] and proof["sample_count"] >= 3
                    and row["candidate"]["observe_after_ns"] < proof["first_ns"] < proof["last_ns"] <= self.clock()
                    and proof["last_ns"]-proof["first_ns"] >= device["deployment"]["min_quiet_ns"]
                    and self.clock()-proof["last_ns"] < 1000000000
                    and proof["frame_id"]=="world" and proof["axes"]==CONTROL["axes"],
                    "plugin.verifier_invalid","missing original operation and fresh stationary witness")
            start, end = proof["start_pose"], proof["stop_pose"]
            require(all(isinstance(p,list) and len(p)==3 and all(type(x) in (int,float) and math.isfinite(x) for x in p)
                        for p in (start,end)), "plugin.verifier_invalid","invalid pose witness")
            expected = absolute_goal(start, self.goal(op)["translation_heading"])
            passed = (math.dist(expected[:2],end[:2]) <= .10 and abs(wrap_yaw(expected[2]-end[2])) < .055
                      and math.dist(start[:2],end[:2]) >= .025 and 0 <= proof["max_linear_mps"] < .025
                      and 0 <= proof["max_angular_radps"] < .05)
            verified = {**proof,"target_pose":list(expected),"position_error_m":math.dist(expected[:2],end[:2]),
                        "heading_error_rad":abs(wrap_yaw(expected[2]-end[2])),
                        "measured_translation_m":math.dist(start[:2],end[:2]),
                        "chassis_profile_sha256":device["deployment"]["chassis_profile_sha256"]}
            self.db.execute("UPDATE physical_ops SET state=?,proof=? WHERE op=?",
                            ("SUCCEEDED" if passed else "FAILED",json.dumps(verified),op))
            self.event(pin["resource_id"],"execute.verified",op,arrived=passed)
        return self.status(op)


def absolute_goal(start, local):
    x,y,yaw = start
    dx,dy,turn = local
    return x+math.cos(yaw)*dx-math.sin(yaw)*dy, y+math.sin(yaw)*dx+math.cos(yaw)*dy, wrap_yaw(yaw+turn)


class ViewController(Controller):
    socket_roles = ("agent","executor","verifier")
    verification_error = "navigation.pose_verification_failed"

    def scope(self):
        return {"binding":self.pin["binding"]}

    def capability(self):
        return {"effect":"actuation","parameters":{"goal_ref":"string"},
                "description":"Drive the rover to one stamped, approved pose and final heading. Select goal_ref from the latest capture view options of this task. Stop and independently verify arrival."}

    def command(self, role, data):
        require(data["action"] != "freeze_plan","plugin.permission_denied","one feedback operation is required")
        result = super().command("camera" if role=="executor" else role, data)
        if role=="executor" and data["action"]=="check":
            self.heartbeat["camera"] = time.monotonic()
        if data["action"]=="next" and result and result["kind"] in ("execute","candidate"):
            result["goal"] = self.ledger.goal(result["operation_id"])
        return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("role",choices=("controller","executor","verifier","guard"))
    role=parser.parse_args().role
    guard_site()
    if role != "controller":
        from navigation.view_motion_actors import run
        return run(role)
    state=Path("/state")
    site=deploy_profile()
    ledger=ViewLedger(state, Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    try:
        with deployment_owner(state):
            ledger.install(site, owner_locked=True)
        asyncio.run(serve(ViewController(ledger,site),Path("/sockets"),state,state))
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
