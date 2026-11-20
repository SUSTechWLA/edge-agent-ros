"""SDD-051. Build, admit, freeze and call the released plugins on a software-only site."""
from argparse import Namespace
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "scripts")]
from edge_contracts.navigation import validate_route
from edge_contracts.spatial_facts import SpatialFactError
from module_journal import ModuleJournal
from perception.spatial_facts.gateway import published_batch
from perception.spatial_facts.odom_capture import position_sample
from perception.spatial_facts.replay import capture
from planning import local_plan
from prepare_navigation import bench_settings, prepare
from prepare_soft_pilots import sha
from site_pack import verify
from soft_host import Host
from soft_runtime import SoftRuntimeError
from v2_registry import V2Registry


STAMP = 1770911200000000001


class NavigationPack(unittest.IsolatedAsyncioTestCase):
    def test_ros_covariance_scalars_become_interoperable_numbers(self):
        from types import SimpleNamespace as Row
        class DriverFloat(float):
            pass
        cfg = {"actor_ref": "rover", "source_id": "wheel", "clock_id": "unix", "calibration_ref": "reported",
               "localization_epoch": "e1", "frame_id": "odom", "child_frame_id": "base",
               "map_ref": "sha256:" + "a" * 64}
        cov = [0.0] * 36
        cov[0] = cov[7] = DriverFloat(.001)
        msg = Row(header=Row(frame_id="odom", stamp=Row(sec=1, nanosec=0)), child_frame_id="base",
                  pose=Row(covariance=cov, pose=Row(position=Row(x=0., y=0.))))
        sample = position_sample(msg, cfg)
        self.assertEqual(sample["variance_m2"], [.001, .001])
        self.assertIs(type(sample["variance_m2"][0]), float)

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.site = self.base / "site"
        manifest = json.dumps({"schemaVersion": 2, "layers": [], "config": {"digest": "sha256:" + "0"*64}}).encode()
        self.settings = bench_settings()
        for snapshot in self.settings["sensing.replay"]["actors"].values():
            snapshot["as_of_ns"] = str(STAMP + 5)
            for sample in snapshot["samples"]:
                sample["stamp_ns"] = str(STAMP)
        lock_hash = prepare(self.site, manifest, "a" * 40, settings=self.settings)
        lock, items = verify(self.site, lock_hash)
        self.assertEqual(lock["policy"]["effects"], ["read"])
        self.assertTrue(all(item["class"] == "soft" for item in items.values()))
        self.registry = V2Registry(self.base / "plugins")
        self.addCleanup(self.registry.close)
        self.registry.install_pack(list(items.values()), lock_hash)
        self.journal = ModuleJournal(self.base / "journal.sqlite")
        self.addCleanup(self.journal.close)
        self.host = Host(self.registry, self.site, self.journal, sha(manifest))
        await self.host.reconcile()
        self.assertEqual({}, self.host.errors)

    def caps(self):
        return {cap["id"]: cap for cap in self.host.catalog()["tools"]}

    async def freeze(self, task, cap, args):
        step = {"tool": cap["id"], "version": cap["version"], "binding": cap["_soft"]["binding"],
                "soft_pin": cap["_soft"], "args": args}
        _, (saved,) = await self.host.command({"action": "freeze_plan", "task": task,
                                               "steps": [step], "provider": "local"})
        pin = saved["soft_pin"]
        return pin, {"version": saved["version"], "arguments": args, "operation_id": pin["reservation_id"]}

    async def call(self, task, tool, args):
        pin, wire = await self.freeze(task, self.caps()[tool], args)
        receipt = await self.host.command({"action": "call", "pin": pin, "wire": wire})
        await self.host.command({"action": "settle", "pin": pin})
        return receipt

    async def test_four_profiles_through_pinned_software_host(self):
        for actor, route_type, dimension in (("rover", "ground", 2), ("dog", "terrain", 2),
                                             ("person", "guidance", 2), ("drone", "flight", 3)):
            with self.subTest(actor=actor):
                step, = local_plan(f"为 {actor} 试算到 office 的路线", self.caps())
                receipt = await self.call("plan-" + actor, step["tool"], step["args"])
                self.assertEqual("SUCCEEDED", receipt["state"], receipt)
                pose, route = (receipt["evidence"][k] for k in ("localization", "route"))
                self.assertEqual(route_type, route["route_type"])
                self.assertEqual(actor, route["actor_ref"])
                self.assertEqual(dimension, len(route["waypoints"][-1]))
                self.assertEqual(str(STAMP), route["localization_stamp_ns"])
                self.assertEqual(("replay", str(STAMP), str(STAMP + 5)),
                                 (pose["basis"], pose["stamp_ns"], pose["as_of_ns"]))
                self.assertTrue(all(s["stamp_ns"] == str(STAMP) for s in pose["sources"]))
                validate_route(route, **{k: route[k] for k in ("actor_ref", "map_ref", "frame_id",
                                                               "localization_epoch", "profile_ref")})
        observations = await self.call("observations", "read_spatial_observations", {"actor_ref": "rover"})
        self.assertEqual(str(STAMP), observations["evidence"]["samples"][0]["stamp_ns"])
        self.assertEqual([], self.registry.reservations())

    async def test_dependency_drain_keeps_old_plan_and_independent_resolution(self):
        cap = self.caps()["nav_plan"]
        args = {"actor_ref": "rover", "destination_ref": "office"}
        pin, wire = await self.freeze("prior", cap, args)
        plugin = "localization.weighted"
        self.assertIn(plugin, pin["dependencies"])
        row = next(r for r in self.registry.entries() if r["descriptor"]["id"] == plugin)

        def manage(action, revision):
            release = self.site / "releases" / (plugin + ".plugin.json")
            req = Namespace(action=action, kind="v2", id=plugin if action != "publish" else None,
                            definition=release if action == "publish" else None, expect=revision,
                            request_id="nav-" + action, root=self.base / "plugins", trust_root=self.site / "trust")
            return self.registry.change(req, self.site / "trust")["revision"]

        drain = manage("drain", row["revision"])
        await self.host.reconcile()
        self.assertNotIn("nav_plan", self.caps())
        with self.assertRaises(SoftRuntimeError) as failure:
            await self.freeze("late", cap, args)
        self.assertEqual("tool.binding_unavailable", failure.exception.code)
        self.assertIsNone(self.registry.saved_plan("late"))
        self.assertEqual("SUCCEEDED", (await self.call("resolve", "nav_resolve", args))["state"])
        op = {"action": "call", "pin": pin, "wire": wire}
        receipt = await self.host.command(op)
        self.assertEqual("SUCCEEDED", receipt["state"], receipt)
        self.assertEqual(receipt, await self.host.command(op))
        await self.host.command({"action": "settle", "pin": pin})
        await self.host.reconcile()
        removed = manage("remove", drain)
        await self.host.reconcile()
        self.assertNotIn("nav_locate", self.caps())
        manage("publish", removed)
        await self.host.reconcile()
        self.assertEqual("SUCCEEDED", (await self.call("restored", "nav_plan", args))["state"])
        self.assertEqual([], self.registry.reservations())
        kinds = [json.loads(raw)["kind"] for raw, in self.journal.db.execute("SELECT event FROM module_bus_events")
                 if json.loads(raw).get("operation_id") == wire["operation_id"]]
        self.assertEqual(1, kinds.count("plugin.operation.succeeded"))

    def test_live_gateway_time_window_and_actor(self):
        path = self.base / "published.json"
        batch = capture(self.settings["sensing.replay"], "rover")
        batch.update(clock_id="unix", basis="published")
        for s in batch["samples"]:
            s["clock_id"] = "unix"
        path.write_text(json.dumps(batch), encoding="utf8")
        self.assertEqual(STAMP, published_batch(path, "rover", STAMP + 5, 5)["samples"][0]["stamp_ns"])
        for actor, now in (("dog", STAMP), ("rover", STAMP - 1), ("rover", STAMP + 6)):
            with self.subTest(actor=actor, now=now), self.assertRaises(SpatialFactError):
                published_batch(path, actor, now, 5)


if __name__ == "__main__":
    unittest.main()
