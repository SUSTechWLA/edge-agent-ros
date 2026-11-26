"""SDD-045 P2. Actual deployment preflight + role endpoints and persistent controller transactions."""
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from threading import Barrier
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"agent"),str(ROOT/"scripts")]
from physical_controller import Controller, deployment_owner, load_deployment
from physical_ledger import PhysicalError, PhysicalLedger
from plugin_descriptor import DescriptorError, compute_content_digest
from prepare_physical_camera import prepare_site
from prepare_soft_pilots import sha
from v2_registry import RegistryError, command as publish_v2


class CameraLedgerContract(unittest.TestCase):
    camera, verifier = "a"*12, "b"*12

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.raw = json.dumps({"schemaVersion":2,"config":{"digest":"sha256:" + "0"*64},"layers":[]}).encode()
        self.image = sha(self.raw)
        self.site = self.root/"site"
        prepare_site(self.site,self.raw,"a"*40)
        self.deploy = load_deployment(self.site,self.image)
        self.resource = self.deploy["pin"]["resource_id"]
        self.now = 1_000_000_000
        self.ledger = self.open("boot1")
        self.ledger.install(self.deploy)
        self.host = Controller(self.ledger,self.deploy)
        self.poll("camera")
        self.poll("verifier")

    def open(self, boot):
        ledger = PhysicalLedger(self.root/"state",boot,lambda:self.now)
        self.addCleanup(ledger.close)
        return ledger

    def request(self, role, data):
        if role in ("camera","verifier"):
            data = {"instance_id":getattr(self,role), **data}
        return self.host.command(role,data)

    def poll(self, role):
        return self.request(role,{"action":"next","instance_id":getattr(self,role),**self.deploy["pin"]["sensor"]})

    def reserve(self,task="sample"):
        pin = self.deploy["pin"]
        step = {"tool":pin["name"],"version":pin["version"],"binding":pin["binding"],"args":{},"physical_pin":pin}
        _,items = self.request("agent",{"action":"freeze_plan","task":task,"steps":[step],"provider":"user"})
        return items[0]["physical_pin"]

    def run_op(self, pin):
        return self.request("agent",{"action":"call","pin":pin,"wire":{
            "operation_id":pin["reservation_id"],"version":pin["version"],"arguments":{}}})

    def candidate(self, pin):
        self.assertEqual(self.run_op(pin)["state"], "RUNNING")
        job = self.poll("camera")
        self.assertIsNone(self.poll("camera"))
        lease = job["lease"]
        op = pin["reservation_id"]
        self.assertTrue(self.request("camera",{"action":"check","lease":lease}))
        measurement = {"artifact_ref":"frames/" + hashlib.sha256(op.encode()).hexdigest()+f"-{lease['epoch']}.cdr",
                       "artifact_sha256":sha(b"recorded frame"),"source_id":pin["sensor"]["source_id"],"frame_id":"optical","stamp_ns":1234}
        self.request("camera",{"action":"candidate","lease":lease,"measurement":measurement})
        pending = self.poll("verifier")
        self.assertEqual(pending["candidate"],measurement)
        proof = {k:v for k,v in measurement.items() if k != "artifact_ref"}
        proof.update(operation_id=op,resource_id=self.resource,epoch=lease["epoch"],verdict="pass",evidence_ref="witness/one")
        return lease,proof

    def quiescence(self):
        job = self.poll("verifier")
        self.assertEqual(job["kind"],"stop")
        self.now += 500000000
        return {**{k:job[k] for k in ("resource_id","plugin_id","boot_id","epoch","stop_id","camera_instance","verifier_instance")},"verdict":"stopped",
                "source_alive":True,"observations":4,"window_ns":500000000,"subscription_count":0,"evidence_ref":"witness/stop"}

    def reject(self, code, callback, *args):
        with self.assertRaises(PhysicalError) as caught:
            callback(*args)
        self.assertEqual(caught.exception.code,code)

    def test_no_self_verification_and_no_malformed_or_incomplete_receipt(self):
        pin = self.reserve()
        lease,proof = self.candidate(pin)
        self.reject("plugin.permission_denied",self.request,"camera",{"action":"verify","proof":proof})
        self.reject("plugin.permission_denied",self.request,"agent",{"action":"release"})
        for change in ({"stamp_ns":0},{"epoch":True},{"artifact_sha256":sha(b"wrong")},{"frame_id":"other"}):
            self.reject("plugin.verifier_invalid",self.request,"verifier",{"action":"verify","proof":dict(proof,**change)})
        self.assertEqual(self.run_op(pin)["state"], "RUNNING")
        self.request("verifier",{"action":"verify","proof":proof})
        response = self.run_op(pin)
        self.assertEqual(response["state"],"SUCCEEDED")
        self.assertEqual(response["evidence"]["verification"],proof)
        self.assertEqual(self.run_op(pin),response)
        self.assertIsNone(self.poll("camera"))
        self.reject("plugin.fence_rejected",self.request,"camera",{"action":"check","lease":lease})

    def test_incremental_reservation_is_atomic_across_retrying_clients(self):
        pin = self.deploy["pin"]
        step = {"tool":pin["name"], "version":pin["version"], "binding":pin["binding"], "args":{}, "physical_pin":pin}
        request = {"action":"freeze_step","task":"frame","index":3,"provider":"test","step":step}
        other = Controller(self.open("boot1"), self.deploy)
        for role in ("camera", "verifier"):
            other.command(role, {"action":"next", "instance_id":getattr(self,role), **pin["sensor"]})
        start = Barrier(2)
        def admit(host):
            start.wait(timeout=5)
            return host.command("agent", request)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, retry = pool.map(admit, [self.host, other])
        self.assertEqual(first, retry)
        self.assertEqual(first[1]["physical_pin"]["reservation_id"],"frame/step/3")
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM physical_ops").fetchone(),(1,))
        kinds = [json.loads(event)["kind"] for event, in self.ledger.db.execute("SELECT event FROM module_bus_events")]
        self.assertEqual(kinds.count("plugin.physical.reserved"),1)

    def test_drain_fixed_undispatched_step_stop_release_exit_and_new_version(self):
        pin = self.reserve()
        self.ledger.drain(self.resource)
        self.assertFalse(self.host.catalog()["tools"])
        self.reject("plugin.stop_unconfirmed",self.ledger.release,self.resource)
        self.assertIsNone(self.poll("verifier"))
        lease,proof = self.candidate(pin)
        self.request("verifier",{"action":"verify","proof":proof})
        self.assertIsNone(self.poll("verifier"))
        self.request("agent",{"action":"settle","pin":pin})
        quiet = self.quiescence()
        self.reject("plugin.stop_unconfirmed",self.request,"verifier",{"action":"stopped","proof":dict(quiet,window_ns=4)})
        self.reject("plugin.stop_unconfirmed",self.request,"verifier",{"action":"stopped","proof":dict(quiet,stop_id="stale")})
        self.reject("plugin.stop_unconfirmed",self.request,"verifier",{"action":"stopped","proof":dict(quiet,window_ns=900000000)})
        self.reject("plugin.stop_unconfirmed",self.request,"verifier",{"action":"stopped","proof":dict(quiet,camera_instance="other")})
        self.request("verifier",{"action":"stopped","proof":quiet})
        self.assertEqual(self.ledger.release(self.resource)["phase"], "RELEASED")
        newer_site = self.root/"v2"
        prepare_site(newer_site,self.raw,"b"*40,"2.0.0")
        newer = load_deployment(newer_site,self.image)
        self.reject("plugin.physical_hot_reload_forbidden",self.ledger.install,newer)
        self.reject("plugin.handoff_unconfirmed",self.ledger.process_stopped,self.resource,
                    {"Id":"f"*64,"State":{"Running":False,"Status":"exited"}})
        inspection = {"Id":"a"*64,"Image":self.deploy["image_config_digest"],"Config":{"Hostname":self.camera,
            "Entrypoint":["/usr/local/bin/edge-entrypoint",*self.deploy["camera_argv"]]},"State":{"Running":False,"Status":"exited"}}
        self.reject("plugin.handoff_unconfirmed",self.ledger.process_stopped,self.resource,dict(inspection,Image="wrong"))
        platform = {"digest":self.deploy["pin"]["image_digest"],"mediaType":"application/vnd.oci.image.manifest.v1+json"}
        for altered in (dict(platform,digest=sha(b"wrong image")),dict(platform,mediaType="application/vnd.oci.image.index.v1+json")):
            self.reject("plugin.handoff_unconfirmed",self.ledger.process_stopped,self.resource,
                        dict(inspection,ImageManifestDescriptor=altered))
        self.ledger.process_stopped(self.resource,dict(inspection,Image=sha(b"index"),ImageManifestDescriptor=platform))
        # Classic engines identify the exact pinned image config in .Image.
        self.ledger.process_stopped(self.resource,inspection)
        switched = self.ledger.install(newer)
        self.assertGreater(switched["epoch"],lease["epoch"])
        self.assertEqual(self.ledger.status(pin["reservation_id"])["proof"],proof)
        self.reject("tool.binding_unavailable",self.request,"agent",{"action":"catalog"})

    def test_crash_after_dispatch_never_reoffers_and_conflicting_device_lease_waits(self):
        first,second = self.reserve("a"),self.reserve("b")
        self.run_op(first)
        lease = self.poll("camera")["lease"]
        self.reject("plugin.lease_conflict",self.run_op,second)
        self.host = Controller(self.open("boot1"),self.deploy)
        self.assertIsNone(self.poll("camera"))
        self.assertEqual(self.run_op(first)["state"],"RUNNING")
        self.now = lease["valid_until_ns"] + 1
        self.reject("tool.outcome_unknown",self.run_op,first)
        self.assertEqual(self.ledger.status(first["reservation_id"])["pin"],first)
        self.assertIsNone(self.poll("camera"))
        self.reject("plugin.handoff_unconfirmed",self.request,"agent",{"action":"settle","pin":first})

    def test_unknown_on_kernel_reboot_and_no_stale_proof(self):
        pin = self.reserve()
        waiting = self.reserve("next")
        lease,proof = self.candidate(pin)
        self.host = Controller(self.open("boot2"),self.deploy)
        recovered = self.host.ledger.status(pin["reservation_id"])
        self.assertEqual(recovered["state"],"RECOVERY_REQUIRED")
        self.assertEqual(recovered["reason"],"plugin.boot_changed")
        self.reject("tool.outcome_unknown",self.run_op,waiting)
        self.assertIsNone(self.host.ledger.status(waiting["reservation_id"])["lease"])
        self.assertFalse(self.host.catalog()["tools"])
        self.reject("plugin.fence_rejected",self.request,"camera",{"action":"check","lease":lease})
        self.reject("plugin.verifier_invalid",self.request,"verifier",{"action":"verify","proof":proof})

    def test_reboot_readmits_only_the_identical_kernel_fenced_deployment(self):
        pin = self.reserve()
        self.candidate(pin)
        rebooted = self.open("boot2")
        fenced = rebooted.device(self.resource)
        self.assertEqual(fenced["phase"],"DRAINING")
        newer_site = self.root/"v2"
        prepare_site(newer_site,self.raw,"b"*40,"2.0.0")
        self.reject("plugin.physical_hot_reload_forbidden",rebooted.install,load_deployment(newer_site,self.image))
        # A second reboot before the controller returns keeps the kernel fence.
        rebooted = self.open("boot3")
        readmitted = rebooted.install(self.deploy)
        self.assertEqual((readmitted["phase"],readmitted["camera_instance"],readmitted["verifier_instance"]),("ACTIVE",None,None))
        self.assertGreater(readmitted["epoch"],fenced["epoch"])
        self.assertEqual(rebooted.install(self.deploy),readmitted)
        old = rebooted.status(pin["reservation_id"])
        self.assertEqual((old["state"],old["reason"]),("RECOVERY_REQUIRED","plugin.boot_changed"))
        self.host = Controller(rebooted,self.deploy)
        self.camera, self.verifier = "c"*12, "d"*12
        self.poll("camera")
        self.poll("verifier")
        self.assertTrue(self.host.catalog()["tools"])
        fresh = self.reserve("after-reboot")
        lease,proof = self.candidate(fresh)
        self.assertEqual(lease["epoch"],readmitted["epoch"])
        self.request("verifier",{"action":"verify","proof":proof})
        self.assertEqual(self.run_op(fresh)["state"],"SUCCEEDED")
        self.assertEqual(rebooted.status(pin["reservation_id"])["state"],"RECOVERY_REQUIRED")
        kinds = [json.loads(event)["kind"] for event, in rebooted.db.execute("SELECT event FROM module_bus_events")]
        self.assertEqual(kinds.count("plugin.physical.fence.readmitted"),1)

    def test_restarted_process_takes_only_a_silent_idle_slot(self):
        previous = self.camera
        self.camera = "e"*12
        self.reject("plugin.fence_rejected",self.poll,"camera")  # The attached process is still heard.
        pin = self.reserve()
        self.camera = previous
        self.assertEqual(self.run_op(pin)["state"],"RUNNING")
        job = self.poll("camera")
        self.host.heartbeat["camera"] -= self.deploy["health_ttl_ms"]/1000 + 1
        self.camera = "e"*12
        self.reject("plugin.fence_rejected",self.poll,"camera")  # Claimed work keeps its original owner.
        self.now = job["lease"]["valid_until_ns"] + 1
        self.assertIsNone(self.poll("camera"))
        self.assertEqual(self.ledger.status(pin["reservation_id"])["state"],"RECOVERY_REQUIRED")
        self.assertEqual(self.ledger.device(self.resource)["camera_instance"],"e"*12)
        self.reject("plugin.fence_rejected",self.request,"camera",{"action":"check","lease":job["lease"],"instance_id":previous})
        kinds = [json.loads(event)["kind"] for event, in self.ledger.db.execute("SELECT event FROM module_bus_events")]
        self.assertEqual(kinds.count("plugin.physical.camera.replaced"),1)
        self.host.heartbeat["verifier"] -= self.deploy["health_ttl_ms"]/1000 + 1
        self.verifier = "e"*12
        self.reject("plugin.verifier_invalid",self.poll,"verifier")

    def test_deployer_drain_survives_reboot(self):
        self.ledger.drain(self.resource)
        rebooted = self.open("boot2")
        self.reject("plugin.physical_hot_reload_forbidden",rebooted.install,self.deploy)
        self.assertEqual(rebooted.device(self.resource)["phase"],"DRAINING")

    def test_settled_device_accepts_new_approved_version_only_after_reboot(self):
        newer_site = self.root/"v2"
        prepare_site(newer_site,self.raw,"b"*40,"2.0.0")
        newer = load_deployment(newer_site,self.image)
        self.reject("plugin.physical_hot_reload_forbidden",self.ledger.install,newer)
        rebooted = self.open("boot2")
        installed = rebooted.install(newer)
        self.assertEqual((installed["phase"],installed["deployment"]),("ACTIVE",newer))
        last = json.loads(rebooted.db.execute("SELECT event FROM module_bus_events ORDER BY seq DESC LIMIT 1").fetchone()[0])
        self.assertEqual((last["kind"],last["data"]["handoff"],last["data"]["version"],last["data"]["previous_version"]),
                         ("plugin.physical.installed","boot_fence","2.0.0",self.deploy["pin"]["version"]))

    def test_idle_same_boot_upgrade_requires_code_bound_actors_lock_and_settled_work(self):
        newer_site = self.root/"v2"
        prepare_site(newer_site,self.raw,"b"*40,"2.0.0")
        newer = load_deployment(newer_site,self.image)
        self.ledger.actors_bind_code = True
        self.reject("plugin.physical_hot_reload_forbidden",self.ledger.install,newer)  # No deployment lock.
        pin = self.reserve()
        self.reject("plugin.physical_hot_reload_forbidden",lambda:self.ledger.install(newer,owner_locked=True))
        self.request("agent",{"action":"settle","pin":pin})
        self.ledger.actors_bind_code = False
        self.reject("plugin.physical_hot_reload_forbidden",lambda:self.ledger.install(newer,owner_locked=True))
        self.ledger.actors_bind_code = True
        installed = self.ledger.install(newer,owner_locked=True)
        self.assertEqual((installed["phase"],installed["camera_instance"],installed["deployment"]),("ACTIVE",None,newer))
        last = json.loads(self.ledger.db.execute("SELECT event FROM module_bus_events ORDER BY seq DESC LIMIT 1").fetchone()[0])
        self.assertEqual(last["data"]["handoff"],"idle_upgrade")
        self.assertEqual(self.ledger.status(pin["reservation_id"])["state"],"CANCELED")

    def test_deployment_volume_excludes_live_controllers_and_offline_installer(self):
        with deployment_owner(self.root):
            with self.assertRaises(PhysicalError) as caught, deployment_owner(self.root):
                self.fail("concurrent controller/installer acquired the volume")
        self.assertEqual(caught.exception.code,"plugin.physical_hot_reload_forbidden")
        with deployment_owner(self.root):
            self.assertTrue((self.root/".controller-lock.sqlite").exists())

    def test_forged_claims_and_provenance_are_blocked_before_device_dispatch(self):
        self.reject("plugin.permission_denied",self.request,"verifier",{"action":"install"})
        self.reject("plugin.fence_rejected",self.request,"camera",{"action":"next","instance_id":"other",**self.deploy["pin"]["sensor"]})
        pin = self.reserve()
        self.reject("tool.binding_unavailable",self.run_op,dict(pin,version="9.0.0"))
        self.assertIsNone(self.poll("camera"))
        with self.assertRaises(PhysicalError) as caught:
            load_deployment(self.site,"sha256:"+"f"*64)
        self.assertEqual(caught.exception.code,"plugin.permission_denied")
        for key in ("device_lease","stop_protocol","verifier","operation_ledger"):
            item = json.loads((self.site/"camera.plugin.json").read_text())
            item.pop(key)
            with self.assertRaises(DescriptorError) as missing:
                compute_content_digest(item,self.site/"trust")
            self.assertEqual(missing.exception.code,"plugin.physical_contract_missing")

    def test_missing_physical_contract_rejected_at_publish_and_deploy_load(self):
        path = self.site / "camera.plugin.json"
        original = json.loads(path.read_bytes())
        for field in ("device_lease","stop_protocol","operation_ledger","verifier","self_verifier"):
            with self.subTest(field=field):
                candidate = dict(original)
                if field == "self_verifier":
                    candidate["verifier"] = candidate["id"]
                    code = "plugin.verifier_invalid"
                else:
                    candidate.pop(field)
                    code = "plugin.physical_contract_missing"
                path.write_text(json.dumps(candidate),encoding="utf-8")
                request = Namespace(action="publish",definition=path,root=self.root/"registry",expect="absent",
                                    trust_root=self.site/"trust",request_id="negative-"+field)
                with self.assertRaises(RegistryError) as refused:
                    publish_v2(request)
                with self.assertRaises(DescriptorError) as unavailable:
                    load_deployment(self.site,self.image)
                self.assertEqual((refused.exception.code,unavailable.exception.code),(code,code))
        self.assertIsNone(self.poll("camera"))

    def test_source_scope_is_frozen_and_no_mismatched_endpoint_takes_device_claim(self):
        pin = self.reserve()
        self.run_op(pin)
        request = {"action":"next","instance_id":self.camera,**pin["sensor"]}
        for wrong in ({"source_id":"camera.back"},{"topic":"/elsewhere"},{"ros_domain_id":False}):
            self.reject("plugin.permission_denied",self.request,"camera",dict(request,**wrong))
        offered = self.ledger.status(pin["reservation_id"])
        self.assertEqual(offered["state"],"OFFERED")
        lease = self.poll("camera")["lease"]
        self.reject("plugin.fence_rejected",self.request,"camera",{"action":"check","lease":dict(lease,epoch=True)})
        reference = "frames/"+hashlib.sha256(pin["reservation_id"].encode()).hexdigest()+f"-{lease['epoch']}.cdr"
        sample = {"artifact_ref":reference,"artifact_sha256":sha(b"frame"),"source_id":"camera.back","frame_id":"optical","stamp_ns":1234}
        self.reject("tool.receipt_invalid",self.request,"camera",{"action":"candidate","lease":lease,"measurement":sample})
        self.assertIsNone(self.ledger.status(pin["reservation_id"])["candidate"])

    def test_journal_failure_rolls_back_claim_before_provider_can_read(self):
        pin = self.reserve()
        self.run_op(pin)
        from unittest.mock import patch
        with patch.object(self.ledger.journal,"emit",side_effect=OSError("journal unavailable")):
            with self.assertRaises(OSError):
                self.poll("camera")
        self.assertEqual(self.ledger.status(pin["reservation_id"])["state"], "OFFERED")
        self.assertEqual(self.poll("camera")["lease"]["operation_id"],pin["reservation_id"])
        self.assertIsNone(self.poll("camera"))

    def test_diagnostic_config_does_not_change_hardware_contract(self):
        before = self.host.catalog()["tools"][0]["_physical"]
        self.assertEqual(self.ledger.diagnostic_config(self.resource,b'{"diagnostic_interval_ms":5000}'),5000)
        for denied in (b'{"lease_ms":999}',b'{"topic":"/new"}',b'{"diagnostic_interval_ms":true}'):
            self.assertEqual(self.ledger.diagnostic_config(self.resource,denied),5000)
        self.assertEqual(before,self.host.catalog()["tools"][0]["_physical"])
        events = [json.loads(raw) for raw, in self.ledger.db.execute("SELECT event FROM module_bus_events")]
        rejected = [e for e in events if e["kind"] == "plugin.physical.config.rejected"]
        self.assertEqual(len(rejected),3)
        self.assertTrue(all(e["data"]["observed_digest"] != e["data"]["accepted_digest"] for e in rejected))


if __name__ == "__main__":
    unittest.main()
