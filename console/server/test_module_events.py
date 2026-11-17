"""SDD-001: durable observation ingress is independent of task execution."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from history import History
from module_events import parse_event
from observer import Observer


def envelope(event_id, **fields):
    return {"schema_version":1,"event_id":event_id,"trace_id":"trace-site-a","module":"motor.controller",
            "code":"driver.voltage_low","kind":"driver.alert","trigger":{"kind":"watchdog","id":"scan-7","source":"motor"},
            "task_id":"job-2","data":{"volts":11.7},**fields}


class ModuleEventTest(unittest.TestCase):
    def test_strict_contract_and_privacy(self):
        original=envelope("e",detail="authorization Bearer abc123",data={"token":"private","temperature":21})
        parsed,_=parse_event(json.dumps(original))
        self.assertNotIn("abc123",str(parsed))
        self.assertNotIn("private",str(parsed))
        for field in ({"schema_version":True},{"event_id":""},{"parent_event_id":"e"},{"utc":"2026-11-17"},
                      {"data":{"sample":float("nan")}},{"trigger":"watchdog"},{"detail":"a"*65536}):
            with self.subTest(field=list(field)), self.assertRaises(ValueError):
                parse_event(json.dumps(envelope("e",**field)) if "event_id" not in field else
                            json.dumps({**envelope("e"),**field}))

    def test_old_database_and_reopened_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbfile=Path(tmp)/"history.db"
            with sqlite3.connect(dbfile) as db:
                db.execute("""CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    utc TEXT NOT NULL, observed_at REAL NOT NULL,kind TEXT NOT NULL,level TEXT NOT NULL,
                    source TEXT NOT NULL,session_id TEXT NOT NULL,mission_id TEXT,operation_id TEXT,
                    detail TEXT NOT NULL,data TEXT NOT NULL,cause_seq INTEGER,source_id TEXT,source_seq INTEGER,
                    snapshot BLOB NOT NULL,UNIQUE(source_id,source_seq))""")
                import zlib
                db.execute("""INSERT INTO events(utc,observed_at,kind,level,source,session_id,detail,data,snapshot)
                              VALUES('old',0,'old','info','old','session','','{}',?)""",
                           (zlib.compress(b'{"session_id":"session"}'),))
            history=History(dbfile)
            self.assertIsNone(history.replay(1)["event"]["trigger_id"])
            history.close()
            history=History(dbfile)
            self.assertEqual(history.page(None,None,20,{"trigger_id":"absent"})["events"],[])
            history.close()

    def test_ros_producer_out_of_order_duplicate_conflict_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{
            "EDGE_CONSOLE_DIR":tmp,"EDGE_TASK_DB":tmp+"/absent","EDGE_LEDGER_PATH":tmp+"/missing",
            "EDGE_ENVIRONMENT":"hardware","EDGE_OBSERVATIONS_DIR":tmp+"/observations"}):
            rclpy.init()
            reader=Observer()
            emitter=Node("test_module_event_emitter")
            publisher=emitter.create_publisher(String,"/edge/events",100)
            def wait(predicate):
                end=time.monotonic()+7
                while time.monotonic()<end:
                    rclpy.spin_once(reader,timeout_sec=.025)
                    if predicate():
                        return
                self.fail("ROS observation failed to arrive")

            def send(event):
                publisher.publish(String(data=json.dumps(event)))

            def rows(**filters):
                return reader.history.page(0,None,100,filters)["events"]
            try:
                wait(lambda:publisher.get_subscription_count()>0)
                child=envelope("child-7",parent_event_id="parent-7",utc="2025-01-01T00:00:05Z")
                parent=envelope("parent-7",utc="2025-01-01T00:00:02Z")
                send(child)
                wait(lambda:rows(event_id="child-7"))
                self.assertIsNone(rows(event_id="child-7")[0]["cause_seq"])
                send({**parent,"parent_event_id":"child-7"})
                wait(lambda:rows(kind="module.event_invalid"))
                self.assertEqual(rows(event_id="parent-7"),[])
                send(child)
                send(parent)
                send({**child,"data":{"volts":0.0}})
                wait(lambda:rows(kind="module.event_conflict"))
                selection={"trace_id":"trace-site-a","trigger_kind":"watchdog","code":"driver.voltage_low",
                           "task_id":"job-2","module":"motor.controller","trigger_id":"scan-7"}
                c,p=rows(**selection)
                self.assertLess(c["seq"],p["seq"])
                self.assertEqual(c["cause_seq"],p["seq"])
                self.assertEqual(c["source"],"ros:/edge/events")
                self.assertEqual(c["data"]["volts"],11.7)
                frame=reader.history.replay(c["seq"])
                self.assertIsNone(frame["telemetry_at"])
                self.assertEqual(frame["event"]["parent_event_id"],"parent-7")
                self.assertFalse(Path(tmp+"/absent").exists())
                reader.history.close()
                reader.history=History(Path(tmp)/"history.db")
                reader.module_event(String(data=json.dumps(parent)))
                exported=[json.loads(line) for line in reader.history.export(selection,True)]
                self.assertEqual([e["seq"] for e in exported],[c["seq"],p["seq"]])
                print("module-event DDS trace:",[(e["event_id"],e["seq"],e["cause_seq"]) for e in exported])
            finally:
                reader.history.close()
                emitter.destroy_node()
                reader.destroy_node()
                rclpy.shutdown()


if __name__=="__main__":
    unittest.main()
