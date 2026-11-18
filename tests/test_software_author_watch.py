"""SDD-019. Verify source binding, restricted generation, crash replay and release handoff."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"client"))
from test_plugin_author_watch import Fixture
import design_contracts
import plugin_design as design
import software_author_watch as code
import software_provider as provider


class Interrupted(BaseException):
    pass


class TestCodeWatch(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.source=Fixture()
        self.addCleanup(self.source.close)
        self.contract=self.root/"contract.json"
        self.contract.write_bytes((ROOT/"contracts/design/text-transform.json").read_bytes())
        self.drafts=self.root/"drafts"
        self.drafts.mkdir()
        self.watch=None
        self.addCleanup(lambda:self.watch and self.watch.close())
        self.fields={"description":"统计单词","prefix":"数词","expression":"text.casefold().split().count('robot')"}
        self.open()

    def open(self,attempts=1):
        if self.watch:
            self.watch.close()
        self.watch=code.SoftwareAuthor(self.source.url,self.drafts,self.root/"private",
                                      self.root/"public/plugin.software_author/module-events.sqlite",
                                      self.source.url,"fixture-model",self.contract,attempts=attempts,retry_delay=0)

    def add(self,task,interface="text-transform",unresolved=None):
        ident=design.sha(task.encode())
        interfaces={"text-transform":design_contracts.read_contract(self.contract)}
        model={"proposal":{"interface":interface,"behavior":"count a word"},"unresolved_fields":unresolved or []}
        item={"design_id":ident,"status":"draft","source":{"trace_id":task,"task_id":task},
              "model_design":model,"interfaces":interfaces,"goal":"private text goal",
              "contracts_sha256":design_contracts.fingerprint(interfaces)}
        raw=design.json_bytes(item)
        path=self.drafts/(ident+".json")
        path.write_bytes(raw)
        row=dict(code.SoftwareAuthor.selector,seq=len(self.source.events)+1,event_id="draft-"+task,
                 parent_event_id="req-"+task,task_id=task,trace_id=task,data={"design_id":ident,
                 "draft_sha256":design.sha(raw),"contracts_sha256":item["contracts_sha256"]})
        self.source.events.append(row)
        return row,path

    def drain(self):
        for _ in range(40):
            if not self.watch.poll()[0]:
                return
        self.fail("queue did not drain")

    def events(self,stage):
        return [json.loads(r[0]) for r in self.watch.journal.db.execute(
            "SELECT event FROM module_bus_events WHERE json_extract(event,'$.kind')=? ORDER BY seq",
            ("plugin.software_author."+stage,))]

    def answer(self,fields=None):
        return json.dumps({"done":True,"message":{"content":json.dumps(fields or self.fields)}}).encode()

    def test_new_model_tool_and_verified_handoff_survive_replay(self):
        row,path=self.add("count")
        with patch.object(code,"http_bytes",return_value=self.answer()) as model:
            self.drain()
            self.open()
            page=self.watch.sink.page
            with patch.object(self.watch.sink,"page",side_effect=lambda a,n:dict(page(a,n),archive_id="rebuilt")):
                self.drain()
        self.assertEqual(model.call_count,1)
        prompt=json.loads(model.call_args.args[1])
        self.assertEqual(set(prompt["format"]["required"]),{"description","prefix","expression"})
        self.assertEqual(prompt["options"]["num_predict"],384)
        self.assertEqual({key:(spec["minLength"],spec["maxLength"]) for key,spec in prompt["format"]["properties"].items()},
                         {"description":(1,160),"prefix":(1,160),"expression":(1,512)})
        authored,=self.events("authored")
        completed,=self.events("completed")
        self.assertEqual(completed["parent_event_id"],authored["event_id"])
        self.assertEqual(self.events("model_requested")[0]["parent_event_id"],row["event_id"])
        folder=self.root/"private/packages"/path.stem
        source,identity,version=provider.package(folder)
        self.assertEqual(version,authored["data"]["provider_version"])
        self.assertEqual(identity["contract"]["id"],"software.t_"+path.stem[:32])
        self.assertEqual(json.loads((folder/"design-event.json").read_bytes()),row)
        self.assertEqual(provider.evaluate(provider.program(source),"Robot robotics ROBOT"),2)
        self.assertNotIn("private text goal",str(self.watch.journal.db.execute("SELECT event FROM module_bus_events").fetchall()))
        self.assertFalse(self.events("intervention_required"))

    def test_one_bad_row_does_not_send_stale_or_other_contracts_to_the_model(self):
        self.add("unsupported","Service")
        self.add("unresolved",unresolved=["outputs"])
        self.add("broken")[1].write_bytes(b"{}")
        changed=self.add("uninstalled")[1]
        draft=json.loads(changed.read_bytes())
        draft["interfaces"]["text-transform"]["owner"]="other"
        draft["contracts_sha256"]=design_contracts.fingerprint(draft["interfaces"])
        raw=design.json_bytes(draft)
        changed.write_bytes(raw)
        self.source.events[-1]["data"].update(draft_sha256=design.sha(raw),contracts_sha256=draft["contracts_sha256"])
        self.add("ok")
        with patch.object(code,"http_bytes",return_value=self.answer()) as model:
            self.drain()
        self.assertEqual(model.call_count,1)
        self.assertEqual(self.events("skipped")[0]["data"]["reason"],"other_interface")
        self.assertEqual({e["data"]["reason"] for e in self.events("intervention_required")},
                         {"design_incomplete","design_digest_mismatch","contract_unapproved"})
        self.assertEqual([e["task_id"] for e in self.events("authored")],["ok"])

    def test_invalid_expression_and_timeout_have_a_bounded_budget(self):
        self.open(attempts=2)
        self.add("invalid")
        invalid=dict(self.fields,expression="text.capitalize()")
        with patch.object(code,"http_bytes",side_effect=[TimeoutError(),self.answer(invalid)]) as model:
            self.drain()
        self.assertEqual(model.call_count,2)
        self.assertEqual(self.events("retry")[0]["data"]["reason"],"model_unavailable")
        self.assertEqual(self.events("intervention_required")[0]["data"]["reason"],"model_reply_invalid")
        self.assertEqual(self.events("candidate_rejected")[0]["data"]["reason"],"software.expression")
        self.assertFalse(self.events("authored"))
        self.assertEqual(list((self.root/"private/packages").iterdir()),[])

    def test_output_limits_charge_attempts_across_restart_and_preserve_following_work(self):
        self.open(attempts=2)
        self.add("long")
        done=json.loads(self.answer())
        limited=design.json_bytes(dict(done,done_reason="length",eval_count=384))
        with patch.object(code,"http_bytes",return_value=limited):
            self.watch.poll()
        self.assertEqual(self.events("retry")[0]["data"]["reason"],"model_output_limit")
        self.open(attempts=8)
        over=design.json_bytes(dict(done,done_reason="stop",eval_count=2901))
        with patch.object(code,"http_bytes",side_effect=[over,self.answer()]) as model:
            self.drain()
            self.add("healthy")
            self.drain()
        self.assertEqual(model.call_count,2)
        final,=self.events("intervention_required")
        self.assertEqual((final["data"]["reason"],final["data"]["budget"],final["data"]["attempt"]),
                         ("model_output_limit",2,2))
        self.assertEqual([e["data"]["eval_count"] for e in self.events("model_replied")[:2]],[384,2901])
        self.assertEqual(self.events("model_requested")[0]["data"]["num_predict"],384)
        self.assertEqual(self.events("model_replied")[0]["data"]["done_reason"],"length")
        self.assertEqual([e["task_id"] for e in self.events("authored")],["healthy"])
        self.assertFalse((self.root/"private/packages"/design.sha(b"long")).exists())

    def test_schema_field_lengths_are_enforced_for_unconstrained_endpoints(self):
        for key,limit in code.FIELD_LIMITS.items():
            self.add(key)
            value=dict(self.fields)
            value[key]="a"*(limit+1)
            with patch.object(code,"http_bytes",return_value=self.answer(value)):
                self.drain()
        self.assertEqual(len(self.events("intervention_required")),3)
        self.assertTrue(all(e["data"]["reason"]=="model_reply_invalid" for e in self.events("intervention_required")))
        self.assertFalse(self.events("prepared"))

    def test_reconcile_prepared_result_and_lost_ack_without_model_on_last_attempt(self):
        self.add("prepared")
        with patch.object(code,"http_bytes",return_value=self.answer()) as first, \
                patch.object(code.os,"rename",side_effect=Interrupted),self.assertRaises(Interrupted):
            self.watch.poll()
        self.assertEqual(first.call_count,1)
        self.open()
        with patch.object(code,"http_bytes",side_effect=AssertionError):
            self.drain()
        self.add("lost-ack")
        original=self.watch.emit
        def lose_ack(stage,*args,**kwargs):
            if stage=="completed":
                raise Interrupted()
            return original(stage,*args,**kwargs)
        with patch.object(code,"http_bytes",return_value=self.answer()), \
                patch.object(self.watch,"emit",side_effect=lose_ack),self.assertRaises(Interrupted):
            self.watch.poll()
        self.open()
        with patch.object(code,"http_bytes",side_effect=AssertionError):
            self.drain()
        self.assertEqual([e["task_id"] for e in self.events("authored")],["prepared","lost-ack"])
        self.assertEqual(len(self.events("completed")),2)
        self.assertEqual(self.watch.db.execute("SELECT count(*) FROM software_prepared").fetchone()[0],0)


if __name__=="__main__":
    unittest.main()
