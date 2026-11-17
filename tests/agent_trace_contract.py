"""Run with the ROS application image. No model, network or moving robot is needed."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"agent"))
import planning
from service import Journal, Robot, event_diagnosis, worker
from tracing import artifact, contract, encoded


class TraceContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=self.root/"contracts"
        self.plugins=self.root/"plugins"
        self.extra=self.root/"extra"
        for d in (self.config,self.plugins,self.extra):
            d.mkdir()
        env=patch.dict(os.environ,{"EDGE_CONTRACT_DIR":str(self.config),"EDGE_PLUGIN_DIR":str(self.plugins),
            "EDGE_EXTRA_PLUGIN_DIR":"","EDGE_PLANNER_URL":"","EDGE_OLLAMA_URL":"http://model-a",
            "EDGE_OLLAMA_MODEL":"model-a","EDGE_TRACE_CONTENT":"redacted"})
        env.start()
        self.addCleanup(env.stop)

    def journal(self):
        db=Journal(self.root/"db")
        self.addCleanup(db.owner.close)
        self.addCleanup(db.db.close)
        return db

    def test_legacy_rows_and_trigger_idempotence(self):
        d=self.root/"db"
        d.mkdir()
        with sqlite3.connect(d/"tasks.db") as old:
            old.execute("""CREATE TABLE events(seq INTEGER PRIMARY KEY,utc TEXT,task_id TEXT,step INTEGER,
                       kind TEXT,detail TEXT,operation_id TEXT)""")
            old.execute("""CREATE TABLE tasks(id TEXT PRIMARY KEY,text TEXT,state TEXT,detail TEXT,provider TEXT,
                       steps TEXT,cursor INTEGER,retries INTEGER,due REAL,created TEXT,updated TEXT)""")
            old.execute("INSERT INTO tasks VALUES('old','status','SUCCEEDED','ok','local','[]',1,0,0,'then','then')")
            old.execute("INSERT INTO events VALUES(1,'then','old',0,'verified','ok','old/step/1')")
        j=self.journal()
        self.assertIsNone(j.get("old")["trigger"])
        self.assertIsNone(j.event_page("old")[0][0]["event_id"])
        one=j.create("owner","status")
        parent=j.event_page("owner")[0][0]["event_id"]
        trigger={"kind":"watchdog","id":"incident-01","source":"power","parent_event_id":parent}
        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks=list(pool.map(lambda _:j.create(None,"status",trigger),range(12)))
        key=tasks[0]["id"]
        self.assertEqual({t["id"] for t in tasks},{key})
        self.assertEqual(tasks[0]["trace_id"],one["trace_id"])
        self.assertEqual(len(j.event_page(key)[0]),1)
        with self.assertRaises(planning.Refused):
            j.create(None,"other",trigger)
        with self.assertRaises(planning.Refused):
            j.create("different-id","status",trigger)
        j.db.close()
        j.owner.close()
        recovered=self.journal()
        self.assertEqual(recovered.create(None,"status",trigger)["id"],key)
        self.assertEqual(json.loads(recovered.event_page(key)[0][0]["payload"])["trace"]["parent_event_id"],parent)

    def test_default_trigger_cannot_rebind_an_explicit_source_event(self):
        j=self.journal()
        j.create("task-one","status",{"kind":"user","source":"user","id":"same-trigger"})
        with self.assertRaises(planning.Refused):
            j.create("same-trigger","status")
        self.assertIsNone(j.get("same-trigger"))
        self.assertEqual(j.get("task-one")["text"],"status")

    def test_missing_legacy_binding_cannot_dispatch_a_replacement(self):
        journal = self.journal()
        cap = {"id":"sample", "version":"1", "description":"sample", "effect":"idempotent",
               "parameters":{}, "endpoint":"http://replacement/sample"}
        (self.plugins/"sample.json").write_text(json.dumps(cap), encoding="utf-8")
        robot = Mock(spec=Robot)
        robot.run.return_value = {"sample_id":"new-sample"}
        task = journal.create("legacy-sample", "sample")
        op = "legacy-sample/step/1"
        task["steps"] = [{"tool":"sample", "version":"1", "args":{}, "status":"RUNNING", "operation_id":op}]
        journal.save(task, "WAITING", "original provider was awaiting a receipt")

        with patch("service.rclpy.ok", side_effect=[True,False]):
            worker(journal, robot, False)
        robot.run.assert_not_called()
        held = journal.get(task["id"])
        self.assertEqual(held["state"], "RECOVERY_REQUIRED")
        self.assertFalse(held["can_retry"])
        error = journal.event_page(task["id"])[0][-1]
        self.assertEqual(error["operation_id"], op)
        self.assertEqual(json.loads(error["payload"])["diagnostic"]["code"], "tool.binding_unverified")

        # A newly frozen plan can use the registered provider with its own stable operation ID.
        fresh = journal.create("new-sample", "sample")
        fresh["provider"], fresh["steps"] = planning.plan(fresh["text"], planning.registry())
        journal.save(fresh, "RUNNING", "new binding")
        with patch("service.rclpy.ok", side_effect=[True,False]):
            worker(journal, robot, False)
        self.assertEqual(journal.get(fresh["id"])["state"], "SUCCEEDED")
        self.assertEqual(robot.run.call_args.args[1], "new-sample/step/1")

    def test_legacy_builtins_only_use_the_native_implementation(self):
        journal = self.journal()
        for name, args in (("status", {}), ("inspect", {"points":[[0,0]]})):
            native = planning.BASE[name]
            path = self.plugins/(name+".json")
            for override in (False, True):
                with self.subTest(tool=name, override=override):
                    if override:
                        path.write_text(json.dumps(dict(native, endpoint="http://replacement/run")))
                    task = journal.create(f"legacy-{name}-{override}", name)
                    task["steps"] = [{"tool":name, "version":native["version"], "args":args, "status":"PENDING"}]
                    journal.save(task, "RUNNING", "plan recorded before bindings")
                    robot = Mock(spec=Robot)
                    robot.run.return_value = {"verified":True}
                    with patch.dict(os.environ, {"EDGE_RETRY_LIMIT":"0"}), patch("service.rclpy.ok", side_effect=[True,False]):
                        worker(journal, robot, False)
                    if override:
                        robot.run.assert_not_called()
                        self.assertEqual(journal.get(task["id"])["state"], "RECOVERY_REQUIRED")
                        self.assertTrue(journal.get(task["id"])["can_retry"])
                    else:
                        self.assertEqual(robot.run.call_args.args[2], native)
                        self.assertEqual(journal.get(task["id"])["state"], "SUCCEEDED")
            path.unlink()

    def test_invalid_legacy_measurement_requires_reconciliation_once(self):
        j=self.journal()
        t=j.create("legacy-reading","fixture")
        t["steps"]=[{"tool":"fixture","args":{},"operation_id":"legacy-reading/step/1",
                     "status":"SUCCEEDED","evidence":{"reading":float("nan")}}]
        t["cursor"]=1
        j.save(t,"SUCCEEDED","historical confirmation",kind="verified",op="legacy-reading/step/1")
        event_id=j.event_page(t["id"])[0][-1]["event_id"]
        j.db.execute("PRAGMA user_version=0")
        j.db.commit()
        j.db.close()
        j.owner.close()
        for _ in range(2):
            repaired=self.journal()
            row=repaired.get(t["id"])
            self.assertEqual(row["state"],"RECOVERY_REQUIRED")
            self.assertFalse(row["can_retry"])
            self.assertEqual(row["steps"][0]["evidence"]["reading"],{"invalid_number":"NaN"})
            json.dumps(row,allow_nan=False)
            events=repaired.event_page(t["id"])[0]
            self.assertEqual([e["kind"] for e in events],["accepted","verified","evidence_corrected"])
            self.assertEqual(events[1]["event_id"],event_id)
            self.assertEqual(json.loads(events[-1]["payload"])["prior_state"],"SUCCEEDED")
            repaired.db.close()
            repaired.owner.close()

    def test_request_and_receipt_keep_actual_configuration_and_sanitized_content(self):
        j=self.journal()
        t=j.create("sensitive","询问设备位置 api_key=abcdefg12345")
        overrides={"schema_version":1,"prompts":{"planner.system":{"version":"v2","text":"Custom system prompt"}}}
        (self.config/"planner.json").write_text(json.dumps(overrides))
        trace=lambda k,v:j.exchange_event(t,k,v,None)
        answer={"message":{"content":json.dumps({"steps":[{"tool":"status","args":{}}],"reason":"ok"})}}
        with patch.object(planning,"http_json",return_value=answer) as io:
            planning.plan(t["text"],planning.registry(),trace)
        actual=io.call_args.args[1]
        self.assertEqual(actual["messages"][0]["content"],"Custom system prompt")
        rows=j.event_page("sensitive")[0]
        self.assertEqual([r["kind"] for r in rows],["accepted","planner.context","model.request","model.response"])
        envelopes=[json.loads(r["payload"])["trace"] for r in rows]
        _,context,req,reply=envelopes
        self.assertEqual(context["artifact"]["content"][0]["id"],"inspect")
        self.assertEqual(req["prompt"]["version"],"v2")
        self.assertEqual(req["artifact"]["sha256"],hashlib.sha256(encoded(actual)).hexdigest())
        self.assertTrue(req["artifact"]["content"]["format"]["properties"])
        self.assertEqual(reply["call_id"],req["call_id"])
        self.assertEqual(reply["parent_event_id"],req["event_id"])
        self.assertEqual(req["trigger"],t["trigger"])
        self.assertNotIn("abcdefg12345",json.dumps(rows))
        self.assertEqual(j.event_page("sensitive",after=rows[0]["seq"],limit=1)[1],True)
        (self.config/"planner.json").unlink()
        self.assertEqual(contract("prompts","planner.system")["version"],"1")
        self.assertEqual(req["artifact"]["content"]["messages"][0]["content"],"Custom system prompt")

    def test_model_profile_pins_call_and_hot_switches(self):
        profile=self.config/"planner-profile.json"
        profile.write_text(json.dumps({"schema_version":1,"version":"first","protocol":"ollama","url":"http://v1",
                                      "model":"model-b","options":{"temperature":0.3,"num_ctx":2048}}))
        proposal={"steps":[{"tool":"status","args":{}}],"reason":"ok"}
        events=[]
        def invoke(url, body, timeout):
            profile.write_text(json.dumps({"schema_version":1,"version":"second","protocol":"http-planner","url":"http://v2"}))
            self.assertEqual((url,body["model"],body["options"]["num_ctx"]),("http://v1/api/chat","model-b",2048))
            return {"message":{"content":json.dumps(proposal)}}
        trace=lambda kind,v:events.append((kind,v))
        with patch.object(planning,"http_json",side_effect=invoke):
            self.assertEqual(planning.plan("机器人朝向哪边",planning.registry(),trace)[0],"ollama/model-b")
        self.assertEqual([e["profile"]["version"] for _,e in events if "profile" in e],["first","first"])
        with patch.object(planning,"http_json",return_value=proposal) as io:
            self.assertEqual(planning.plan("机器人朝向哪边",planning.registry(),trace)[0],"http-planner")
            self.assertEqual(io.call_args.args[0],"http://v2")
        self.assertEqual(events[-1][1]["profile"]["version"],"second")
        profile.write_text("{")
        with patch.object(planning,"http_json") as io,self.assertRaises(planning.Refused) as caught:
            planning.plan("机器人朝向哪边",planning.registry(),trace)
        self.assertEqual(caught.exception.code,"planner.profile_invalid")
        io.assert_not_called()
        profile.unlink()
        self.assertEqual(planning.planner_profile()["version"],"env")

    def test_runtime_diagnostic_contract_preserves_remote_code(self):
        (self.config/"nav.json").write_text(json.dumps({"schema_version":1,"diagnostics":{
            "nav.path_blocked":{"version":"3","hint":"核对地图版本"}}}))
        step={"tool":"visit","version":"1","args":{}}
        reply={"operation_id":"s1","state":"FAILED","error":{"code":"nav.path_blocked","message":"blocked",
              "details":{"map":"v4","api_key":"dontarchive"}}}
        calls=[]
        with patch("service.http_json",return_value=reply),self.assertRaises(planning.Refused) as caught:
            Robot.run(None,step,"s1",{"endpoint":"http://provider","effect":"idempotent"},lambda k,v:calls.append((k,v)))
        self.assertEqual([k for k,_ in calls],["adapter.request","adapter.response"])
        info=event_diagnosis(caught.exception,False,"tool.failed")
        self.assertEqual((info["origin"],info["code"],info["catalog"]["version"]),("provider","nav.path_blocked","3"))
        self.assertEqual(info["details"]["content"]["map"],"v4")
        self.assertNotIn("dontarchive",json.dumps((info,calls)))
        ex=planning.failure(planning.OutcomeUnknown,"no heartbeat [code:navigation.heartbeat_expired]",
                            "tool.failed","edge_core")
        self.assertEqual(event_diagnosis(ex,False,"tool.failed")["code"],"navigation.heartbeat_expired")

    def test_metadata_and_bounded_payload(self):
        self.assertTrue(artifact({"text":"a"*50000})["truncated"])
        with patch.dict(os.environ,{"EDGE_TRACE_CONTENT":"metadata"}):
            self.assertNotIn("content",artifact({"token":"secret"}))
            self.assertNotIn("secret",json.dumps(artifact({"token":"secret"})))

    def test_optional_semantic_place_plugin_and_literal_patterns(self):
        manifest={"id":"scene.visit","version":"1","effect":"idempotent","parameters":{"place":"string"},
                  "description":"场所巡检","endpoint":"http://scene/tool","utterances":["去{place}巡检","前往{place}","?+{place}"]}
        (self.extra/"visit.json").write_text(json.dumps(manifest))
        self.assertNotIn("scene.visit",planning.registry())
        with patch.dict(os.environ,{"EDGE_EXTRA_PLUGIN_DIR":str(self.extra)}):
            caps=planning.registry()
            for request in ("去庭院巡检","前往庭院","?+庭院"):
                provider,steps=planning.plan(request,caps)
                self.assertEqual((provider,steps[0]["args"]),("local",{"place":"庭院"}))
            self.assertIsNone(planning.local_plan("另去庭院巡检",caps))
            self.assertEqual(planning.plan("前往 (1,0)",caps)[1][0]["tool"],"inspect")
            (self.extra/"visit.json").unlink()
            self.assertNotIn("scene.visit",planning.registry())


if __name__=="__main__":
    unittest.main()
