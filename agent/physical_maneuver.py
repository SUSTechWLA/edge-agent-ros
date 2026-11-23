"""DSH physical controller for a site-approved rover maneuver. Transport is owned by the separately deployed adapter."""
import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

from physical_controller import Controller, deployment_owner, serve
from physical_ledger import PhysicalLedger, canonical, digest, number, ref, require
from plugin_descriptor import GIT, SHA, compute_content_digest, validate_descriptor
from rover_maneuver import finite, pose, world_goal
from rover_site import load_profile

TOOL = "avoid_front_obstacle"
PLUGIN = "rover.local_maneuver"
SCRIPT = "/opt/edge/agent/rover_maneuver_ros.py"


def approved_site(site, platform_digest, domain, profile="sim.json"):
    cfg,profile_digest,approval_digest=load_profile(site,profile,platform_digest,domain)
    platform = (site/"platform.json").read_bytes()
    require("sha256:"+hashlib.sha256(platform).hexdigest() == platform_digest,
            "plugin.digest_mismatch", "local platform manifest differs from site approval")
    manifest = json.loads(platform)
    require(isinstance(manifest.get("config"), dict) and SHA.fullmatch(manifest["config"].get("digest","")),
            "plugin.permission_denied", "a platform image manifest is required")
    resource=cfg["resource_id"]
    require(cfg.get("schema_version") == 1
            and ref(cfg["report_source"]) and ref(cfg["report_calibration"]) and GIT.fullmatch(cfg["source_base_commit"])
            and type(domain) is int and 1 <= domain <= 232,
            "plugin.permission_denied", "expected an approved rover site and explicit ROS domain")
    limits = {"activation_range_m":4,"clearance_beyond_surface_m":3,"max_forward_m":4,
              "max_path_m":8,"timeout_s":100,"arrival_tolerance_m":.12}
    require(all(finite(cfg.get(k)) and 0 < cfg[k] <= maximum for k,maximum in limits.items())
            and cfg["activation_range_m"] < cfg["max_forward_m"] <= cfg["max_path_m"],
            "plugin.config_invalid", "invalid operator maneuver limits")
    ros=cfg["ros"]
    names=[ros.get(k) for k in ("odom","report","witness","submit","query","cancel")]
    if approval_digest:
        names.append(ros["stop_witness"])
    require(all(isinstance(v,str) and re.fullmatch(r"(/[A-Za-z_][A-Za-z_0-9]*)+",v) for v in names)
            and len(set(names))==len(names) and all(ref(ros.get(k)) for k in ("odom_frame","witness_frame","base_frame"))
            and ros["odom_frame"]!=ros["witness_frame"] and ros["base_frame"]=="base_link",
            "plugin.config_invalid","independent witness and odometry must have distinct approved topics and reference frames")
    interface = "rover/bounded_maneuver@1"
    empty = {"type":"object","properties":{},"additionalProperties":False}
    command = ["python3","-B",SCRIPT,"execute","--site","/site","--socket","/role/physical.sock"]
    if profile!="sim.json":
        command.extend(["--profile",profile])
    descriptor = {"schema_version":2,"id":PLUGIN,"version":"1.0.0","class":"physical","kind":"tool",
        "runtime":"process","effects":"actuation","entrypoint":command,"content_digest":None,
        "provenance":{"author":"edge-agent-ros","source_commit":cfg["source_base_commit"],"image_digest":platform_digest},
        "parameters":empty,"outcomes":{"type":"object","properties":{},"additionalProperties":True},"config_schema":empty,
        "provides":[{"type":"tool","name":TOOL,"schema_ref":interface}],"requires":[],
        "hot_reload":False,"drain_required":True,"device_lease":resource,"stop_protocol":"rover.stationary_window",
        "verifier":"rover.world_witness","operation_ledger":"rover.maneuver_operations","mounts":[],
        "network":{"ingress":[],"egress":["site.core","site.observations"]},
        "resources":{"cpu":1,"memory_mib":256,"max_inflight":1,"shutdown_ms":5000},
        "health":{"probe":"rover.ready","ttl_ms":6000,"timeout_ms":1000},
        "policy":{"install":["deployment"],"enable":["deployment"],"site_approval":True}}
    with tempfile.TemporaryDirectory() as directory:
        trust = Path(directory)
        record = {"id":PLUGIN,"exports":[TOOL],"artifact":"platform.json","kind":"image","entrypoint":command,
                  "class":"physical",**{k:descriptor["provenance"][k] for k in ("source_commit","image_digest")},
                  "mounts":descriptor["mounts"],"network":descriptor["network"]}
        (trust/"platform.json").write_bytes(platform)
        (trust/"arguments.json").write_text(json.dumps(empty))
        (trust/"catalog.json").write_text(json.dumps({"implementations":{platform_digest:[record]},
                                                     "schemas":{interface:"arguments.json"}}))
        descriptor["content_digest"] = compute_content_digest(descriptor,trust)
        validate_descriptor(descriptor,trust)
    pin = {"class":"physical","plugin_id":PLUGIN,"name":TOOL,"version":descriptor["version"],
           "resource_id":resource,"provider_id":"rover.core_adapter","verifier_id":descriptor["verifier"],
           "content_digest":descriptor["content_digest"],"image_digest":platform_digest,
           "binding":digest([descriptor["content_digest"],profile_digest,approval_digest,domain]),
           "sensor":{"ros_domain_id":domain,"site_digest":profile_digest,"approval_digest":approval_digest}}
    return {"pin":pin,"limits":cfg,"descriptor":descriptor,"lease_ms":120000,"min_quiet_ns":400_000_000,
            "health_ttl_ms":6000,"camera_argv":command,"image_config_digest":manifest["config"]["digest"]}


class ManeuverLedger(PhysicalLedger):
    claim_kind = "maneuver"

    def __init__(self,*args,**options):
        super().__init__(*args,**options)
        self.db.execute("CREATE TABLE IF NOT EXISTS maneuver_intents(op TEXT PRIMARY KEY, intent TEXT NOT NULL)")

    def intent(self, lease, facts):
        self.assert_active(lease)
        op = lease["operation_id"]
        with self.write():
            existing = self.db.execute("SELECT intent FROM maneuver_intents WHERE op=?",(op,)).fetchone()
            if existing:
                require(existing[0] == canonical(facts),"plugin.fence_rejected","original target changed")
                return facts
            cfg = self.device(lease["resource_id"])["deployment"]["limits"]
            require(pose(facts.get("odom_initial"),3) and number(facts.get("odom_stamp_ns"))
                    and number(facts.get("report_stamp_ns")) and ref(facts.get("report_ref")) and facts.get("swept_coverage") is True and
                    facts.get("report_source")==cfg["report_source"] and facts.get("report_calibration")==cfg["report_calibration"]
                    and facts.get("footprint_profile")==cfg["profile_id"] and
                    abs(facts["odom_stamp_ns"]-facts["report_stamp_ns"]) <= 250_000_000,
                    "tool.receipt_invalid","invalid initial pose or sensor witness")
            goal,origin,near = facts.get("odom_goal"),facts["odom_initial"],facts.get("front_min_m")
            if facts.get("disposition") == "clear_no_motion":
                require(goal is None and finite(near) and near > cfg["activation_range_m"],
                        "tool.receipt_invalid","clearance conclusion lacks a clear initial observation")
            else:
                require(facts.get("disposition") == "pass_front_surface" and finite(near) and 0 < near <= cfg["activation_range_m"]
                        and pose(goal,2) and all(abs(v) <= 4 for v in goal),
                        "plugin.permission_denied","goal or obstacle outside authorization")
                forward = min(cfg["max_forward_m"],near+cfg["clearance_beyond_surface_m"])
                expected = [origin[0]+forward*math.cos(origin[2]),origin[1]+forward*math.sin(origin[2])]
                require(math.dist(expected,goal) < 1e-6,"plugin.permission_denied","goal differs from bounded sensor intent")
            self.db.execute("INSERT INTO maneuver_intents VALUES(?,?)",(op,canonical(facts)))
            self.event(lease["resource_id"],"maneuver.intent",op,intent=facts)
        return facts

    def reject(self, lease, code):
        """The executor can conclusively reject only before it has frozen a dispatch intent."""
        with self.write():
            op = lease["operation_id"]
            self.assert_active(lease)
            require(ref(code) and code.startswith("navigation.") and not self.db.execute(
                "SELECT 1 FROM maneuver_intents WHERE op=?", (op,)).fetchone(),
                "plugin.fence_rejected","cannot reject a prepared maneuver")
            self.db.execute("UPDATE physical_ops SET state='FAILED',reason=? WHERE op=?", (code,op))
            self.event(lease["resource_id"], "maneuver.rejected", op, reason=code)
        return self.status(op)

    def candidate(self,lease,receipt):
        op = lease["operation_id"]
        with self.write():
            row = self.status(op)
            if row["candidate"] is not None:
                require(row["lease"] == lease and row["candidate"] == receipt,"plugin.fence_rejected","receipt changed")
                return row
            self.assert_active(lease)
            intent = self.db.execute("SELECT intent FROM maneuver_intents WHERE op=?",(op,)).fetchone()
            require(intent and receipt.get("intent") == json.loads(intent[0]) and number(receipt.get("terminal_stamp_ns"))
                    and receipt["terminal_stamp_ns"] >= receipt["intent"]["odom_stamp_ns"],
                    "tool.receipt_invalid","no matching frozen intent")
            moves = receipt["intent"]["odom_goal"] is not None
            admitted = moves and receipt.get("core_state") != "REJECTED"
            require(receipt.get("core_mission_id") == (op if admitted else None)
                    and receipt.get("core_operation") == (op+"/waypoint/1" if admitted else None)
                    and receipt.get("core_state") in (("SUCCEEDED","FAILED","CANCELED","BLOCKED","REJECTED") if moves else ("NOT_DISPATCHED",))
                    and (receipt["core_state"] != "SUCCEEDED" or receipt.get("verified_count") == 1),
                    "tool.receipt_invalid","Core result differs from authorized operation")
            self.db.execute("UPDATE physical_ops SET state='CANDIDATE',candidate=? WHERE op=?",(canonical(receipt),op))
            self.event(lease["resource_id"],"maneuver.candidate",op,core_state=receipt["core_state"])
        return self.status(op)

    @staticmethod
    def require_brake_proof(ros,proof,earliest):
        if "stop_witness" not in ros:
            return
        brake=proof.get("braking",{})
        require(isinstance(brake,dict) and brake.get("source")==ros["stop_witness"] and brake.get("frame_id")==ros["stop_frame"]
                and number(brake.get("first_ns")) and number(brake.get("last_ns")) and brake["first_ns"]>=earliest
                and brake["last_ns"]-brake["first_ns"]>=400_000_000 and number(brake.get("samples"),4)
                and number(proof.get("last_ns")) and 0<=proof["last_ns"]-brake["last_ns"]<=200_000_000
                and finite(brake.get("max_linear_mps")) and 0<=brake["max_linear_mps"]<=.025
                and finite(brake.get("max_angular_radps")) and 0<=brake["max_angular_radps"]<=.06,
                "plugin.verifier_invalid","commissioned independent brake sensor has not confirmed stationarity")

    def verify(self,proof,*,source):
        op=proof["operation_id"]
        with self.write():
            row=self.status(op)
            pin, candidate=row["pin"],row["candidate"]
            device=self.device(pin["resource_id"])
            require(candidate and source == pin["verifier_id"] and proof.get("resource_id")==pin["resource_id"]
                    and proof.get("epoch")==row["lease"]["epoch"]==device["epoch"]
                    and proof.get("boot_id")==self.boot and proof.get("world_frame")==device["deployment"]["limits"]["ros"]["witness_frame"]
                    and row["state"] in ("CANDIDATE","RECOVERY_REQUIRED","SUCCEEDED","FAILED"),
                    "plugin.verifier_invalid","unbound world measurement")
            if row["proof"]:
                require({k:v for k,v in row["proof"].items() if k not in ("world_goal","arrival_error_m")}==proof,
                        "plugin.fence_rejected","verification changed")
                return row
            cfg=device["deployment"]["limits"]
            self.require_brake_proof(cfg["ros"],proof,candidate["terminal_stamp_ns"])
            require(pose(proof.get("world_initial"),3) and pose(proof.get("world_final"),2)
                    and number(proof.get("first_ns")) and number(proof.get("last_ns"))
                    and proof["first_ns"] >= candidate["terminal_stamp_ns"]
                    and proof["last_ns"]-proof["first_ns"] >= device["deployment"]["min_quiet_ns"]
                    and number(proof.get("samples"),4) and 0 <= proof["max_linear_mps"] <= .025
                    and 0 <= proof["max_angular_radps"] <= .06 and finite(proof.get("observed_path_m"))
                    and 0 <= proof["observed_path_m"] and proof.get("source")==cfg["ros"]["witness"],
                    "plugin.verifier_invalid","independent stationary observation missing")
            expected=world_goal(candidate["intent"],proof["world_initial"])
            error=math.dist(expected,proof["world_final"])
            passed=(candidate["core_state"] in ("NOT_DISPATCHED","SUCCEEDED")
                    and error <= cfg["arrival_tolerance_m"] and proof["observed_path_m"]<=cfg["max_path_m"]
                    and (candidate["core_state"]!="NOT_DISPATCHED" or proof["observed_path_m"]<=.03))
            result={**proof,"world_goal":expected,"arrival_error_m":error}
            failure=re.search(r"\[code:([a-z][a-z0-9_.]+)\]",candidate.get("core_detail",""))
            reason=None if passed else (failure[1] if failure else "navigation.verification_failed")
            self.db.execute("UPDATE physical_ops SET state=?,proof=?,reason=? WHERE op=?",
                            ("SUCCEEDED" if passed else "FAILED",canonical(result),reason,op))
            self.event(pin["resource_id"],"maneuver.verified",op,passed=passed,world_error_m=error)
        return self.status(op)

    def confirm_stopped(self,resource,proof,*,source):
        with self.write():
            device=self.device(resource)
            pin=device["deployment"]["pin"]
            require(source==pin["verifier_id"] and device["verifier_instance"] and device["camera_instance"]
                    and device["verifier_instance"]!=device["camera_instance"],"plugin.verifier_invalid","wrong stop witness")
            if device["stop_proof"]==proof:
                return device
            request=self.db.execute("SELECT challenge,issued_ns FROM physical_stop_requests WHERE resource=? AND epoch=? AND boot=?",
                                    (resource,device["epoch"],self.boot)).fetchone()
            # A stopped rover could still have a queued Core goal. A handoff must
            # never release the physical resource while an outcome is unresolved.
            pending=self.db.execute("SELECT 1 FROM physical_ops WHERE resource=? AND settled=0 LIMIT 1",(resource,)).fetchone()
            if "stop_witness" in device["deployment"]["limits"]["ros"]:
                require(number(proof.get("first_ns")),"plugin.verifier_invalid","stationary interval has no start time")
                self.require_brake_proof(device["deployment"]["limits"]["ros"],proof,proof["first_ns"]-200_000_000)
            require(request and not pending and device["phase"]=="DRAINING" and
                    all(proof.get(k)==device[k] for k in ("resource_id","boot_id","epoch","camera_instance","verifier_instance"))
                    and proof.get("stop_id")==request[0] and proof.get("plugin_id")==pin["plugin_id"]
                    and proof.get("source")==device["deployment"]["limits"]["ros"]["witness"] and number(proof.get("samples"),4)
                    and number(proof.get("window_ns"),device["deployment"]["min_quiet_ns"])
                    and self.clock()-request[1]>=device["deployment"]["min_quiet_ns"]
                    and finite(proof.get("max_linear_mps")) and 0<=proof["max_linear_mps"]<=.025
                    and finite(proof.get("max_angular_radps")) and 0<=proof["max_angular_radps"]<=.06,
                    "plugin.stop_unconfirmed","a fresh independent stationary window is required")
            self.db.execute("UPDATE physical_device SET phase='QUIESCENT',stop_proof=? WHERE resource=?",(canonical(proof),resource))
            self.event(resource,"stop.confirmed",stop_id=proof["stop_id"])
        return self.device(resource)


class ManeuverController(Controller):
    failure_code = "navigation.verification_failed"

    def catalog(self):
        view=super().catalog()
        for cap in view["tools"]:
            cap.update(effect="actuation",description="执行一次有界的本机前向障碍避让。小车依现场传感器检查前方通行空间，"
                       "有需要时在许可范围内绕过或等待后停稳。适用于用户明确要求车体执行避障，当前方已清空时保持静止。")
        return view

    def command(self,role,data):
        if role=="camera" and data["action"] in ("prepare","check") and self.pin["sensor"]["approval_digest"]:
            require(time.monotonic()-self.heartbeat.get("verifier",-float("inf"))<self.site["health_ttl_ms"]/1000,
                    "plugin.verifier_invalid","commissioned independent witness unavailable")
        if role=="camera" and data["action"] in ("prepare","reject"):
            require(data.get("instance_id")==self.ledger.device(self.resource)["camera_instance"],
                    "plugin.fence_rejected","unknown executor")
            if data["action"]=="prepare":
                return self.ledger.intent(data["lease"],data["intent"])
            return self.ledger.reject(data["lease"],data["code"])
        result=super().command(role,data)
        if role=="agent" and data["action"]=="call":
            if result.get("state")=="SUCCEEDED":
                result["evidence"].update(result["evidence"]["intent"])
            elif result.get("state")=="FAILED":
                row=self.ledger.status(result["operation_id"])
                receipt=row["candidate"] or {}
                result["error"].update(message=receipt.get("core_detail") or result["error"]["code"],
                                       details={"core_mission_id":receipt.get("core_mission_id"),
                                                "core_operation":receipt.get("core_operation"),
                                                "core_state":receipt.get("core_state"),
                                                "intent":receipt.get("intent"),"verification":row["proof"]})
        return result


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("action",choices=("serve","status","drain","release","process-stopped"))
    cli.add_argument("--site",type=Path,default=Path("/site"))
    cli.add_argument("--profile",default="sim.json")
    cli.add_argument("--state",type=Path,default=Path("/ledger"))
    cli.add_argument("--sockets",type=Path,default=Path("/sockets"))
    cli.add_argument("--inspection-file",type=Path)
    args=cli.parse_args()
    site=approved_site(args.site,os.environ["EDGE_PHYSICAL_PLATFORM_DIGEST"],int(os.environ["ROS_DOMAIN_ID"]),args.profile)
    resource=site["pin"]["resource_id"]
    args.state.mkdir(parents=True,exist_ok=True)
    ledger=ManeuverLedger(args.state,Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    try:
        if args.action=="serve":
            with deployment_owner(args.state):
                prior=ledger.db.execute("SELECT phase FROM physical_device WHERE resource=?",(resource,)).fetchone()
                if prior is None or prior[0]=="STOPPED":
                    ledger.install(site)
                else:
                    require(ledger.device(resource)["deployment"]==site,"plugin.physical_hot_reload_forbidden",
                            "complete independent handoff before installing a different site")
            asyncio.run(serve(ManeuverController(ledger,site),args.sockets,args.state,args.site))
            return
        require(ledger.device(resource)["deployment"]==site,"tool.binding_unavailable","incorrect deployment for this resource")
        if args.action=="status":
            print(json.dumps(ledger.device(resource)))
        elif args.action=="drain":
            print(json.dumps(ledger.drain(resource)))
        elif args.action=="release":
            print(json.dumps(ledger.release(resource)))
        else:
            require(args.inspection_file,"plugin.handoff_unconfirmed","provide the trusted deployment container exit record")
            record=json.loads(args.inspection_file.read_bytes())
            if isinstance(record,list) and len(record)==1:
                record=record[0]
            print(json.dumps(ledger.process_stopped(resource,record)))
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
