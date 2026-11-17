"""SDD-008 autonomous recovery with restart windows, explicit authority and measured evidence."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"client"))
import plugin_manager_watch as manager
import author_tool
import planning
from test_author_tool import blocked_task, speed_case


class ManagerRecovery(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        self.policy=self.root/"policy.json"
        self.registry=self.root/"registry"
        proposals=self.root/"private"
        (proposals/"decisions").mkdir(parents=True)
        self.args=("http://fixture", self.policy, proposals, self.registry, self.root/"state")
        self.s=manager.Supervisor(*self.args)
        self.addCleanup(lambda: self.s.close())
        self.source=blocked_task()
        self.source["events"][-1]["event_id"]="failure-1"
        payload=json.loads(self.source["events"][-1]["payload"])
        payload["trace"]["event_id"]="failure-1"
        self.source["events"][-1]["payload"]=json.dumps(payload)
        self.source["events_has_more"]=True
        trace,task=self.source["trace_id"],self.source["id"]
        receipt_id=manager.digest(author_tool.json_bytes(["archive",5]))
        tool="derived.task_"+manager.digest(author_tool.json_bytes([task,trace,"failure-1"]))[:32]
        self.definition={"schema_version":1,"id":tool,"description":self.source["text"],"field":"speed","unit":"km/h"}
        self.version=manager.capability(self.definition)["version"]
        receipt={"status":"proposed","source":{"task_id":task,"trace_id":trace,"event_id":"failure-1",
                 "archive_id":"archive","seq":5,"reason_code":"planner.capability_missing"},"definition":self.definition}
        raw=author_tool.json_bytes(receipt)
        self.receipt_path=proposals/"decisions"/(receipt_id+".json")
        self.receipt_path.write_bytes(raw)
        self.p={"source":"journal:plugin.author","module":"plugin.author","kind":"plugin.author.proposed","code":"author.proposed",
                "task_id":task,"trace_id":trace,"event_id":"proposal-6","parent_event_id":"failure-1",
                "data":{"archive_id":"archive","source_seq":5,"receipt_id":receipt_id,"receipt_sha256":manager.digest(raw),
                        "tool_id":tool,"candidate_version":self.version}}
        self.rule={"text":self.source["text"],"semantic":{"field":"speed","unit":"km/h"},
                   "cases":[speed_case(-.5,-1.8),speed_case(1,3.6)]}
        self.write_policy(self.rule)
        self.submits=0
        self.followup=None
        self.configure()
        self.feed()

    def write_policy(self,*rules):
        self.policy.write_bytes(author_tool.json_bytes({"schema_version":1,"rules":list(rules)}))

    def configure(self):
        self.s.sink.copy=Mock(return_value=False)
        self.s.api=self.api

    def feed(self,archive="archive",seq=8):
        with self.s.sink.db:
            self.s.sink.db.execute("INSERT INTO events VALUES(?,?,?)",(archive,seq,json.dumps(self.p)))

    def api(self,path,body=None):
        if body is not None:
            self.submits+=1
            if self.followup is None:
                self.followup={**body,"trace_id":self.source["trace_id"],"state":"RUNNING"}
            self.assertEqual(self.followup["trigger"],body["trigger"])
            return self.followup
        return self.source if self.source["id"] in path else self.followup

    def events(self,db=None):
        return [json.loads(r[0]) for r in (db or self.s.db).execute("SELECT event FROM module_bus_events ORDER BY seq")]

    def restart(self):
        self.s.close()
        self.s=manager.Supervisor(*self.args)
        self.configure()

    def resume(self):
        with self.s.db:
            self.s.db.execute("UPDATE supervisor_work SET due=0")
        self.s.poll()

    def succeed(self):
        task=self.followup
        op=task["id"]+"/step/1"
        e=manager.evaluate(self.definition,self.rule["cases"][0]["sample"])
        e["definition_version"]=self.version
        task.update(state="SUCCEEDED", steps=[{"status":"SUCCEEDED","tool":self.definition["id"],
                    "version":self.version,"operation_id":op,"evidence":e}], events_has_more=True,
                    events=[{"task_id":task["id"],"kind":"verified","operation_id":op,"event_id":"verified-42",
                             "payload":json.dumps({"trace":{"event_id":"verified-42","trace_id":self.source["trace_id"]}})}])

    def test_autonomous_version_pinned_followup_survives_restart_and_duplicate_archive_delivery(self):
        self.s.poll()
        self.assertEqual(self.submits,1)
        self.assertEqual(self.followup["trigger"]["read_only_tool"],{"id":self.definition["id"],"version":self.version})
        self.succeed()
        self.restart()
        self.feed("rebuilt",1)
        self.resume()
        facts=self.events()
        self.assertEqual([e["kind"] for e in facts],["plugin.supervisor."+k for k in
                         ("approved","published","submitted","verified")])
        final=facts[-1]
        self.assertEqual((final["parent_event_id"],final["data"]["value"]),("verified-42",-1.8))
        self.assertEqual(final["data"]["release_event_id"],facts[1]["data"]["release_event_id"])
        self.assertEqual(self.submits,1)
        self.assertNotIn(self.source["text"],json.dumps(facts,ensure_ascii=False))

    def test_process_dies_after_publication_before_receipt_checkpoint(self):
        record=self.s.record
        def fail(p,phase,*args,**kwargs):
            if phase=="published":
                raise SystemExit(42)
            return record(p,phase,*args,**kwargs)
        with patch.object(self.s,"record",side_effect=fail),self.assertRaises(SystemExit):
            self.s.poll()
        self.restart()
        self.resume()
        self.assertEqual(self.submits,1)
        with closing(sqlite3.connect(self.registry/".history"/"module-events.sqlite")) as db:
            self.assertEqual([e["kind"] for e in self.events(db)],["plugin.release.requested","plugin.release.applied"])

    def test_loss_after_remote_submit_reuses_the_original_remote_task(self):
        api=self.s.api
        def fail(path,body=None):
            result=api(path,body)
            if body:
                raise ConnectionError("lost acknowledgement")
            return result
        self.s.api=fail
        self.s.poll()
        original=self.followup
        self.assertEqual(self.events()[-1]["code"],"supervisor.dependency_unavailable")
        self.restart()
        self.resume()
        self.assertIs(self.followup,original)
        self.assertEqual(self.submits,2)
        self.assertEqual(self.events()[-1]["kind"],"plugin.supervisor.submitted")

    def test_failed_sensor_task_requires_intervention_without_reissuing_tool(self):
        self.s.poll()
        self.followup["state"]="RECOVERY_REQUIRED"
        self.resume()
        fact=self.events()[-1]
        self.assertEqual(fact["code"],"supervisor.followup_failed")
        self.assertIn("sensor",fact["data"]["action"])
        self.resume()
        self.assertEqual(self.submits,1)

    def test_forged_receipt_or_missing_consent_has_no_side_effect(self):
        self.receipt_path.write_text("modified private data")
        self.s.poll()
        self.assertFalse(self.registry.exists())
        self.assertEqual(self.events()[-1]["code"],"supervisor.receipt_invalid")
        self.assertEqual(self.submits,0)

    def test_exact_text_and_independent_samples_are_required(self):
        with self.assertRaisesRegex(manager.Review,"policy_denied"):
            manager.rule_for(self.policy,self.source["text"]+" again",self.definition)
        self.write_policy(dict(self.rule,semantic={"field":"x","unit":"km/h"}))
        with self.assertRaisesRegex(manager.Review,"semantic_mismatch"):
            manager.rule_for(self.policy,self.source["text"],self.definition)
        bad=[speed_case(-.5,999),speed_case(1,3.6)]
        self.write_policy(dict(self.rule,cases=bad))
        self.s.poll()
        self.assertEqual(self.events()[-1]["code"],"supervisor.cases_failed")
        self.assertFalse(self.registry.exists())

    def test_other_tool_or_wrong_terminal_operation_cannot_verify(self):
        self.s.poll()
        self.succeed()
        for field,value in (("version","other"),("operation_id","other"),("tool","inspect")):
            step=self.followup["steps"][0]
            with patch.dict(step,{field:value}),self.assertRaisesRegex(manager.Review,"receipt_mismatch"):
                manager.check_result(self.followup,self.definition,self.followup["id"],self.followup["trigger"])
        self.followup["events"][0]["operation_id"]="another-step"
        self.resume()
        self.assertEqual(self.events()[-1]["code"],"supervisor.receipt_mismatch")

    def test_pinned_planner_cannot_choose_motion_or_http_even_on_description_or_id_collision(self):
        cap=manager.capability(self.definition)
        pin={"id":cap["id"],"version":cap["version"]}
        caps={cap["id"]:cap,"inspect":dict(planning.BASE["inspect"],description=cap["description"])}
        with patch.object(planning,"planner_profile",side_effect=AssertionError("no general planner")):
            _,steps=planning.plan_read_only(self.source["text"],caps,pin)
            self.assertEqual([s["tool"] for s in steps],[cap["id"]])
            caps[cap["id"]]=dict(cap,endpoint="http://other-provider")
            with self.assertRaises(planning.Refused):
                planning.plan_read_only(self.source["text"],caps,pin)


if __name__=="__main__":
    unittest.main()
