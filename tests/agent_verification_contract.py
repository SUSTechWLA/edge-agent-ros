"""SDD-006 task persistence and compatibility tests in the ROS image."""
import json
import os
from pathlib import Path
import sys
import tempfile
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
