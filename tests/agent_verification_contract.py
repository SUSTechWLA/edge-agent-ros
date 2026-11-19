"""SDD-006 task persistence and compatibility tests in the ROS image."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import planning
import runtime_plugins
from service import Journal, Robot, worker


class VerificationContracts(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = patch.dict(os.environ, {"EDGE_PLUGIN_DIR":temp.name,"EDGE_EXTRA_PLUGIN_DIR":"",
                              "EDGE_CONTRACT_DIR":str(self.root/"site"), "EDGE_RETRY_LIMIT":"0",
                              "EDGE_OLLAMA_URL":"","EDGE_PLANNER_URL":""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cap = {"id":"camera","description":"collect photo","version":"1","effect":"idempotent",
                    "parameters":{},"endpoint":"http://camera/capture","outcomes":{"image":"uri"}}
        (self.root/"camera.json").write_text(json.dumps(self.cap))
        (self.root/"runtime").mkdir()
        self.manifest = {"schema_version":1, "id":"camera-check", "version":"1",
                         "endpoint":"http://check/verify", "provides":["verify.image"], "requires":[]}
        self.file = self.root/"runtime/camera-check.json"
        self.file.write_text(json.dumps(self.manifest))
        self.j = Journal(self.root/"data")
        self.addCleanup(self.j.db.close)
        self.addCleanup(self.j.owner.close)
        self.robot = Mock(spec=Robot)
        self.robot.run.return_value = {"uri":"fixture"}

    def tick(self, *, planning=False, verifying=False):
        with patch("service.rclpy.ok", side_effect=[True,False]):
            worker(self.j, self.robot, planning, verifying)

    def test_old_protected_step_already_in_flight_requires_reconciliation(self):
        for status in ("PENDING","RUNNING"):
            key = "legacy-"+status
            task = self.j.create(key,"collect photo")
            step = {"tool":"camera","args":{},"version":"1","binding":planning.binding(self.cap),"status":status}
            if status == "RUNNING":
                step["operation_id"] = key+"/step/1"
            task["steps"] = [step]
            self.j.save(task,"RUNNING","old format")
            self.tick()
            row = self.j.get(key)
            if status == "PENDING":
                self.assertEqual(row["steps"][0]["status"],"VERIFYING")
            else:
                self.assertEqual(row["state"],"RECOVERY_REQUIRED")
                self.assertFalse(row["can_retry"])
                last = self.j.event_page(key)[0][-1]
                self.assertEqual(json.loads(last["payload"])["diagnostic"]["code"],"verification.legacy_unbound")
        self.assertEqual(self.robot.run.call_count,1)

    def test_lost_freeze_reply_reuses_the_original_plan(self):
        task = self.j.create("frozen","compute")
        proposal = ["pinned", [{"tool":"old","soft_pin":{"binding":"original"}}]]
        self.j.soft_intent(task["id"], proposal)
        with patch.dict(os.environ,{"EDGE_SOFT_SOCKET":"/unused"}), patch("service.plan") as model, \
                patch("service.soft_client.request",return_value=proposal) as freeze:
            self.tick(planning=True)
        model.assert_not_called()
        freeze.assert_called_once_with("freeze_plan",task="frozen",provider="pinned",steps=proposal[1])
        self.assertEqual(self.j.get("frozen")["steps"],proposal[1])
        self.assertIsNone(self.j.soft_intent("frozen"))

    def test_partial_mixed_admission_resumes_original_intent_without_dispatch(self):
        from copy import deepcopy
        from soft_client import SoftError

        key = "mixed-admission"
        self.j.create(key, "read and capture")
        steps = [
            {"tool":"read","status":"PENDING","soft_pin":{"binding":"a"}},
            {"tool":"capture","status":"PENDING","physical_pin":{"class":"physical","binding":"b"}},
        ]
        self.j.soft_intent(key, ["fixed", steps])
        accepted = deepcopy(steps)
        accepted[0]["soft_pin"]["reservation_id"] = key + "/step/1"
        complete = deepcopy(accepted)
        complete[1]["physical_pin"]["reservation_id"] = key + "/step/2"
        with patch.dict(os.environ,{"EDGE_SOFT_SOCKET":"/unused"}), patch("service.plan") as model, \
                patch("service.soft_client.request",return_value=["fixed",accepted]) as soft, \
                patch("plugin_tracks.physical_request", side_effect=[
                    SoftError("plugin.transport","freeze reply lost"), ["fixed",complete]]) as physical:
            self.tick(planning=True)
            self.assertEqual(self.j.get(key)["state"],"BLOCKED")
            self.assertTrue(self.j.get(key)["can_retry"])
            self.assertEqual(self.j.soft_intent(key), ["fixed",steps])
            self.assertEqual(self.j.retry(key)["state"],"PLANNING")
            self.tick(planning=True)
        self.assertEqual(self.j.get(key)["steps"], complete)
        self.assertIsNone(self.j.soft_intent(key))
        self.assertEqual(soft.call_args_list[0],soft.call_args_list[1])
        self.assertEqual(physical.call_args_list[0],physical.call_args_list[1])
        model.assert_not_called()
        self.robot.run.assert_not_called()

    def test_soft_host_outage_preserves_kernel_status(self):
        self.j.create("kernel","status")
        with patch.dict(os.environ,{"EDGE_SOFT_SOCKET":"/nonexistent/edge-soft.sock"}):
            self.tick(planning=True)
        row = self.j.get("kernel")
        self.assertEqual((row["state"],row["steps"][0]["tool"]),("RUNNING","status"))
        self.assertNotIn("soft_pin",row["steps"][0])

    def test_soft_settlement_follows_terminal_evidence(self):
        task = self.j.create("soft", "query soft facts")
        pins = [{"reservation_id":f"soft/step/{i}"} for i in (1,2)]
        task["steps"] = [{"soft_pin":pin, "status":"PENDING"} for pin in pins]
        def queued():
            return [r[0] for r in self.j.db.execute("SELECT operation FROM soft_settlements ORDER BY operation")]
        self.j.save(task,"RUNNING","frozen",kind="planned")
        self.j.save(task,"RECOVERY_REQUIRED","lost receipt",kind="error")
        self.assertEqual(queued(),[])
        task["steps"][0]["status"] = "SUCCEEDED"
        task["cursor"] = 1
        self.j.save(task,"RUNNING","independently verified",kind="verified")
        self.assertEqual(queued(),["soft/step/1"])
        self.j.save(task,"BLOCKED","rejected before dispatch",kind="blocked")
        self.assertEqual(queued(),["soft/step/1","soft/step/2"])

    def test_settlement_outbox_migrates_and_one_poison_batch_cannot_starve_other_tasks(self):
        from soft_client import SoftError

        task = self.j.create("retry-owner","trace the original read task")
        self.j.save(task,"RECOVERY_REQUIRED","operator may retry",kind="retry_exhausted")
        operations = [f"retry-owner/step/{i}" for i in range(1,67)]
        with self.j.db:
            self.j.db.execute("DROP TABLE soft_settlements")
            self.j.db.execute("CREATE TABLE soft_settlements(operation TEXT PRIMARY KEY,pin TEXT NOT NULL)")
            self.j.db.executemany("INSERT INTO soft_settlements VALUES(?,?)",
                                  [(op,json.dumps({"reservation_id":op})) for op in operations])
        self.j.db.close()
        self.j.owner.close()
        self.j = Journal(self.root/"data")
        self.addCleanup(self.j.db.close)
        self.addCleanup(self.j.owner.close)
        unavailable = set(operations[:65])

        def settle(action, *, pin):
            self.assertEqual(action,"settle")
            if pin["reservation_id"] in unavailable:
                raise SoftError("plugin.draining","injected persistent failure")
            return True

        stop = threading.Event()
        with patch("service.soft_client.request",side_effect=settle) as rpc, patch("service.time.time",return_value=1000):
            self.j.settle_soft_batch(stop)
            self.assertEqual(rpc.call_count,64)
            self.j.settle_soft_batch(stop)
            self.assertEqual(rpc.call_count,66)
            self.j.settle_soft_batch(stop)
            self.assertEqual(rpc.call_count,66)
        queued = self.j.db.execute("SELECT operation,attempts FROM soft_settlements").fetchall()
        self.assertEqual(dict(queued),{op:1 for op in unavailable})
        with patch("service.soft_client.request",side_effect=settle), patch("service.time.time",return_value=1001):
            self.j.settle_soft_batch(stop)
            self.j.settle_soft_batch(stop)
        unavailable.clear()
        with patch("service.soft_client.request",side_effect=settle), patch("service.time.time",return_value=1003):
            self.j.settle_soft_batch(stop)
            self.j.settle_soft_batch(stop)
        self.assertEqual(self.j.db.execute("SELECT COUNT(*) FROM soft_settlements").fetchone()[0],0)
        counts = dict(self.j.db.execute("SELECT kind,count(*) FROM events WHERE kind LIKE 'settlement_%' GROUP BY kind"))
        self.assertEqual(counts,{"settlement_deferred":65,"settlement_recovered":65})
        self.assertTrue(self.j.get("retry-owner")["can_retry"])

    def test_atomic_candidate_and_independent_queues(self):
        self.j.create("capture","collect photo")
        self.tick(planning=True)
        self.tick()
        events = self.j.event_page("capture")[0]
        self.assertEqual([row["kind"] for row in events][-2:], ["tool.response","verification_pending"])
        self.assertEqual(json.loads(events[-2]["payload"])["trace"]["outcome"], "CANDIDATE")
        step = self.j.get("capture")["steps"][0]
        self.assertEqual(step["verification"]["candidate"], {"uri":"fixture"})
        self.assertEqual(self.j.due(False, True)["id"], "capture")
        self.assertIsNone(self.j.due(False))
        self.j.create("other","status")
        self.tick(planning=True)
        self.assertEqual(self.j.due(False)["id"], "other")
        self.tick()
        self.assertEqual(self.j.get("other")["state"],"SUCCEEDED")
        receipt = {"verdict":"pass","evidence":{"decoded":True}}
        with patch.object(runtime_plugins,"verify",return_value=receipt) as verifier:
            self.tick(verifying=True)
        self.assertEqual(verifier.call_args.args[1]["operation_id"], step["operation_id"])
        finished = self.j.get("capture")["steps"][0]
        self.assertEqual(finished["evidence"], {"uri":"fixture"})
        self.assertEqual(finished["verification"]["decisions"], [receipt])
        self.assertEqual(self.robot.run.call_count,2)

    def test_candidate_and_trace_rollback_together_on_failed_write(self):
        self.j.create("capture","collect photo")
        self.tick(planning=True)
        task = self.j.get("capture")
        task["steps"][0]["status"] = "VERIFYING"
        task["steps"][0]["verification"]["candidate"] = {"uri":"fixture"}
        original = self.j.append
        def interrupted(task, kind, *args, **kwargs):
            if kind == "verification_pending":
                raise RuntimeError("injected transaction abort")
            return original(task, kind, *args, **kwargs)
        with patch.object(self.j,"append",side_effect=interrupted), self.assertRaises(RuntimeError):
            self.j.save(task,"RUNNING","candidate",kind="verification_pending",op="capture/step/1",
                        tool_response={"outcome":"CANDIDATE"})
        self.assertEqual(self.j.get("capture")["steps"][0]["status"],"PENDING")
        self.assertNotIn("tool.response",[row["kind"] for row in self.j.event_page("capture")[0]])

    def test_policy_failure_never_leads_to_dispatch(self):
        folder = self.root/"site"
        folder.mkdir()
        policy_file = folder/"verification-policy.json"
        policy_file.write_text("["*1200 + "]"*1200)
        self.j.create("capture","collect photo")
        self.tick(planning=True)
        task = self.j.get("capture")
        self.assertEqual(task["state"], "BLOCKED")
        self.assertTrue(task["can_retry"])
        self.assertFalse(task["steps"])
        self.robot.run.assert_not_called()
        self.assertEqual(json.loads(self.j.event_page("capture")[0][-1]["payload"])["diagnostic"]["code"],
                         "verification.policy_invalid")
        policy_file.unlink()
        self.j.retry("capture")
        self.tick(planning=True)
        self.assertEqual(self.j.get("capture")["state"], "RUNNING")

    def test_corrupt_legacy_task_cannot_stop_either_queue(self):
        for key, raw in (("broken","{bad"), ("wrong-type","{}")):
            self.j.create(key, "collect photo")
            with self.j.db:
                self.j.db.execute("UPDATE tasks SET state='RUNNING',steps=? WHERE id=?", (raw,key))
        self.j.db.close()
        self.j.owner.close()
        self.j = Journal(self.root/"data")
        self.addCleanup(self.j.db.close)
        self.addCleanup(self.j.owner.close)
        originals = dict(self.j.db.execute("SELECT task_id,original_steps FROM quarantined_steps"))
        self.assertEqual(originals, {"broken":"{bad","wrong-type":"{}"})
        for key in originals:
            self.assertFalse(self.j.get(key)["can_retry"])
            self.assertEqual(self.j.get(key)["state"], "RECOVERY_REQUIRED")
            self.assertEqual(self.j.event_page(key)[0][-1]["kind"],"steps_quarantined")
        self.assertIsNone(self.j.due(False, True))
        self.j.create("healthy","status")
        self.tick(planning=True)
        self.tick()
        self.assertEqual(self.j.get("healthy")["state"],"SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
