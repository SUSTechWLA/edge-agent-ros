"""SDD-054 watcher consumes only causal, undispatched, user-originated native planner gaps."""
from argparse import Namespace
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from client import field_plugin_watch as watch
from agent import read_plugin_families
from perception.spatial_facts import authored_screen


class WatcherTest(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.db=sqlite3.connect(self.root/"worker.db")
        self.addCleanup(self.db.close)
        self.args=Namespace(state=self.root,site=self.root,registry=self.root,policy=self.root/"approved.json",
                            agent_url="http://localhost",ollama_url="http://localhost:11434",
                            model="fixture",model_timeout=10,poll_seconds=1,
                            family_profile=self.root/"families.json")
        watch.save(self.args.family_profile, {"schema_version":1, "families":{
            "depth_column_review":{"description":"Example read-only review", "provider_prefix":"field."}}})
        self.w=watch.Watch(self.args,self.db)
        self.feed=[]
        self.tasks={}
        self.posted=[]
        self.model_calls=[]
        self.tool="survey_zones"
        self.pin="sha256:"+"b"*64
        self.receipt={"plugin_id":"field."+self.tool,"tool_id":self.tool,"binding":self.pin,
                      "content_digest":"sha256:"+"e"*64,"event_id":"a"*32}
        self.program={"statistic":"p10","unknown_if":["valid_fraction",0.6,"lt"],
                      "near_if":["depth_m","distance_m","le"],"review_order":"nearest_first"}
        self.author_patch=patch.object(watch,"fetch",self.fetch)
        self.author_patch.start()
        self.addCleanup(self.author_patch.stop)

    def add(self,name,source="user"):
        trigger={"kind":source,"source":source,"id":name}
        trace={"trigger":trigger,"trace_id":name}
        events=[]
        for k,fields in (("planner.plugin_gap",{"family":"depth_column_review","module":"planner",
                           "provider":"ollama/fixture","layer":"planner"}),
                         ("blocked",{"module":"agent","layer":"planner"})):
            seq=len(self.feed)+1
            eid=f"{seq:032x}"
            payload={"trace":{**trace,**fields,"event_id":eid}}
            if k=="blocked":
                payload["diagnostic"]={"code":watch.CODE}
            ev={"seq":seq,"kind":k,"task_id":name,"event_id":eid,"payload":json.dumps(payload)}
            self.feed.append(ev)
            events.append(ev)
        self.tasks[name]={"id":name,"trace_id":name,"trigger":trigger,"text":"分析前方深度供人工复核",
                          "state":"BLOCKED","steps":[],"events":events}
        return self.tasks[name],events[-1]

    def fetch(self,url,data=None,timeout=12,size=0):
        parsed=urlsplit(url)
        query=parse_qs(parsed.query)
        if parsed.path.endswith("task-events"):
            after=int(query["after"][0])
            return watch.wire({"source_id":"agent:test","next_after":len(self.feed),"events_has_more":False,
                               "events":self.feed[after:]})
        if parsed.path.endswith("api/chat"):
            self.model_calls.append(json.loads(data))
            program=dict(self.program)
            for key in ("unknown_if","near_if"):
                program[key]=dict(zip(("left","right","op"),program[key]))
            result={"name":self.tool,"description":"检查视区","program":program}
            if len(self.model_calls)==1:
                result["program"]["review_order"]="farthest_first"
            return watch.wire({"model":"fixture","done":True,"done_reason":"stop","message":{"content":json.dumps(result)}})
        if data is not None:
            posted=json.loads(data)
            self.posted.append(posted)
            child={"id":posted["id"],"text":posted["text"],"trigger":posted["trigger"],"trace_id":posted["trigger"]["trace_id"],
                   "state":"SUCCEEDED","steps":[{"tool":self.tool,"binding":self.pin}]}
            self.tasks[child["id"]]=child
            return watch.wire(child)
        return watch.wire(self.tasks[query["id"][0]])

    def test_retired_default_family_denies_authoring_without_disabling_ordinary_planning(self):
        profile = ROOT / "profiles/field-read-families.json"
        with patch.dict(os.environ, {"EDGE_READ_PLUGIN_FAMILIES":str(profile)}):
            self.assertEqual([], read_plugin_families.catalog([]))
        with self.assertRaisesRegex(ValueError, "not approved"):
            watch.approved_family(profile, "depth_column_review")

    def test_head_filter_model_feedback_pinned_continuation_and_no_replay(self):
        self.add("old")
        self.w.tick()
        self.assertEqual([],self.model_calls)
        self.add("current")
        def publish(site,registry,policy,candidate,url,folder):
            self.assertEqual(self.program,candidate["program"])
            watch.save(folder/"published-plugin.json",self.receipt)
            watch.save(policy,{"schema_version":1,"bindings":{self.tool:self.pin}})
            return self.receipt
        family=replace(watch.FAMILIES["depth_column_review"],publish=publish)
        with patch.dict(watch.FAMILIES,{"depth_column_review":family}):
            self.w.tick()
            watch.Watch(self.args,self.db).tick()
        self.assertEqual(2,len(self.model_calls))
        self.assertNotIn(self.tasks["current"]["text"],str(self.model_calls))
        self.assertEqual(self.program,json.loads((self.root/"jobs/current/candidate-2.json").read_bytes())["program"])
        self.assertIsInstance(json.loads((self.root/"jobs/current/author-proposal-2.json").read_bytes())
                              ["program"]["unknown_if"],dict)
        feedback=json.loads(self.model_calls[1]["messages"][1]["content"])["previous_attempt"]
        self.assertEqual("independent_validation_failed",feedback["reason"])
        self.assertNotIn("expected",str(feedback))
        self.assertEqual(1,len(self.posted))
        trigger=self.posted[0]["trigger"]
        self.assertEqual((self.pin,"current"),(trigger["read_only_tool"]["binding"], trigger["parent_task_id"]))
        self.assertEqual("SUCCEEDED",json.loads((self.root/"jobs/current/followup.json").read_bytes())["state"])

    def test_typed_transport_preserves_model_operands_and_rejects_malformed_output(self):
        base=dict(self.program,unknown_if={"left":"valid_fraction","right":0.5,"op":"gt"},
                  near_if={"left":"distance_m","right":"depth_m","op":"lt"})
        draft=watch.lower_depth(base)
        self.assertEqual(["valid_fraction",0.5,"gt"],draft["unknown_if"])
        self.assertEqual(["distance_m","depth_m","lt"],draft["near_if"])
        with self.assertRaises(ValueError):
            authored_screen.qualify(draft)
        for expression in (["valid_fraction","le","lt"], {"left":"valid_fraction","right":"le","op":"lt"},
                           {"left":"valid_fraction","right":0.9,"op":"le"}):
            with self.assertRaises(ValueError):
                watch.lower_depth(dict(base,unknown_if=expression))

    def test_reject_changes_to_source_scope_and_model_retry_budget(self):
        task,event=self.add("suspect",source="agent")
        with self.assertRaisesRegex(ValueError,"user request"):
            watch.gap(task,event)
        task,event=self.add("altered")
        with self.assertRaisesRegex(ValueError,"source event"):
            watch.gap(task,{**event,"event_id":"unrelated"})
        actual=task["events"][0]
        data=json.loads(actual["payload"])
        data["trace"]["trace_id"]="other"
        actual["payload"]=json.dumps(data)
        with self.assertRaisesRegex(ValueError,"preceding"):
            watch.gap(task,event)
        folder=self.root/"budget"
        folder.mkdir()
        with patch.object(watch,"fetch",side_effect=TimeoutError("timeout")) as fail:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError,"exhausted"):
                    watch.authored(folder,task,"depth_column_review","http://localhost","fixture",10)
            self.assertEqual(2,fail.call_count)


if __name__=="__main__":
    unittest.main()
