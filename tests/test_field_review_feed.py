"""The field Console may attribute authoring, reuse and sensor results only to recorded evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from client import field_review_feed as feed


def sha(char):
    return "sha256:"+char*64


def utc(second):
    return f"2026-11-23T01:00:{second:02d}+00:00"


def save(path,obj):
    path.parent.mkdir(exist_ok=True,parents=True)
    path.write_text(json.dumps(obj),encoding="utf8")


def row(task,kind,second,operation=None,**fields):
    eid=task["id"]+kind
    trace={"trace_id":task["trace_id"],"event_id":eid,"trigger":task["trigger"],**fields}
    return {"task_id":task["id"],"kind":kind,"utc":utc(second),"operation_id":operation,"event_id":eid,
            "payload":json.dumps({"trace":trace})}


def source(key,second,time_ns):
    task={"id":key,"state":"SUCCEEDED","created":utc(second),"updated":utc(second+4),"text":"测量相机有效深度",
          "trace_id":key,"trigger":{"kind":"user","source":"user","id":key}}
    operation=key+"/step/1"
    task["events"]=[row(task,"planner.family_selected",second+1,selected="depth_column_review",
                        provider="ollama",reason="compare pixels",artifact={"sha256":"a"*64,"content":"private-prompt"}),
                    row(task,"verified",second+2,operation)]
    task["steps"]=[{"tool":"screen","args":{"columns":1,"distance_m":1.3},"status":"SUCCEEDED","operation_id":operation,
                    "binding":sha("a"),"soft_pin":{"name":"screen","plugin_id":"field.screen","binding":sha("a"),
                    "version":"1.0.0","content_digest":sha("b")},"evidence":{
                        "analysis":{"clock_id":"unix","depth_source_id":"d435.depth","depth_ref":sha("b"),
                                    "depth_stamp_ns":str(time_ns),"grid":[1,1],"private_source":"unprojected",
                                    "regions":[{"bounds_xyxy":[0,0,2,2],"value":1.1,"pixels":4,"valid_pixels":4}]},
                        "review":{"program_sha256":sha("d"),"column_count":1,"review_order":[1],"unknown_columns":[],
                                  "regions":[{"column":1,"state":"near_surface","depth_m":1.1}]}}}]
    return task


class TestFieldFeed(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.opt=SimpleNamespace(selection=root/"selection.json",agent="unused",rgb=root/"rgb",depth=root/"depth",
                                 author_state=root/"author",out=root/"out")
        self.opt.out.mkdir()
        save(self.opt.selection,{"task_ids":["first","second"]})
        save(self.opt.author_state/"ready.json",{"utc":utc(0),"source_id":"agent:site"})
        self.tasks={k:source(k,s,1795159000000000000+s) for k,s in (("first",5),("second",18))}

    def view(self):
        with patch.object(feed,"get",side_effect=lambda _,path,id,limit:self.tasks[id]):
            return feed.snapshot(self.opt)

    def test_real_timestamps_receipts_and_author_audit(self):
        job=self.opt.author_state/"jobs"/"first"
        job.mkdir(parents=True)
        rows=[{"phase":"author_requested","attempt":1,"utc":utc(6)},{"phase":"author_replied","utc":utc(7)},
              {"phase":"candidate_verified","utc":utc(8),"case_count":4}]
        for record in rows:
            record.update(task_id="first",trace_id="first",local_reason="unprojected",password="secret-text")
        (job/"events.jsonl").write_text("\n".join(json.dumps(r) for r in rows),encoding="utf8")
        self.tasks["first"]["text"]+=" password=secret-text"
        result=self.view()
        fst,snd=result["runs"]
        self.assertEqual((fst["author_calls"],fst["authored_utc"],snd["author_calls"]),(1,utc(7),0))
        self.assertEqual(snd["reuse"]["to_depth_stamp_ns"],"1795159000000000018")
        self.assertEqual(snd["reuse"]["from_operation_id"],"first/step/1")
        self.assertEqual(snd["reuse"]["to_receipt_event_id"],"secondverified")
        family,= [e for e in result["events"] if e["phase"]=="planner.family_selected" and e["task_id"]=="first"]
        self.assertEqual(family["source"]["artifact_sha256"],"a"*64)
        self.assertEqual(family["utc"],utc(6))
        reused,=[e for e in result["events"] if e["phase"]=="plugin.reused"]
        self.assertEqual(reused["utc"],utc(20))
        for prohibited in ("secret-text","private-prompt","unprojected"):
            self.assertNotIn(prohibited,json.dumps(result))

    def test_all_invalid_or_older_frame_or_different_binding_cannot_prove_reuse(self):
        first,second=self.tasks.values()
        step=second["steps"][0]
        analysis=step["evidence"]["analysis"]
        with self.subTest("invalid depth"):
            analysis["regions"][0]["valid_pixels"]=0
            result=self.view()["runs"][1]
            self.assertNotIn("reuse",result)
            self.assertEqual(result["measurement_status"],"unknown")
            self.assertEqual(result["review"]["unknown_columns"],[1])
            self.assertEqual(result["review"]["regions"][0]["state"],"unknown")
            self.assertEqual(result["review"]["review_order"],[])
            self.assertIsNone(result["analysis"]["regions"][0]["value"])
        analysis["regions"][0]["valid_pixels"]=4
        with self.subTest("identical sensor stamp"):
            analysis["depth_stamp_ns"]=first["steps"][0]["evidence"]["analysis"]["depth_stamp_ns"]
            self.assertNotIn("reuse",self.view()["runs"][1])
        with self.subTest("mismatched pin"):
            analysis["depth_stamp_ns"]="1795159000000000029"
            step["soft_pin"]["binding"]=sha("c")
            self.assertNotIn("reuse",self.view()["runs"][1])

    def test_maintenance_is_scoped_and_redacted_and_requires_source(self):
        source={"container_id":"2a","started_at":utc(9),"password":"hidden"}
        records=[{"task_id":"first","utc":utc(9),"detail":"人工修复作者服务目录权限","source":source},
                 {"task_id":"third","utc":utc(9),"detail":"outside","source":source},
                 {"task_id":"first","utc":utc(9),"detail":"unsourced","source":{"private":"hidden"}}]
        save(self.opt.selection.with_name("maintenance.json"),records)
        events=[e for e in self.view()["events"] if e["phase"]=="operator.maintenance"]
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]["source"],{"container_id":"2a","started_at":utc(9)})

    def test_untrusted_trace_or_success_state_alone_cannot_prove_reuse(self):
        self.tasks["second"]["events"][1]["event_id"]="different"
        self.assertNotIn("reuse",self.view()["runs"][1])
        self.tasks["second"]["events"][0]["task_id"]="first"
        self.assertNotIn("family_selections",self.view()["runs"][1])

    def test_publication_continuation_must_match_immutable_parent_and_pin(self):
        parent=self.tasks["first"]
        parent.update(state="BLOCKED",steps=[],events=parent["events"][:1])
        child=source("child",8,1795159000000000008)
        job=self.opt.author_state/"jobs"/"first"
        release={"tool_id":"screen","plugin_id":"field.screen","binding":sha("a"),"event_id":"site-release"}
        save(job/"published-plugin.json",release)
        child.update(trace_id="first",trigger={"kind":"agent","source":"plugin.supervisor","id":"child",
                         "parent_task_id":"first","trace_id":"first","parent_event_id":"site-release",
                         "read_only_tool":{"id":"screen","binding":sha("a")}})
        child["events"]=[row(child,"verified",11,"child/step/1")]
        self.tasks["child"]=child
        save(job/"followup-post.json",{"id":"child","trigger":child["trigger"]})
        first,second=self.view()["runs"]
        self.assertEqual(first["original_state"],"BLOCKED")
        self.assertEqual(second["reuse"]["from_ledger_task_id"],"child")
        child["trigger"]["parent_event_id"]="other-release"
        first,second=self.view()["runs"]
        self.assertNotIn("continuation",first)
        self.assertNotIn("reuse",second)

    def test_checks_existing_png_against_exact_reference(self):
        content=b"\x89PNG\r\n\x1a\ncontents"
        h=hashlib.sha256(content).hexdigest()
        dst=self.opt.out/(h+".png")
        dst.write_bytes(content)
        self.assertEqual(feed.bound_image(self.opt.rgb,self.opt.out,"sha256:"+h),dst.name)
        dst.write_bytes(content+b"changed")
        with self.assertRaises(ValueError):
            feed.bound_image(self.opt.rgb,self.opt.out,"sha256:"+h)


if __name__=="__main__":
    unittest.main()
