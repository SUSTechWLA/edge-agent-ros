"""SDD-045. Exercise the actual soft-host commands, registry, site artifacts and journals."""
from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "scripts")]
from module_journal import ModuleJournal
from plugin_descriptor import compute_content_digest
from prepare_soft_pilots import DEFAULT_SERIES, prepare_site, sha
from soft_host import Host, socket_owner
from soft_runtime import SoftRuntimeError
from v2_registry import V2Registry


class HostContracts(unittest.IsolatedAsyncioTestCase):
    series = "derived.series_mean"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.site = self.base / "site"
        self.trust = self.site / "trust"
        manifest = json.dumps({"schemaVersion":2,"layers":[],"config":{"digest":"sha256:" + "0"*64}}).encode()
        prepare_site(self.site, manifest, "a"*40, DEFAULT_SERIES)
        self.image = sha(manifest)
        self.registry = V2Registry(self.base / "plugins")
        self.addCleanup(self.registry.close)
        self.journal = ModuleJournal(self.base / "journal.sqlite")
        self.addCleanup(self.journal.close)
        self.host = self.restart()
        self.sequence = 0

    def restart(self):
        return Host(self.registry, self.site, self.journal, self.image)

    def definition(self, name):
        return json.loads((self.site / "releases" / (name + ".plugin.json")).read_bytes())

    def admin(self, action, name, expect=None, definition=None):
        self.sequence += 1
        path = self.site / "releases" / (name + ".plugin.json")
        if definition:
            definition["content_digest"] = compute_content_digest(definition, self.trust)
            path.write_text(json.dumps(definition), encoding="utf-8")
        args = Namespace(action=action,kind="v2",id=name if action!="publish" else None,
                         definition=path if action=="publish" else None,expect=expect or "absent",
                         request_id=f"host-req-{self.sequence}",root=self.base / "plugins",trust_root=self.trust)
        return self.registry.change(args, self.trust)["revision"]

    async def freeze(self, task="job", tool=None, arguments=None):
        name = tool or self.series
        cap = next(c for c in self.host.catalog()["tools"] if c["id"] == name)
        step = {"tool":name, "version":cap["version"], "soft_pin":cap["_soft"], "binding":cap["_soft"]["binding"],
                "args":{"series":"[1,2,3]"} if arguments is None else arguments}
        _, (saved,) = await self.host.command({"action":"freeze_plan", "task":task,"steps":[step],"provider":"local"})
        pin = saved["soft_pin"]
        return pin, {"operation_id":pin["reservation_id"],"version":saved["version"],"arguments":saved["args"]}

    async def reject(self, code, operation):
        with self.assertRaises(SoftRuntimeError) as caught:
            await operation
        self.assertEqual(caught.exception.code, code)

    async def test_saved_plan_survives_restart_during_drain_and_old_generation_settles(self):
        revision = self.admin("publish", self.series)
        await self.host.reconcile()
        pin, wire = await self.freeze()
        drain = self.admin("drain", self.series, revision)
        self.host = self.restart()
        await self.host.reconcile()
        self.assertFalse(self.host.catalog()["tools"])
        receipt = await self.host.command({"action":"call","pin":pin,"wire":wire})
        self.assertEqual(receipt["evidence"]["value"], 2)
        self.assertEqual(await self.host.invoke(pin,wire), receipt)
        await self.host.command({"action":"settle","pin":pin})
        await self.host.command({"action":"settle","pin":pin})
        await self.host.reconcile()
        self.admin("remove", self.series, drain)
        self.assertEqual(self.registry.saved_plan("job")[1][0]["soft_pin"], pin)
        self.assertFalse(self.registry.reservations())
        kinds = [json.loads(raw)["kind"] for raw, in self.journal.db.execute("SELECT event FROM module_bus_events")]
        self.assertEqual(kinds.count("plugin.operation.intent"), 1)
        self.assertEqual(kinds.count("plugin.operation.succeeded"), 1)

    async def test_wire_scope_arguments_and_full_settlement_pin_must_match_plan(self):
        self.admin("publish", self.series)
        await self.host.reconcile()
        pin, wire = await self.freeze()
        for change in ({"arguments":{"series":"99"}},{"version":"changed"},{"trace":{"task_id":"elsewhere"}},
                       {"operation_id":"other/step/1"}, {"new_field":"ignored?"}):
            await self.reject("tool.binding_unavailable", self.host.invoke(pin, dict(wire, **change)))
        forged = deepcopy(pin)
        forged["dependencies"][self.series][2] = "forged"
        for action in ("call","settle"):
            await self.reject("tool.binding_unavailable", self.host.command({"action":action,"pin":forged,"wire":wire}))
        self.assertTrue(self.registry.reservations())
        self.assertFalse(self.journal.db.execute("SELECT 1 FROM soft_operations").fetchone())
        await self.reject("plugin.permission_denied", self.host.command({"action":"settle","pin":pin},session_only=True))

    async def test_schema_trust_loss_blocks_existing_pins_and_recovers_same_fiber(self):
        self.admin("publish", self.series)
        await self.host.reconcile()
        pin, wire = await self.freeze()
        before = self.host.catalog()["plugins"][0]["generation"]
        schema = self.trust / "assets" / (self.series + ".schema.json")
        saved = schema.read_bytes()
        schema.unlink()
        await self.host.reconcile()
        self.assertFalse(self.host.catalog()["tools"])
        await self.reject("plugin.dependency_unavailable", self.host.invoke(pin, wire))
        schema.write_bytes(saved)
        await self.host.reconcile()
        self.assertEqual(self.host.catalog()["plugins"][0]["generation"], before)
        self.assertEqual((await self.host.invoke(pin, wire))["state"], "SUCCEEDED")

    async def test_config_rejection_recovery_and_pins_keep_their_revision(self):
        self.admin("publish", self.series)
        await self.host.reconcile()
        pin, wire = await self.freeze()
        directory = self.site / "config"
        directory.mkdir()
        path = directory / (self.series + ".json")
        path.write_text("[", encoding="utf-8")
        await self.host.reconcile()
        self.assertEqual(self.host.catalog()["plugins"][0]["last_error"], "plugin.config_invalid")
        await self.reject("plugin.dependency_unavailable", self.host.invoke(pin, wire))
        path.write_text('{"tuning":5}', encoding="utf-8")
        await self.host.reconcile()
        second, _ = await self.freeze(task="new")
        self.assertNotEqual(second["binding"], pin["binding"])
        self.assertEqual((await self.host.invoke(pin,wire))["state"], "SUCCEEDED")

    async def test_frozen_a_then_b_after_restart_first_dispatch_observes_each_original_value(self):
        # An observable config-dependent adapter exercises the real host, persistence,
        # descriptor validation and output contract. Unit inputs never leave this process.
        def configurable_adapter(item, site, profile):
            def apply(ctx):
                def invoke(wire):
                    return {"operation_id":wire["operation_id"],"state":"SUCCEEDED","evidence":{
                        "source":"unit","input_sha256":"a"*64,"input_count":1,"output_count":1,
                        "value":ctx.config()["settings"]["scale"],"definition_version":"unit"}}
                ctx.tools.register(self.series, invoke)
            return apply

        self.admin("publish", self.series)
        directory = self.site / "config"
        directory.mkdir()
        path = directory / (self.series + ".json")
        with patch("soft_host.adapter", configurable_adapter):
            path.write_text('{"scale":2}', encoding="utf-8")
            await self.host.reconcile()
            a, wa = await self.freeze("old")
            path.write_text('{"scale":7}', encoding="utf-8")
            await self.host.reconcile()
            b, wb = await self.freeze("new")
            self.host = self.restart()
            await self.host.reconcile()
            self.assertEqual((await self.host.invoke(a,wa))["evidence"]["value"], 2)
            self.assertEqual((await self.host.invoke(b,wb))["evidence"]["value"], 7)
            fresh, wc = await self.freeze("current")
            self.assertEqual((await self.host.invoke(fresh,wc))["evidence"]["value"], 7)
            self.assertEqual(fresh["binding"], b["binding"])
            self.assertNotEqual(a["binding"], b["binding"])
        snapshot_rows = self.journal.db.execute("SELECT settings FROM soft_config_snapshots").fetchall()
        self.assertEqual(sorted(snapshot_rows), [('{"scale":2}',), ('{"scale":7}',)])

    async def test_restore_rejects_modified_snapshot_or_provenance_before_dispatch(self):
        self.admin("publish", self.series)
        await self.host.reconcile()
        pin, wire = await self.freeze()
        with self.journal.db:
            self.journal.db.execute("UPDATE soft_config_snapshots SET settings=?", ('{"corrupted":true}',))
        self.host = self.restart()
        await self.host.reconcile()
        await self.reject("tool.binding_unavailable", self.host.invoke(pin,wire))
        self.assertFalse(self.journal.db.execute("SELECT 1 FROM soft_operations").fetchone())
        self.host = Host(self.registry,self.site,self.journal,"sha256:" + "0"*64)
        await self.host.reconcile()
        await self.reject("plugin.dependency_unavailable", self.host.invoke(pin,wire))
        self.assertTrue(self.registry.reservations_for(pin["reservation_id"]))

    async def test_plaintext_credential_is_rejected_while_references_remain_usable(self):
        self.admin("publish", self.series)
        directory = self.site / "config"
        directory.mkdir()
        path = directory / (self.series + ".json")
        path.write_text('{"password":"unit-plain"}', encoding="utf-8")
        await self.host.reconcile()
        self.assertEqual(self.host.catalog()["plugins"][0]["last_error"],"plugin.config_invalid")
        self.assertFalse(self.journal.db.execute("SELECT 1 FROM soft_config_snapshots").fetchone())
        path.write_text('{"password":{"secret_ref":"site/model-reader"}}', encoding="utf-8")
        await self.host.reconcile()
        await self.freeze("refs")
        rows = self.journal.db.execute("SELECT settings FROM soft_config_snapshots").fetchall()
        self.assertEqual(rows, [('{"password":{"secret_ref":"site/model-reader"}}',)])

    async def test_initial_probe_requires_contract_then_session_preserves_old_hex_wire(self):
        name = "session.default"
        self.admin("publish", name)
        expected = self.definition(name)["config_schema"]["properties"]["profile"]["const"]["health_expected"]
        with patch("soft_host.http_exchange", return_value=(200, {"wrong":"service"})):
            await self.host.reconcile()
        self.assertEqual(self.host.catalog()["plugins"][0]["lifecycle"], "PENDING")
        self.assertEqual(self.host.catalog()["services"], {})
        self.host.probed.clear()
        with patch("soft_host.http_exchange", return_value=(200, expected)):
            await self.host.reconcile()
        pin = (await self.host.command({"action":"session_pin"}, session_only=True))["service"]["pin"]
        wire = {"schema_version":1,"request_id":"fresh","plugin_id":name,"plugin_version":"1",
                "binding":pin["binding"].removeprefix("sha256:"),"task_id":"job"}
        def req(w=wire):
            return self.host.command({"action":"session","pin":pin,"wire":w}, session_only=True)
        with patch("soft_host.http_exchange", return_value=(200,{"request_id":"fresh","document":{}})) as post:
            await self.reject("tool.binding_unavailable", req(dict(wire,binding=pin["binding"])))
            with self.assertRaises(SoftRuntimeError):
                await req(dict(wire,binding="0"*64))
            self.assertEqual((await req())[0], 200)
            self.assertEqual(post.call_count, 1)
        self.host.probed[name] -= 10
        self.assertFalse(self.host.catalog()["services"])
        await self.reject("plugin.dependency_unavailable", req())

    async def test_interrupted_disposal_never_acknowledged_by_new_process(self):
        version = self.admin("publish",self.series)
        await self.host.reconcile()
        self.admin("drain",self.series,version)
        # Crash after the host committed its UNLOADING event, before a disposer returned.
        with self.journal.db:
            self.journal.db.execute("UPDATE soft_instances SET lifecycle='UNLOADING' WHERE plugin_id=?", (self.series,))
        self.host = self.restart()
        await self.host.reconcile()
        self.assertEqual(self.host.catalog()["plugins"][0]["last_error"], "plugin.cleanup_incomplete")
        self.assertFalse(self.registry.entries()[0]["disposed"])

    def test_second_host_cannot_take_live_socket_directory(self):
        (self.base / "agent").mkdir()
        with socket_owner(self.base):
            with self.assertRaises(sqlite3.OperationalError), socket_owner(self.base):
                self.fail("second host took the live socket")
        with socket_owner(self.base):
            self.assertTrue((self.base / "agent" / ".host-lock.sqlite").is_file())

    async def test_readiness_and_running_vs_unknown_write_recovery(self):
        key, tool = "evidence.evidence_schema", "evidence_schema"
        item = self.definition(key)
        item["effects"] = item["config_schema"]["properties"]["profile"]["const"]["legacy"]["effect"] = "idempotent"
        self.admin("publish", key, definition=item)
        expected = item["config_schema"]["properties"]["profile"]["const"]["health_expected"]
        with patch("soft_host.http_exchange", return_value=(200, expected)):
            await self.host.reconcile()
        pin, wire = await self.freeze(tool=tool, arguments={})
        def invoke():
            return self.host.invoke(pin, wire)
        with patch("soft_host.http_exchange", return_value=(200, {"operation_id":wire["operation_id"],"state":"RUNNING"})):
            self.assertEqual((await invoke())["state"], "RUNNING")
        self.host = self.restart()
        with patch("soft_host.http_exchange", return_value=(200, expected)):
            await self.host.reconcile()
        reply = {"operation_id":wire["operation_id"],"state":"SUCCEEDED","evidence":
                 {"schema_version":1,"table":"events","columns":[],"query_id":"test"}}
        with patch("soft_host.http_exchange", return_value=(200,reply)) as post:
            self.assertEqual(await invoke(),reply)
            self.assertEqual(await invoke(),reply)
            self.assertEqual(post.call_count,1)
        other, other_wire = await self.freeze(task="unknown",tool=tool, arguments={})
        with patch("soft_host.http_exchange", side_effect=ConnectionError):
            await self.reject("plugin.provider_unavailable", self.host.invoke(other,other_wire))
        self.host = self.restart()
        with patch("soft_host.http_exchange", return_value=(200, expected)):
            await self.host.reconcile()
        with patch("soft_host.http_exchange") as post:
            await self.reject("tool.outcome_unknown", self.host.invoke(other,other_wire))
            post.assert_not_called()
        self.assertTrue(self.registry.reservations_for(other["reservation_id"]))


if __name__ == "__main__":
    unittest.main()
