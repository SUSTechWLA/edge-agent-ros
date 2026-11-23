"""DSH rover admission, frozen native mission, independent odometry verification and release."""
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"agent"))
from physical_ledger import PhysicalError
from physical_maneuver import approved_site, ManeuverController, ManeuverLedger, TOOL
from kernel.authorization import motion_grant, task_capabilities
from kernel.ledger import Journal
from edge_contracts.errors import Refused
from rover_maneuver import relative_intent
from rover_site import sha

RESOURCE="sim.lingao.base"


class ManeuverContract(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path=Path(folder.name)
        self.cfg=json.loads((ROOT/"capabilities/rover-maneuver/sim.json").read_bytes())
        (self.path/"sim.json").write_text(json.dumps(self.cfg))
        raw=json.dumps({"schemaVersion":2,"config":{"digest":"sha256:"+"c"*64},"layers":[]}).encode()
        (self.path/"platform.json").write_bytes(raw)
        self.platform="sha256:"+hashlib.sha256(raw).hexdigest()
        self.site=approved_site(self.path,self.platform,142)
        self.now=2_000_000_000
        self.ledger=ManeuverLedger(self.path,"boot-test",lambda:self.now)
        self.addCleanup(self.ledger.close)
        self.ledger.install(self.site)
        self.controller=ManeuverController(self.ledger,self.site)
        self.report={"schema_version":1,"valid":True,"source_id":"rover.scan","frame_id":"base_link","clock_id":"ros",
                     "ttc_footprint_ref":self.cfg["profile_id"],"calibration_ref":self.cfg["report_calibration"],
                     "report_ref":"sample/front/15","stamp_ns":1_000_000_000,"front_min_m":1.45,"swept_coverage":True}
        self.poll("camera")
        self.poll("verifier")

    def poll(self,role):
        return self.controller.command(role,{"action":"next",**self.site["pin"]["sensor"],"instance_id":role})

    def intent(self,**fields):
        return relative_intent(self.cfg,[0.,0.,0.],1_000_000_000,{**self.report,**fields})

    def begin(self,task="test"):
        pin=self.site["pin"]
        step={"tool":TOOL,"version":pin["version"],"binding":pin["binding"],"args":{},"physical_pin":pin}
        _,steps=self.controller.command("agent",dict(action="freeze_plan",task=task,steps=[step],provider="ollama/test"))
        pin=steps[0]["physical_pin"]
        self.call={"action":"call","pin":pin,"wire":{"version":pin["version"],"arguments":{},"operation_id":pin["reservation_id"]}}
        self.assertEqual(self.controller.command("agent",self.call)["state"],"RUNNING")
        job=self.poll("camera")
        self.assertEqual(job["kind"],"maneuver")
        return job["lease"]

    def observe(self,lease,fact,state="SUCCEEDED"):
        self.controller.command("camera",dict(action="prepare",instance_id="camera",lease=lease,intent=fact))
        op=lease["operation_id"]
        dispatched=fact["odom_goal"] is not None
        measurement={"intent":fact,"core_mission_id":op if dispatched else None,"core_operation":op+"/waypoint/1" if dispatched else None,
                     "core_state":state,"verified_count":int(state=="SUCCEEDED"),"terminal_stamp_ns":2_000_000_000,
                     "core_detail":"done" if state!="FAILED" else "blocked [code:navigation.obstacle_blocked]"}
        self.controller.command("camera",dict(action="candidate",instance_id="camera",lease=lease,measurement=measurement))
        self.assertEqual(self.poll("verifier")["candidate"],measurement)
        x=fact["odom_goal"][0] if dispatched else 0.
        return dict(operation_id=op,epoch=lease["epoch"],boot_id=lease["boot_id"],resource_id=self.site["pin"]["resource_id"],
                    world_frame=self.cfg["ros"]["witness_frame"],world_initial=[6.,1.,math.pi/2],world_final=[6.,1.+x],source=self.cfg["ros"]["witness"],
                    observed_path_m=x,samples=14,max_linear_mps=0.,max_angular_radps=0.,
                    first_ns=2_000_000_000,last_ns=2_510_000_000)

    def verify(self,proof):
        return self.controller.command("verifier",dict(action="verify",instance_id="verifier",proof=proof))

    def test_rotated_world_goal_native_receipt_replay_and_handoff(self):
        lease=self.begin()
        fact=self.intent()
        proof=self.observe(lease,fact)
        with self.assertRaises(PhysicalError):
            self.verify({**proof,"source":"/odom"})
        self.assertEqual(self.verify(proof)["state"],"SUCCEEDED")
        self.assertEqual(self.verify(proof)["state"],"SUCCEEDED")
        receipt=self.controller.command("agent",self.call)["evidence"]
        self.assertEqual(receipt["odom_goal"],[3.25,0.])
        self.assertEqual(receipt["core_mission_id"],lease["operation_id"])
        self.assertAlmostEqual(receipt["verification"]["arrival_error_m"],0.)
        self.assertIsNone(self.poll("camera"))
        self.ledger.settle(self.call["pin"])
        self.ledger.drain(RESOURCE)
        challenge=self.poll("verifier")
        self.now+=600_000_000
        stopped=dict(**challenge,source="/ground_truth",samples=14,window_ns=510_000_000,max_angular_radps=0.,max_linear_mps=0.)
        self.assertEqual(self.controller.command("verifier",dict(action="stopped",instance_id="verifier",proof=stopped))["phase"],"QUIESCENT")
        self.assertEqual(self.ledger.release(RESOURCE)["phase"],"RELEASED")

    def test_changed_goal_refused_and_core_failure_preserved(self):
        lease=self.begin()
        fact=self.intent()
        self.ledger.intent(lease,fact)
        with self.assertRaises(PhysicalError):
            self.ledger.intent(lease,{**fact,"odom_goal":[2.,0.]})
        proof=self.observe(lease,fact,"FAILED")
        self.assertEqual(self.verify(proof)["reason"],"navigation.obstacle_blocked")
        error=self.controller.command("agent",self.call)["error"]
        self.assertEqual(error["details"]["core_state"],"FAILED")
        self.assertIn("navigation.obstacle_blocked",error["message"])

    def test_independent_world_miss_fails_native_success(self):
        proof=self.observe(self.begin(),self.intent())
        self.assertEqual(self.verify({**proof,"world_final":[6.,2.]})["state"],"FAILED")

    def test_report_clear_requires_actual_range_and_complete_coverage(self):
        lease=self.begin()
        for fields in (dict(front_min_m=None),dict(swept_coverage=False),dict(valid=False),
                       dict(calibration_ref="other"),dict(stamp_ns=1)):
            with self.subTest(fields=fields),self.assertRaises(ValueError):
                self.intent(**fields)
        fact=self.intent(front_min_m=4.)
        self.assertIsNone(fact["odom_goal"])
        self.assertEqual(self.verify(self.observe(lease,fact,"NOT_DISPATCHED"))["state"],"SUCCEEDED")
        self.assertIsNone(self.controller.command("agent",self.call)["evidence"]["core_mission_id"])

    def test_missing_sensor_reject_and_uncertain_dispatch_not_repeated(self):
        lease=self.begin()
        self.ledger.reject(lease,"navigation.observation_unavailable")
        self.assertEqual(self.controller.command("agent",self.call)["state"],"FAILED")
        self.ledger.settle(self.call["pin"])
        lease=self.begin("second")
        self.ledger.intent(lease,self.intent())
        with self.assertRaises(PhysicalError):
            self.ledger.reject(lease,"navigation.observation_unavailable")
        self.ledger.unknown(lease,"navigation.maneuver_outcome_unknown")
        self.assertIsNone(self.poll("camera"))
        with self.assertRaisesRegex(PhysicalError,"reconcile"):
            self.controller.command("agent",self.call)
        self.ledger.drain(RESOURCE)
        with self.assertRaises(PhysicalError):
            self.ledger.release(RESOURCE)

    def test_only_operator_can_grant_exact_resource_and_pin_task_scope(self):
        token=self.path/"token"
        token.write_text("temporary-operator-token")
        with patch.dict(os.environ,{"EDGE_MOTION_TOKEN_FILE":str(token)}):
            cap=self.controller.catalog()["tools"][0]
            caps={TOOL:cap,"read":{"effect":"read"},"legacy":{"effect":"idempotent"}}
            self.assertEqual(set(task_capabilities(caps,None)),{"read"})
            self.assertEqual(set(task_capabilities(caps,"other.base")),{"read"})
            for header in ("Bearer wrong",None,""):
                with self.assertRaises(PermissionError):
                    motion_grant(RESOURCE,header)
            grant=motion_grant(RESOURCE,"Bearer temporary-operator-token")
            self.assertEqual(set(task_capabilities(caps,grant)),{"read",TOOL})
            journal=Journal(self.path/"tasks")
            self.addCleanup(journal.owner.close)
            self.addCleanup(journal.db.close)
            journal.create("once","自然语言",motion_resource=grant)
            self.assertEqual(journal.get("once")["motion_resource"],RESOURCE)
            with self.assertRaises(Refused):
                journal.create("once","自然语言")

    def test_same_localization_cannot_witness_and_hardware_is_not_authorized(self):
        simulated=(self.path/"sim.json").read_bytes()
        (self.path/"bench.json").write_bytes(simulated)
        with patch.dict(os.environ,{"EDGE_ROVER_REQUIRE_BENCH":"1"}):
            for filename in ("sim.json","bench.json"):
                with self.assertRaises(PhysicalError):
                    approved_site(self.path,self.platform,142,filename)
        self.cfg["ros"]["witness"]=self.cfg["ros"]["odom"]
        (self.path/"sim.json").write_text(json.dumps(self.cfg))
        with self.assertRaises(PhysicalError):
            approved_site(self.path,self.platform,142)
        self.cfg["resource_id"]="hardware.rover"
        (self.path/"sim.json").write_text(json.dumps(self.cfg))
        with self.assertRaises(PhysicalError):
            approved_site(self.path,self.platform,142)

    def test_commissioned_bench_requires_independent_trust_and_live_braking(self):
        site=self.path/"installed"
        approval=self.path/"commissioning"
        site.mkdir()
        approval.mkdir()
        (site/"platform.json").write_bytes((self.path/"platform.json").read_bytes())
        self.cfg.update(deployment="commissioned_rover_bench",resource_id="hardware.synthetic.rover",
                        profile_id="synthetic.as_built.v2",report_calibration="synthetic_lidar_v2")
        ros=self.cfg["ros"]
        ros.update(witness="/mocap/rover",witness_frame="mocap",stop_witness="/bench/brake_speed",stop_frame="rear_axle")
        profile="bench.json"
        raw=json.dumps(self.cfg).encode()
        (site/profile).write_bytes(raw)
        records={}
        for kind,source in {"as_built_footprint":self.cfg["profile_id"],"sensor_calibration":self.cfg["report_calibration"],
                            "independent_pose":ros["witness"],"braking":ros["stop_witness"]}.items():
            filename=kind+".json"
            proof=json.dumps({"kind":kind,"resource_id":self.cfg["resource_id"],"reference":source,"passed":True,
                              "evidence_origin":"offline contract fixture"}).encode()
            (approval/filename).write_bytes(proof)
            records[kind]={"file":filename,"sha256":sha(proof)}
        decision=json.dumps({"schema_version":1,"decision":"approved_for_bench_motion","reviewer":"test.fixture",
                             "scope":{"resource_id":self.cfg["resource_id"],"ros_domain_id":142,
                                      "platform_digest":self.platform,"profile_digest":sha(raw)},"evidence":records}).encode()
        review=approval/"decision.json"
        review.write_bytes(decision)
        env={"EDGE_ROVER_BENCH_APPROVAL":str(review),"EDGE_ROVER_BENCH_APPROVAL_SHA256":sha(decision),"EDGE_ROVER_REQUIRE_BENCH":"1"}
        with patch.dict(os.environ,{"EDGE_ROVER_BENCH_APPROVAL_SHA256":""}),self.assertRaises(PhysicalError):
            approved_site(site,self.platform,142,profile)
        with patch.dict(os.environ,env):
            with self.assertRaises(PhysicalError):
                approved_site(site,self.platform,143,profile)
            self.site=approved_site(site,self.platform,142,profile)
            state=self.path/"bench-ledger"
            state.mkdir()
            self.ledger=ManeuverLedger(state,"bench-boot",lambda:self.now)
            self.addCleanup(self.ledger.close)
            self.ledger.install(self.site)
            self.controller=ManeuverController(self.ledger,self.site)
            self.poll("camera")
            self.poll("verifier")
            lease=self.begin()
            fact=self.intent(calibration_ref=self.cfg["report_calibration"],ttc_footprint_ref=self.cfg["profile_id"])
            end=self.observe(lease,fact)
            with self.assertRaises(PhysicalError):
                self.verify(end)
            brake={k:end[k] for k in ("first_ns","last_ns","samples","max_linear_mps","max_angular_radps")}
            brake.update(source=ros["stop_witness"],frame_id=ros["stop_frame"])
            with self.assertRaises(PhysicalError):
                self.verify({**end,"braking":{**brake,"source":ros["odom"]}})
            self.assertEqual(self.verify({**end,"braking":brake})["state"],"SUCCEEDED")
            (approval/"braking.json").write_text("{}")
            with self.assertRaises(PhysicalError):
                approved_site(site,self.platform,142,profile)


if __name__=="__main__":
    unittest.main()
