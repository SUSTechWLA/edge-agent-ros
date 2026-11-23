"""Exercise the native DSH pack, live admission, freeze, and failover without any ROS process."""
import asyncio
from argparse import Namespace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/"scripts"), str(ROOT/"agent")]
from agent import planning, read_capability_index
from edge_contracts.errors import Refused, Unavailable
from module_journal import ModuleJournal
from prepare_review_site import prepare, sha
from site_pack import verify
from soft_host import Host
from v2_registry import V2Registry


class ReviewSite(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.site, self.cfg = base/"site", base/"policy"
        self.frame = "sha256:" + "a"*64
        self.workflow = "sha256:" + "b"*64
        manifest = json.dumps({"schemaVersion":2, "layers":[], "config":{"digest":"sha256:"+"c"*64}}).encode()
        self.pack = prepare(self.site, self.cfg, manifest, sha(manifest), "a"*40, self.frame, self.workflow)
        _, self.items = verify(self.site, self.pack["lock"])
        self.reg = V2Registry(base/"registry")
        self.addCleanup(self.reg.close)
        self.reg.install_pack(list(self.items.values()), self.pack["lock"])
        self.journal = ModuleJournal(base/"host.sqlite")
        self.addCleanup(self.journal.close)
        self.host = Host(self.reg, self.site, self.journal, sha(manifest))
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.env = {"EDGE_READ_CAPABILITY_INDEX":str(self.cfg/"implementations.json"),
                    "EDGE_AUTONOMOUS_READ_POLICY":str(self.cfg/"approved.json"),
                    "EDGE_CONTRACT_DIR":str(base/"absent")}
        self.use = patch.dict(os.environ, self.env)
        self.use.start()
        self.addCleanup(self.use.stop)
        self.healthy = patch("soft_host.http_exchange", return_value=(200, {"frame_ref":self.frame,
                                                                            "workflow_digest":self.workflow}))
        self.healthy.start()
        self.addCleanup(self.healthy.stop)
        self.model = {"protocol":"http-planner","version":"test","url":"http://localhost/model"}

    def live(self):
        async def synced():
            await self.host.reconcile()
            return {cap["id"]:cap for cap in self.host.catalog()["tools"]}
        return self.loop.run_until_complete(synced())

    def plan(self, caps, expected):
        proposal = {"disposition":"ready","reason":"typed review","steps":[{"tool":expected,
                    "args":{"columns":3,"distance_m":1.2}}]}
        model = Mock(side_effect=[{"selected":"obstacle_review","reason":"needs ranked near surface review"},proposal])
        events = []
        _, steps = planning.plan("请复核一米二内三个区域的障碍候选", caps, lambda k,v:events.append((k,v)),
                                 model_profile=self.model, model_request=model)
        selection, = [v for k,v in events if k == "planner.implementation_selected"]
        return steps[0], selection

    def test_native_drain_preserves_old_pin_and_routes_new_request(self):
        caps = self.live()
        self.assertEqual(set(caps), set(self.pack["tools"]))
        self.assertEqual({k:v["_soft"]["binding"] for k,v in caps.items()}, self.pack["tools"])
        first, event = self.plan(caps, "review_builtin")
        first["soft_pin"] = caps[first["tool"]]["_soft"]
        result = self.loop.run_until_complete(self.host.freeze_plan({"task":"first","provider":"test","steps":[first]}))
        pin = result[1][0]["soft_pin"]
        self.assertIn("review055.measure", pin["dependencies"])
        first_revision = next(e["revision"] for e in self.reg.entries() if e["descriptor"]["id"]=="review055.builtin")
        self.reg.change(Namespace(action="drain",id="review055.builtin",expect=first_revision,
                                  request_id="disable-primary"), self.site/"trust")
        new_caps = self.live()
        self.assertNotIn("review_builtin", new_caps)
        second, fallback = self.plan(new_caps, "review_workflow")
        self.assertEqual(event["index_digest"],fallback["index_digest"])
        self.assertEqual([r["eligible"] for r in fallback["candidates"]], [False,True,True])
        self.assertNotEqual(first["binding"],second["binding"])
        self.assertEqual(self.reg.saved_plan("first")[1][0]["soft_pin"], pin)
        self.assertEqual(next(e["frozen"] for e in self.reg.entries() if e["descriptor"]["id"]=="review055.builtin"),1)

    def test_no_matching_provider_or_binding_never_dispatches(self):
        caps = self.live()
        for cap in caps.values():
            cap["_soft"] = {**cap["_soft"],"plugin_id":"untrusted.same_export"}
        with self.assertRaises(Unavailable) as denied:
            self.plan(caps,"review_builtin")
        self.assertEqual(denied.exception.code,"planner.implementation_unavailable")

    def test_corrupt_index_rejected_and_graph_language_is_station_owned(self):
        caps = self.live()
        (self.cfg/"implementations.json").write_bytes((self.cfg/"review_composed.json").read_bytes())
        chosen = read_capability_index.catalog(caps)[0]["preferred"]
        full = caps["review_composed"]["_parameter_schema"]
        language = read_capability_index.task_input_schema(full,chosen)
        self.assertEqual(set(language["properties"]),{"columns","distance_m"})
        self.assertIn("graph",full["required"])
        doc = json.loads((self.cfg/"implementations.json").read_bytes())
        rows = doc["contracts"]["obstacle_review"]["implementations"]
        rows.append(rows[0])
        (self.cfg/"implementations.json").write_text(json.dumps(doc))
        with self.assertRaises(Refused):
            read_capability_index.catalog(caps)


if __name__ == "__main__":
    unittest.main()
