"""SDD-045. Local admin CAS, durable freeze and explicit process handoff."""
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from test_plugin_descriptor import descriptor_fixture


AGENT = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(AGENT))
from plugin_descriptor import compute_content_digest
from plugin_manager import change
from v2_registry import RegistryError, V2Registry, database


class RegistryContract(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "registry"
        self.trust = self.base / "trust"
        self.seq = 0
        self.item = descriptor_fixture(self.trust)
        self.name = self.item["id"]

    def descriptor(self, item):
        self.seq += 1
        path = self.base / f"candidate-{self.seq}.plugin.json"
        path.write_text(json.dumps(item), encoding="utf8")
        return path

    def request(self, action, target, expect=None, request="trial"):
        return Namespace(kind="v2", action=action, root=self.root, trust_root=self.trust, expect=expect,
                         request_id=request, id=target if action in ("drain","remove") else None,
                         definition=target if action in ("validate","check","publish") else None)

    def install(self):
        return change(self.request("publish", self.descriptor(self.item), "absent"))

    def authorize_exports(self, item, names):
        # Emulate the site administrator, separately from the candidate author.
        path = self.trust / "catalog.json"
        catalog = json.loads(path.read_text(encoding="utf8"))
        records = catalog["implementations"][item["provenance"]["code_digest"]]
        next(record for record in records if record["id"] == item["id"])["exports"] = names
        path.write_text(json.dumps(catalog),encoding="utf8")

    def test_admin_lifecycle_reservations_survive_restart_without_rebinding(self):
        receipt = self.install()
        old = self.item["content_digest"]
        newer_item = {**self.item, "version":"2.0.0"}
        newer_item["content_digest"] = compute_content_digest(newer_item,self.trust)
        newer = self.descriptor(newer_item)
        with V2Registry(self.root) as registry:
            plan = registry.freeze_plan("task-a", "model-a", [{"binding":"a"}], [(self.name,old,"task-a/step/1")])
            self.assertEqual(registry.entries()[0]["frozen"], 1)
        drained = change(self.request("drain", self.name, receipt["revision"], "drain1"))
        with V2Registry(self.root) as registry:
            self.assertEqual(registry.saved_plan("task-a"), plan)
            self.assertEqual(registry.freeze_plan("task-a", "new-model", [], []), plan)
            registry.reserve(self.name,old,"task-a/step/1")
            with self.assertRaisesRegex(RegistryError,"plugin.draining"):
                registry.reserve(self.name,old,"other-task/step/1")
            with self.assertRaisesRegex(RegistryError,"cleanup_incomplete"):
                registry.ack_disposed(self.name,drained["revision"])
            with self.assertRaisesRegex(RegistryError,"cleanup_incomplete"):
                change(self.request("publish",newer,drained["revision"],"early"))
            registry.settle(self.name,old,"task-a/step/1")
            with self.assertRaisesRegex(RegistryError,"cleanup_incomplete"):
                change(self.request("remove",self.name,drained["revision"],"before-disposal"))
            registry.ack_disposed(self.name,drained["revision"])
            updated = change(self.request("publish",newer,drained["revision"],"upgrade"))
            self.assertEqual(registry.lookup(self.name,old),self.item)
            self.assertEqual(registry.entries()[0]["descriptor"],newer_item)
            with self.assertRaisesRegex(RegistryError,"binding_unavailable"):
                registry.reserve(self.name,newer_item["content_digest"],"task-a/step/1")
            drained = change(self.request("drain",self.name,updated["revision"],"drain2"))
            registry.ack_disposed(self.name,drained["revision"])
            removed = change(self.request("remove",self.name,drained["revision"],"remove"))
            self.assertEqual(registry.entries(),[])
            self.assertTrue(registry.claimed_exports())
            self.assertEqual(registry.saved_plan("task-a"),plan)
            with self.assertRaisesRegex(RegistryError,"version_conflict"):
                change(self.request("publish",newer,"absent","stale"))
            reinstall = change(self.request("publish",newer,removed["revision"],"reinstall"))
            self.assertNotEqual(reinstall["revision"],updated["revision"])

    def test_concurrent_idempotence_publication_rejection_and_replay(self):
        req = self.request("publish", self.descriptor(self.item), "absent")
        with ThreadPoolExecutor(2) as pool:
            a,b = list(pool.map(change,[req,req]))
        self.assertEqual(a,b)
        bad = self.request("drain",self.name,"absent","stale")
        for _ in range(2):
            with self.assertRaisesRegex(RegistryError,"version_conflict"):
                change(bad)
        with closing(sqlite3.connect(database(self.root))) as db:
            events = [json.loads(raw) for raw, in db.execute("SELECT event FROM module_bus_events ORDER BY seq")]
        self.assertEqual([e["kind"].split(".")[-1] for e in events],["requested","applied","requested","rejected"])
        self.assertEqual(events[1]["parent_event_id"],events[0]["event_id"])
        self.assertEqual(events[1]["data"]["content_digest"],self.item["content_digest"])
        with self.assertRaisesRegex(RegistryError,"request_conflict"):
            change(self.request("remove",self.name,a["revision"],"trial"))
        self.assertEqual(a,change(req))

    def test_atomic_freeze_rollback_and_publication_fault_before_commit(self):
        req = self.request("publish",self.descriptor(self.item),"absent")
        real = V2Registry._event

        def fail_applied(registry, kind, *args):
            if kind == "applied":
                raise sqlite3.OperationalError("injected journal fault")
            return real(registry,kind,*args)

        with patch.object(V2Registry,"_event",fail_applied), self.assertRaises(sqlite3.OperationalError):
            change(req)
        with V2Registry(self.root) as registry:
            self.assertEqual(registry.entries(), [])
            self.assertFalse(registry.db.execute("SELECT 1 FROM module_bus_events").fetchone())
            self.assertFalse(registry.claimed_exports())
            change(req)
            with self.assertRaisesRegex(RegistryError,"binding_unavailable"):
                registry.freeze_plan("t","first",[],[(self.name,self.item["content_digest"],"op"),("missing","x","op")])
            self.assertIsNone(registry.saved_plan("t"))
            self.assertEqual(registry.reservations(), [])

    def test_freeze_races_drain_without_admitting_late_reservations(self):
        release = self.install()
        barrier = Barrier(2)
        digest = self.item["content_digest"]

        def accept():
            with V2Registry(self.root) as registry:
                barrier.wait(timeout=10)
                try:
                    registry.freeze_plan("racer","provider",[],[(self.name,digest,"racer/step/1")])
                    return True
                except RegistryError as exc:
                    self.assertEqual(exc.code,"plugin.draining")
                    return False

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(accept)
            barrier.wait(timeout=10)
            drained = change(self.request("drain",self.name,release["revision"],"race"))
            won = future.result(timeout=10)
        with V2Registry(self.root) as registry:
            self.assertEqual(bool(drained["frozen"]),won)
            self.assertEqual(registry.saved_plan("racer") is not None,won)
            self.assertEqual(len(registry.reservations()),int(won))
            with self.assertRaisesRegex(RegistryError,"draining"):
                registry.reserve(self.name,digest,"late")

    def test_dependency_checks_pending_conflicting_and_removed_export(self):
        wire = self.item["provides"][0]
        consumer = descriptor_fixture(self.trust,plugin_id="reader")
        consumer["requires"] = [wire]
        consumer["content_digest"] = compute_content_digest(consumer,self.trust)
        req = self.request("publish",self.descriptor(consumer),"absent","reader")
        self.assertEqual(change(req)["missing_services"],[wire["name"]])
        loop = {**self.item,"requires":consumer["provides"]}
        loop["content_digest"] = compute_content_digest(loop,self.trust)
        with self.assertRaisesRegex(RegistryError,"dependency_cycle"):
            change(self.request("check",self.descriptor(loop)))
        release = self.install()
        self.assertEqual(change(self.request("check",self.descriptor(self.item)))["revision"],release["revision"])
        drained = change(self.request("drain",self.name,release["revision"],"d"))
        with V2Registry(self.root) as registry:
            registry.ack_disposed(self.name,drained["revision"])
        change(self.request("remove",self.name,drained["revision"],"r"))
        impostor = descriptor_fixture(self.trust,plugin_id="other")
        impostor["provides"] = [wire]
        self.authorize_exports(impostor,[wire["name"]])
        impostor["content_digest"] = compute_content_digest(impostor,self.trust)
        with self.assertRaisesRegex(RegistryError,"service_conflict"):
            change(self.request("publish",self.descriptor(impostor),"absent","other"))

    def test_kernel_exports_are_reserved_even_with_site_approval(self):
        for name in ("inspect","status"):
            with self.subTest(name=name):
                candidate = {**self.item,"provides":[{**self.item["provides"][0],"name":name}]}
                self.authorize_exports(candidate,[name])
                candidate["content_digest"] = compute_content_digest(candidate,self.trust)
                with self.assertRaisesRegex(RegistryError,"execution kernel owns"):
                    change(self.request("publish",self.descriptor(candidate),"absent",name))

    def test_settlement_releases_dependency_closure_atomically_and_replays(self):
        self.install()
        sub = descriptor_fixture(self.trust,plugin_id="sub")
        change(self.request("publish",self.descriptor(sub),"absent","sub"))
        expected = {row["id"]:row["content_digest"] for row in (self.item,sub)}
        with V2Registry(self.root) as registry:
            registry.freeze_plan("task","test",[],[(k,v,"op") for k,v in expected.items()])
            with self.assertRaisesRegex(RegistryError,"complete reservation"):
                registry.settle_operation("op",{self.name:expected[self.name]})
            self.assertEqual(registry.reservations_for("op"),expected)
            registry.settle_operation("op",expected)
            self.assertEqual(registry.reservations_for("op"),{})
            registry.settle_operation("op",expected)

    def test_input_mutation_and_semver_reuse_are_rejected_with_audit(self):
        a = self.install()
        tampered = {**self.item, "version":"5.0.0"}
        with self.assertRaisesRegex(RegistryError,"digest_mismatch"):
            change(self.request("publish",self.descriptor(tampered),a["revision"],"tamper"))
        mutated = {**self.item,"parameters":{"type":"boolean"}}
        mutated["content_digest"] = compute_content_digest(mutated,self.trust)
        with self.assertRaisesRegex(RegistryError,"digest_mismatch"):
            change(self.request("check",self.descriptor(mutated),a["revision"]))
        with self.assertRaisesRegex(RegistryError,"digest_mismatch"):
            change(self.request("publish",self.descriptor(mutated),a["revision"],"immutable"))
        with V2Registry(self.root) as registry:
            self.assertEqual(registry.entries()[0]["descriptor"],self.item)
            last = [json.loads(raw)["data"] for raw, in registry.db.execute(
                "SELECT event FROM module_bus_events ORDER BY seq")]
            self.assertIn("input_sha256",last[2])

    def test_check_is_readonly_real_cli_and_physical_admission(self):
        path = self.descriptor(self.item)
        result = subprocess.run([sys.executable,"-B",str(AGENT/"plugin_manager.py"),"validate",str(path),
                                 "--kind","v2","--trust-root",str(self.trust)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)["content_digest"],self.item["content_digest"])
        self.assertEqual(change(self.request("check",path))["revision"],"absent")
        self.assertFalse(self.root.exists())
        physical = descriptor_fixture(self.trust,plugin_id="camera.site",runtime="process",track="physical")
        path = self.descriptor(physical)
        self.assertEqual(change(self.request("validate",path))["class"],"physical")
        with self.assertRaisesRegex(RegistryError,"physical_hot_reload_forbidden"):
            change(self.request("publish",path,"absent","camera"))
        with V2Registry(self.root) as registry:
            self.assertEqual(registry.entries(),[])


if __name__ == "__main__":
    unittest.main()
