"""Configuration ownership, atomicity and unknown legacy history for SDD-037."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"agent"))
sys.path.insert(0,str(ROOT/"perception/sensor_health"))
from module_journal import ModuleJournal
import incidents


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.j=ModuleJournal(Path(self.tmp.name)/"events.sqlite")
        self.ident=dict(journal=self.j.source_id,robot="rover",topic="/front/raw",expected_frame="front_optical",
                        domain=12,input="rgb",sim_time=False)
        self.row=dict(identity=self.ident,state="needs_attention",code="black_image",
                      latest={"frame_id":"front_optical"},last_stamp=123)
        with self.j.db:incidents.open_table(self.j.db)
    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()
    def fault(self):
        self.row["event_id"]=incidents.boundary(self.j,"rgb",None,"needs_attention","fixture",{"reason":"black_image"})
        incidents.remember(self.j.db,"rgb",self.row)
    def latest(self):
        raw=self.j.db.execute("SELECT event FROM module_bus_events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        return json.loads(raw)
    def test_each_scope_change_detaches_old_owner(self):
        for field,new in dict(robot="g1",topic="/rear/raw",expected_frame="rear_optical",domain=13,
                              input="depth",sim_time=True,journal="another-journal").items():
            with self.subTest(field=field):
                current=dict(identity={**self.ident,field:new},state="waiting",latest={})
                with self.j.db:
                    self.fault()
                    incidents.restore(self.j,{"rgb":current},100,"boot-test")
                event=self.latest()
                self.assertEqual((event["kind"],event["parent_event_id"]),("sensor.source_detached",self.row["event_id"]))
                self.assertEqual(current["state"],"waiting")
                self.assertEqual(self.j.db.execute("SELECT COUNT(*) FROM sensor_health_open").fetchone()[0],0)
    def test_removed_source_and_legacy_events_cannot_claim_a_recovery(self):
        with self.j.db:
            self.fault()
            incidents.restore(self.j,{},100,"boot-test")
            self.assertEqual(self.latest()["kind"],"sensor.source_detached")
            old=self.j.emit({"module":"sensor.health","kind":"sensor.needs_attention","code":"sensor.black_image",
                             "trace_id":"sensor.health.rgb","data":{"phase":"needs_attention","input":"rgb"}})
            current=dict(identity=self.ident,state="waiting")
            incidents.restore(self.j,{"rgb":current},200,"boot-test2")
        self.assertEqual(self.latest()["event_id"],old)
        self.assertEqual(current["state"],"waiting")
    def test_failed_transaction_rolls_back_both_event_and_checkpoint(self):
        with self.assertRaises(RuntimeError):
            with self.j.db:
                self.fault()
                raise RuntimeError("simulated failure before commit")
        for table in ("module_bus_events","sensor_health_open"):
            self.assertEqual(self.j.db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0],0)


if __name__=="__main__":unittest.main()
