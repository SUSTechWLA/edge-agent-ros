"""SDD-049. Exercise the standalone kernel with real SQLite transactions and process locks."""
from contextlib import closing, contextmanager
import errno
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.kernel.ledger import Journal
from agent.kernel.verification import save_candidate


@contextmanager
def open_ledger(directory):
    journal = Journal(directory)
    with closing(journal.owner), closing(journal.db):
        yield journal


def isolated_python(source, *args):
    # Ignore PYTHONPATH and site packages, including ROS installations on CI hosts.
    return subprocess.run([sys.executable, "-I", "-S", "-B", "-c",
                           "import sys\nsys.path.insert(0, sys.argv[1])\n" + source,
                           str(ROOT), *map(str, args)], capture_output=True, text=True, timeout=15)


class StandaloneKernel(unittest.TestCase):
    def test_package_imports_without_apps_or_planner(self):
        result = isolated_python("""
from agent.kernel import authorization, catalog, host, ledger, verification, worker
import json
print(json.dumps(sorted(sys.modules)))
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        modules = set(json.loads(result.stdout))
        for unwanted in ("rclpy", "console", "perception", "model_provider", "sessions_plugin",
                         "evidence", "task_recorder", "planning", "agent.planning", "agent.service"):
            with self.subTest(module=unwanted):
                self.assertFalse(any(m == unwanted or m.startswith(unwanted + ".") for m in modules))

    def test_exclusive_owner_and_legacy_restart(self):
        contender = """
import json
from pathlib import Path
from agent.kernel.ledger import Journal
try:
    journal = Journal(Path(sys.argv[2]))
except OSError as error:
    print(json.dumps({"errno": error.errno}))
else:
    print(json.dumps(journal.get("legacy")))
    journal.db.close()
    journal.owner.close()
"""
        step = {"tool": "status", "version": "1", "args": {}, "status": "PENDING"}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with closing(sqlite3.connect(directory / "tasks.db")) as old, old:
                old.executescript("""
                    CREATE TABLE tasks(id TEXT PRIMARY KEY, text TEXT, state TEXT, detail TEXT, provider TEXT,
                        steps TEXT, cursor INTEGER, retries INTEGER, due REAL, created TEXT, updated TEXT);
                    CREATE TABLE events(seq INTEGER PRIMARY KEY, utc TEXT, task_id TEXT, step INTEGER,
                        kind TEXT, detail TEXT, operation_id TEXT);
                    INSERT INTO events VALUES(1,'then','legacy',0,'accepted','old receipt',NULL);
                """)
                old.execute("INSERT INTO tasks VALUES('legacy','status','RUNNING','','local',?,0,0,0,'then','then')",
                            (json.dumps([step]),))
            with open_ledger(directory) as journal:
                restored = journal.due(planning=False)
                self.assertEqual((restored["id"], restored["trace_id"], restored["steps"]), ("legacy", "legacy", [step]))
                events, more = journal.event_page("legacy")
                self.assertFalse(more)
                self.assertEqual([(e["detail"], e["event_id"], json.loads(e["payload"])) for e in events],
                                 [("old receipt", None, {})])
                blocked = isolated_python(contender, directory)
                self.assertEqual(blocked.returncode, 0, blocked.stderr)
                self.assertIn(json.loads(blocked.stdout)["errno"], (errno.EACCES, errno.EAGAIN))
            resumed = isolated_python(contender, directory)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(json.loads(resumed.stdout), restored)

    def test_event_write_failure_rolls_back_task_and_candidate(self):
        with tempfile.TemporaryDirectory() as temp, open_ledger(Path(temp)) as journal:
            db = journal.db
            db.executescript("""
                CREATE TRIGGER refuse_accept BEFORE INSERT ON events WHEN NEW.kind='accepted'
                BEGIN SELECT RAISE(ABORT,'accepted write failure'); END;
            """)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "accepted write failure"):
                journal.create("receipt", "status")
            self.assertIsNone(journal.get("receipt"))
            self.assertEqual(db.execute("SELECT count(*) FROM task_triggers").fetchone()[0], 0)
            db.execute("DROP TRIGGER refuse_accept")
            task = journal.create("receipt", "status")
            op = "receipt/step/1"
            task["steps"] = [{"tool": "status", "status": "RUNNING", "operation_id": op, "verification": {}}]
            journal.save(task, "RUNNING", "intent", kind="dispatch", op=op)
            db.executescript("""
                CREATE TRIGGER refuse_candidate BEFORE INSERT ON events WHEN NEW.kind='verification_pending'
                BEGIN SELECT RAISE(ABORT,'candidate write failure'); END;
            """)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "candidate write failure"):
                save_candidate(journal, task, task["steps"][0], op, {"x": 2}, {"outcome": "CANDIDATE"})
            durable = journal.get("receipt")["steps"][0]
            self.assertEqual((durable["status"], durable["operation_id"], durable["verification"]), ("RUNNING", op, {}))
            self.assertEqual([row["kind"] for row in journal.event_page("receipt")[0]], ["accepted", "dispatch"])

    def test_incremental_planned_event_omits_prior_step_evidence_bloat(self):
        with tempfile.TemporaryDirectory() as temp, open_ledger(Path(temp)) as journal:
            task = journal.create("cal-inc", "标定")
            step1 = {"tool": "plan_capture_views", "version": "5", "args": {}, "status": "PENDING"}
            task["steps"] = [step1]
            journal.save(task, "RUNNING", "已验证计划", kind="planned")
            first_ev = json.loads(journal.event_page("cal-inc")[0][-1]["payload"])
            self.assertEqual(len(first_ev["steps"]), 1)
            self.assertEqual(first_ev["steps"][0]["tool"], "plan_capture_views")
            # Simulate step 1 completing with a large evidence blob, then incremental step 2 planned.
            task["steps"][0].update(status="SUCCEEDED", operation_id="cal-inc/step/1",
                                    evidence={"view_options": [{"ref": f"v{i}", "data": "x"*200} for i in range(10)]},
                                    verification={"required": False}, physical_cap={"id": "x"})
            task["cursor"] = 1
            step2 = {"tool": "move_to_pose", "version": "1.4.4", "args": {"goal_ref": "view.01"}, "status": "PENDING",
                     "physical_cap": {"id": "move_to_pose"}}
            task["steps"].append(step2)
            journal.save(task, "RUNNING", "已验证计划", kind="planned")
            second_ev = json.loads(journal.event_page("cal-inc")[0][-1]["payload"])
            self.assertEqual(len(second_ev["steps"]), 2)
            self.assertNotIn("evidence", second_ev["steps"][0])
            self.assertNotIn("verification", second_ev["steps"][0])
            self.assertNotIn("physical_cap", second_ev["steps"][0])
            self.assertEqual(second_ev["steps"][0]["status"], "SUCCEEDED")
            self.assertEqual(second_ev["steps"][1]["tool"], "move_to_pose")
            self.assertIn("physical_cap", second_ev["steps"][1])
            # Authoritative task record keeps full evidence intact.
            self.assertIn("view_options", journal.get("cal-inc")["steps"][0]["evidence"])


if __name__ == "__main__":
    unittest.main()
