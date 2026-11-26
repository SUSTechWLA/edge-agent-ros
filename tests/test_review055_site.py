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
from soft_runtime import SoftRuntimeError
from v2_registry import RegistryError, V2Registry


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

    def admin(self, action, plugin_id, expect, request):
        args = Namespace(action=action, id=plugin_id, expect=expect, request_id=request)
        return self.reg.change(args, self.site/"trust")

    def entry(self, plugin_id):
        return next(row for row in self.reg.entries() if row["descriptor"]["id"] == plugin_id)

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

    def test_removal_settles_original_step_and_preserves_shared_measure_service(self):
        caps = self.live()
        step, _ = self.plan(caps, "review_builtin")
        preview = {**step, "soft_pin":caps["review_builtin"]["_soft"]}
        call = self.loop.run_until_complete
        frozen = call(self.host.freeze_plan({"task":"before", "provider":"test", "steps":[dict(preview)]}))
        pin = frozen[1][0]["soft_pin"]
        primary = pin["plugin_id"]
        revision = self.entry(primary)["revision"]
        drain = self.admin("drain", primary, revision, "begin-retirement")
        self.assertEqual(drain, self.admin("drain", primary, revision, "begin-retirement"))
        self.assertNotIn("review_builtin", self.live())
        with self.assertRaises(SoftRuntimeError) as stale:
            call(self.host.freeze_plan({"task":"late", "provider":"test", "steps":[dict(preview)]}))
        self.assertEqual(stale.exception.code, "tool.binding_unavailable")
        self.assertIsNone(self.reg.saved_plan("late"))
        with self.assertRaises(RegistryError) as busy:
            self.admin("remove", primary, drain["revision"], "remove-too-early")
        self.assertEqual(busy.exception.code, "plugin.cleanup_incomplete")
        wire = {"arguments":step["args"], "operation_id":pin["reservation_id"], "version":step["version"]}
        with self.assertRaises(SoftRuntimeError) as modified:
            call(self.host.invoke(pin, {**wire, "arguments":{**step["args"], "distance_m":2.0}}))
        self.assertEqual(modified.exception.code, "tool.binding_unavailable")

        analysis = {"frame_ref":self.frame, "grid":[1,3], "channel":"depth", "unit":"m", "statistic":"p10",
                    "width":3, "height":1, "regions":[{"bounds_xyxy":[i,0,i+1,1], "pixels":1,
                    "valid_pixels":1, "value":depth} for i,depth in enumerate((0.7,2.0,0.5))]}
        with patch("soft_host.http_exchange", return_value=(200, analysis)) as measure:
            receipt = call(self.host.invoke(pin, wire))
            self.assertEqual(receipt["state"], "SUCCEEDED", receipt)
            self.assertEqual(receipt["evidence"]["review"]["review_order"], [3,1])
            self.assertEqual(call(self.host.invoke(pin, wire)), receipt)
            measure.assert_called_once()
        call(self.host.command({"action":"settle", "pin":pin}))
        self.live()
        self.assertTrue(self.entry(primary)["disposed"])
        removed = self.admin("remove", primary, drain["revision"], "finish-retirement")
        self.assertEqual(removed, self.admin("remove", primary, drain["revision"], "finish-retirement"))
        self.assertNotIn(primary, {row["descriptor"]["id"] for row in self.reg.entries()})

        self.host = Host(self.reg, self.site, self.journal, self.host.image_digest)
        live = self.live()
        self.assertEqual(set(live), {"review_composed","review_workflow"})
        self.assertEqual(live["review_composed"]["_soft"]["binding"], caps["review_composed"]["_soft"]["binding"])
        self.assertIn("review055.measure", live["review_composed"]["_soft"]["dependencies"])
        self.assertEqual(self.reg.saved_plan("before"), frozen)
        stored, = self.journal.db.execute("SELECT receipt FROM soft_operations WHERE operation=?",
                                          (pin["reservation_id"],)).fetchone()
        self.assertEqual(json.loads(stored), receipt)

    def test_last_review_removed_reports_empty_candidates_before_any_dispatch(self):
        caps = self.live()
        for cap in caps.values():
            key = cap["_soft"]["plugin_id"]
            change = self.admin("drain", key, self.entry(key)["revision"], "drain-" + key)
            self.live()
            self.admin("remove", key, change["revision"], "remove-" + key)
        self.assertEqual(self.live(), {})
        events = []
        model = Mock(return_value={"selected":"obstacle_review", "reason":"review nearby surfaces"})
        with self.assertRaises(Unavailable) as denied:
            planning.plan("请复核附近障碍候选", self.live(), lambda k,v:events.append((k,v)),
                          model_profile=self.model, model_request=model)
        self.assertEqual(denied.exception.code, "planner.implementation_unavailable")
        decision, = [v for k,v in events if k == "planner.implementation_selected"]
        self.assertIsNone(decision["tool"])
        self.assertEqual({row["tool"]:row["eligible"] for row in decision["candidates"]},
                         {name:False for name in caps})
        model.assert_called_once()
        self.assertFalse(self.reg.reservations())
        self.assertIsNone(self.journal.db.execute("SELECT 1 FROM soft_operations").fetchone())

    def test_graph_is_compiled_from_separate_model_reply_on_normal_planner_entry(self):
        caps = self.live()
        (self.cfg/"implementations.json").write_bytes((self.cfg/"review_composed.json").read_bytes())
        wanted = {"columns":3, "distance_m":1.2}
        graph = {"nodes":[{"id":"a","op":"measure_p10","input":"frame"},
                          {"id":"b","op":"classify_near","input":"a"},
                          {"id":"c","op":"rank_nearest","input":"b"}],"output":"c"}
        replies = [{"selected":"obstacle_review","reason":"human near-surface review"},
                   {"disposition":"ready","reason":"classify the regions",
                    "steps":[{"tool":"review_composed","args":wanted}]}, graph]
        request = Mock(side_effect=replies)
        events = []
        _, steps = planning.plan("以1.2米为界识别三列中优先复核的近距表面", caps,
                                 lambda k,v:events.append(k), model_profile=self.model, model_request=request)
        self.assertIn("planner.graph_composed",events)
        self.assertEqual(steps[0]["args"],{**wanted,"graph":graph})
        self.assertEqual(request.call_count,3)
        base, compiler = (call.args[1] for call in request.call_args_list[1:])
        self.assertNotIn("graph",base["capabilities"][0]["input_schema"]["properties"])
        self.assertEqual(compiler["arguments"],wanted)
        self.assertEqual(set(compiler["operations"]),{"measure_p10","measure_median","classify_near",
                                                     "rank_nearest","rank_image"})

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
