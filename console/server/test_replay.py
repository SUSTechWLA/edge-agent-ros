"""Exercise the public diagnostic boundaries without a live robot or model."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rclpy

from observer import Observer


class ReplayTest(unittest.TestCase):
    def test_import_continues_after_legacy_nonfinite_model_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / "tasks.db"
            db = sqlite3.connect(source)
            db.executescript("""
                CREATE TABLE audit_identity(singleton INTEGER,id TEXT);
                INSERT INTO audit_identity VALUES(1,'legacy-model');
                CREATE TABLE events(seq INTEGER PRIMARY KEY,utc TEXT,task_id TEXT,step INTEGER,
                                    kind TEXT,detail TEXT,operation_id TEXT,payload TEXT);
                INSERT INTO events VALUES
                    (1,'2025-11-01T12:00:00Z','diagnose',0,'model.response','','operation-1',
                    '{"trace":{"event_id":"old-reply","trace_id":"old-trace","artifact":{"rows":[NaN,-Infinity,1e999]}}}'),
                    (2,'2025-11-01T12:00:01Z','diagnose',0,'blocked','','operation-1',
                    '{"trace":{"event_id":"old-block","parent_event_id":"old-reply","trace_id":"old-trace"}}');
            """)
            db.close()
            env = {"EDGE_CONSOLE_DIR": str(path / "console"), "EDGE_TASK_DB": str(source),
                   "EDGE_LEDGER_PATH": str(path / "absent.db"), "EDGE_ENVIRONMENT": "hardware"}
            rclpy.init()
            try:
                with patch.dict(os.environ, env):
                    for _ in range(2):
                        obs = Observer()
                        try:
                            obs.tick()
                            events = [json.loads(line) for line in obs.history.export({"trace_id":"old-trace"},True)]
                            self.assertEqual([e["event_id"] for e in events], ["old-reply","old-block"])
                            self.assertEqual([e["data"]["source_seq"] for e in events], [1,2])
                            archived = events[0]["data"]
                            self.assertEqual(archived["payload"]["trace"]["artifact"]["rows"], ["NaN","-Infinity","1e999"])
                            self.assertCountEqual(archived["legacy_nonfinite_literals"], ["NaN","-Infinity","1e999"])
                            self.assertNotIn("legacy_nonfinite_literals", events[1]["data"])
                            self.assertEqual(events[1]["cause_seq"], events[0]["seq"])
                        finally:
                            obs.history.close()
                            obs.destroy_node()
            finally:
                rclpy.shutdown()

    def test_authoritative_transitions_survive_disconnect(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            journal=sqlite3.connect(root/"tasks.db")
            journal.executescript("""
                CREATE TABLE audit_identity(singleton INTEGER,id TEXT);
                INSERT INTO audit_identity VALUES(1,'agent-v1');
                CREATE TABLE events(seq INTEGER PRIMARY KEY,utc TEXT,task_id TEXT,step INTEGER,
                                    kind TEXT,detail TEXT,operation_id TEXT,payload TEXT);
                INSERT INTO events VALUES(1,'2025-01-01T00:00:01Z','inspect',0,'accepted','inspect A',NULL,'{}'),
                  (2,'2025-01-01T08:00:02+08:00','inspect',0,'dispatch','submitted','inspect/step/1','{}');
            """)
            journal.close()
            ledger=sqlite3.connect(root/"core.db")
            ledger.executescript("""
                CREATE TABLE audit_identity(singleton INTEGER,id TEXT);
                INSERT INTO audit_identity VALUES(1,'core-v1');
                CREATE TABLE missions(mission_id TEXT,state TEXT,detail TEXT);
                CREATE TABLE steps(mission_id TEXT,step INTEGER,x REAL,y REAL,operation_id TEXT,
                                    action_result TEXT,evidence_x REAL,evidence_y REAL,odom_stamp_ns INTEGER);
                INSERT INTO missions VALUES('inspect/step/1','SUCCEEDED','verified');
                INSERT INTO steps VALUES('inspect/step/1',0,1,0,'nav','ok',1,0,5);
                CREATE TABLE mission_events(seq INTEGER PRIMARY KEY,utc TEXT,mission_id TEXT,operation_id TEXT,
                    kind TEXT,prior_state TEXT,state TEXT,detail TEXT,cause_seq INTEGER);
                INSERT INTO mission_events VALUES
                  (1,'2025-01-01T00:00:03Z','inspect/step/1',NULL,'mission.created',NULL,'RUNNING','accepted',NULL),
                  (2,'2025-01-01T00:00:04Z','inspect/step/1','nav','step.dispatch',NULL,NULL,'{"step":0}',1),
                  (3,'2025-01-01T00:00:05Z','inspect/step/1','nav','step.verified',NULL,NULL,
                      '{"step":0,"observed_x":1,"observed_y":0,"odom_stamp_ns":5}',2),
                  (4,'2025-01-01T00:00:06Z','inspect/step/1','nav','mission.transition','RUNNING','SUCCEEDED','verified',3);
            """)
            ledger.close()
            env={"EDGE_CONSOLE_DIR":str(root/"console"),"EDGE_LEDGER_PATH":str(root/"core.db"),
                 "EDGE_TASK_DB":str(root/"tasks.db"),"EDGE_ENVIRONMENT":"hardware"}
            rclpy.init()
            try:
                with patch.dict(os.environ,env):
                    for _ in range(2):
                        observer=Observer()
                        try:
                            observer.tick()
                            rows=[json.loads(line) for line in observer.history.export({"task_id":"inspect"},True)]
                            self.assertEqual(len(rows),6)
                            self.assertEqual(rows[2]["cause_seq"],rows[1]["seq"])
                            self.assertEqual(rows[-1]["cause_seq"],rows[-2]["seq"])
                            earlier=rows[2]["snapshot"]
                            self.assertEqual(earlier["missions"][0]["state"],"RUNNING")
                            self.assertIsNone(earlier["missions"][0]["steps"][0]["evidence_x"])
                            self.assertIsNone(earlier["telemetry_at"])
                            self.assertFalse(earlier["sim_world_aligned"])
                            self.assertEqual(rows[-1]["snapshot"]["missions"][0]["steps"][0]["evidence_x"],1)
                        finally:
                            observer.history.close()
                            observer.destroy_node()
            finally:
                rclpy.shutdown()


if __name__=="__main__":
    unittest.main()
