"""SDD-054 admission, retry and immutable binding checks against an actual DSH soft host."""
import asyncio
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/"agent"), str(ROOT/"scripts")]
from client import field_plugin_publish as deploy
from kernel.catalog import _track_records
from module_journal import ModuleJournal
from prepare_navigation import bench_settings, prepare
from prepare_perception_site import prepare as perception
from soft_host import Host
from v2_registry import V2Registry

PROGRAM = {"statistic":"p10", "unknown_if":["valid_fraction",0.6,"lt"],
           "near_if":["depth_m","distance_m","le"], "review_order":"nearest_first"}


class PluginPublication(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = bench_settings()
        del settings["sensing.replay"]
        manifest = json.dumps({"schemaVersion":2,"layers":[],"config":{"digest":"sha256:"+"a"*64}}).encode()
        lock = prepare(root/"old", manifest, "b"*40, live=True, settings=settings)
        locks = perception(root/"old",lock,root/"q","c"*40,"sha256:"+"d"*64,
                           ROOT/"examples/object-perception/jetson-rover.json","rover",queries=True)
        self.site = root/"q/queries"
        _, self.before = deploy.verify(self.site,locks["queries"])
        self.registry = root/"registry"
        self.db = V2Registry(self.registry)
        self.addCleanup(self.db.close)
        self.db.install_pack(list(self.before.values()),locks["queries"])
        journal = ModuleJournal(root/"host.sqlite")
        self.addCleanup(journal.close)
        self.host = Host(self.db,self.site,journal,self.before[deploy.TEMPLATE]["provenance"]["image_digest"])
        self.policy = root/"approved.json"
        self.policy.write_text(json.dumps({"schema_version":1,"bindings":{"original":"sha256:"+"0"*64}}))
        self.evidence = root/"evidence"
        self.candidate = {"name":"review_zones","description":"按阈值复核静态相机视区","program":PROGRAM}
        self.loops = asyncio.new_event_loop()
        self.addCleanup(self.loops.close)
        revision = patch.dict("os.environ", {"EDGE_FIELD_ADAPTER_COMMIT":"3"*40})
        revision.start()
        self.addCleanup(revision.stop)

    @contextmanager
    def capabilities(self, url, timeout):
        self.assertEqual(url,"http://localhost:8794/agent/capabilities")
        async def catalog():
            await self.host.reconcile()
            view = self.host.catalog()
            rows = _track_records(view, "v2_soft", {r["id"]:r for r in view["tools"]})
            return json.dumps({"records":list(rows)}).encode()
        yield io.BytesIO(self.loops.run_until_complete(catalog()))

    def submit(self, candidate=None):
        return deploy.publish(self.site,self.registry,self.policy,candidate or self.candidate,
                              "http://localhost:8794",self.evidence)

    def test_online_admission_pinned_and_repeat_is_idempotent(self):
        with patch.object(deploy, "urlopen", self.capabilities):
            first = self.submit()
            second = self.submit()
        self.assertEqual(first, second)
        self.assertEqual(first["plugin_id"], "field.review_zones")
        self.assertEqual(first["tool_id"], "review_zones")
        self.assertEqual(first["binding"],deploy.read(self.policy)["bindings"]["review_zones"])
        self.assertEqual(deploy.read(self.policy)["bindings"]["original"],"sha256:"+"0"*64)
        _, items = deploy.verify(self.site, first["lock_digest"])
        for k,v in self.before.items():
            self.assertEqual(items[k],v)
        self.assertEqual(set(items)-set(self.before), {"field.review_zones"})
        self.assertEqual(deploy.read(self.evidence/"published-plugin.json"),first)
        self.assertTrue(all(c["passed"] for c in first["validation"]["cases"]))
        with self.assertRaises(ValueError):
            self.submit({**self.candidate, "description":"a different immutable descriptor"})

    def test_failed_candidate_and_registry_drift_never_grant_authority(self):
        lockfile = self.site/"pack.lock.json"
        initial = lockfile.read_bytes()
        with self.assertRaises(ValueError):
            self.submit({**self.candidate,"program":{**PROGRAM,"near_if":["depth_m","distance_m","gt"]}})
        with self.assertRaises(ValueError):
            self.submit({**self.candidate,"name":"../agent"})
        self.assertEqual(lockfile.read_bytes(),initial)
        lock = json.loads(initial)
        lock["profile"] = "other_site"
        lockfile.write_text(json.dumps(lock))
        with self.assertRaisesRegex(ValueError, "deployed lock"):
            self.submit()
        self.assertNotIn("review_zones",deploy.read(self.policy)["bindings"])

    def test_failed_discovery_retry_recovers_original_admission(self):
        with patch.object(deploy,"live_record", side_effect=ValueError("not available")):
            with self.assertRaisesRegex(ValueError,"not available"):
                self.submit()
        self.assertNotIn("review_zones",deploy.read(self.policy)["bindings"])
        before = self.db.entries()
        with patch.object(deploy,"urlopen",self.capabilities):
            self.submit()
        self.assertEqual(before,self.db.entries())

    def test_crash_between_registry_commit_and_lock_replace(self):
        original_write = deploy.write
        old_lock = (self.site/"pack.lock.json").read_bytes()
        def interrupted(path, raw, **kwargs):
            if path == self.site/"pack.lock.json":
                raise OSError("interrupted")
            original_write(path, raw, **kwargs)
        with patch.object(deploy,"write",interrupted), self.assertRaisesRegex(OSError, "interrupted"):
            self.submit()
        self.assertEqual(old_lock, (self.site/"pack.lock.json").read_bytes())
        self.assertNotIn("review_zones",deploy.read(self.policy)["bindings"])
        with patch.object(deploy,"urlopen",self.capabilities):
            recovered = self.submit()
        self.assertEqual(recovered["active_lock"],deploy.fingerprint(deploy.read(self.site/"pack.lock.json")))


if __name__ == "__main__":
    unittest.main()

