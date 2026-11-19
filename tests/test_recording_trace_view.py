"""Readable tool milestones are a projection. A running poll can never act as terminal evidence."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from task_recorder.trace_view import milestone_key, title, tool_history
from task_recorder.showcase import Showcase, channels, sample_value


class TraceViewTest(unittest.TestCase):
    def test_repeated_progress_and_distinct_operations(self):
        def event(phase, op=1, **extra):
            return dict(kind="agent_trace",phase=phase,operation_id=f"task/step/{op}",tool="sample",**extra)
        request=event("tool.request")
        running=event("tool.response",outcome="RUNNING")
        receipt=event("tool.response",outcome="CANDIDATE")
        verified=event("verified")
        second=event("tool.request",op=2)
        self.assertEqual(tool_history([request,running,request,running,receipt,verified,second]),[request,receipt,verified,second])
        self.assertIsNone(milestone_key(running))
        self.assertIn("CANDIDATE",title(receipt)[0])
        self.assertNotEqual(milestone_key(receipt),milestone_key(verified))

    def test_failure_retains_source_reference(self):
        row=dict(kind="agent_trace",phase="tool.error",operation_id="a/step/1",module="camera",
                 source_utc="2026-11-19T00:00:00.000Z",source_seq=73,code="camera.raw_encoding")
        heading,description=title(row)
        self.assertIn("失败",heading)
        self.assertIn("camera",heading)
        for evidence in ("camera.raw_encoding","a/step/1","73",row["source_utc"]):
            self.assertIn(evidence,description)


class ShowcaseTest(unittest.TestCase):
    def record(self, rows, declared=(), axis="stamp_ns", status="FINISHED"):
        archive=SimpleNamespace(header=dict(task_id="T",status=status,sources=list(declared)),by={},times={},all=[],marks=[])
        for i,(kind,stamp,data) in enumerate(rows,1):
            row=dict(kind=kind,stamp_ns=stamp*1_000_000_000,received_ns=i*1_000_000_000,
                     event_id=f"rec-{i}",task_id="T",seq=i)
            row.update(data)
            row["_at"]=row[axis]
            archive.all.append(row)
            archive.by.setdefault(kind,[]).append(row)
            if kind not in ("clock","map","twist","fused_pose","bus") or row.get("phase"):
                archive.marks.append(row)
        for k,items in archive.by.items():
            items.sort(key=lambda r:(r["_at"],r["seq"]))
            archive.times[k]=[r["_at"] for r in items]
        archive.marks.sort(key=lambda r:(r["_at"],r["seq"]))
        return archive

    def test_signal_age_gap_invalid_and_extension_channels(self):
        signal=dict(source="air",field="metrics.celsius",label="温度",unit="°C",max_age_s=.5)
        rows=[("air",1,dict(metrics={"celsius":0},clock_domain="lab")),
              ("air",3,dict(metrics={"celsius":99},valid=False,clock_domain="lab"))]
        view=Showcase(self.record(rows,["air"]),{"showcase":{"signals":[signal]}},"stamp_ns")
        first,points=view.samples(500_000_000,signal)
        self.assertEqual((first["status"],points),("waiting",[]))
        current,points=view.samples(1_000_000_000,signal)
        self.assertEqual((current["status"],current["value"],current["evidence"]["seq"]),("sample",0,1))
        gap,points=view.samples(2_000_000_000,signal)
        self.assertEqual((gap["status"],gap["age_s"],points),("stale",1,[(1_000_000_000,0)]))
        bad,points=view.samples(3_000_000_000,signal)
        self.assertEqual((bad["status"],bad["value"],points[-1][1]),("invalid",None,None))
        self.assertEqual(view.samples(3_000_000_000,dict(signal,source="absent"))[0]["status"],"unselected")
        self.assertFalse(view.events)
        self.assertEqual(sample_value({"xy":[-3.5,7]},"xy.0"),-3.5)
        for value in [True,float("inf"),float("nan"),"1",None]:
            self.assertIsNone(sample_value({"value":value},"value"))
        for bad_channel in [dict(signal,max_age_s=0),dict(signal,field="a()"),dict(signal,source="../device")]:
            with self.assertRaises(ValueError):
                channels({"showcase":{"signals":[bad_channel]}},["air"])

    def test_declared_available_signals_and_software_only_degradation(self):
        for sources,expected in [(("imu","laser_scan","map","twist"),["laser_scan","imu","twist"]),
                                 (("map","fused_pose","twist"),["twist","fused_pose","map"]),
                                 (("agent_trace",),[])]:
            self.assertEqual([s["source"] for s in channels({},sources)],expected)
        row=("agent_trace",1,dict(phase="tool.request",operation_id="T/step/1",clock_domain="agent_utc"))
        view=Showcase(self.record([row],["agent_trace"]),{},"stamp_ns")
        chapter=view.chapter(view.events[0])
        self.assertEqual(chapter["samples"],[])
        self.assertIsNone(chapter["related"])
        self.assertEqual(chapter["identity"]["operation_id"],"T/step/1")

    def test_empty_laser_returns_have_a_stamp_and_differ_from_missing_or_malformed(self):
        m=dict(beam_count=360,no_return_count=360,return_count=0,invalid_count=0)
        row=dict(valid=False,reason="no_valid_ranges",metrics=m,clock_domain="sim")
        rows=[("laser_scan",1,row),("laser_scan",2,dict(row,metrics=dict(m,invalid_count=1,no_return_count=359)))]
        view=Showcase(self.record(rows,["laser_scan"]),{},"stamp_ns")
        one,_=view.samples(1_000_000_000,view.signals[0])
        self.assertEqual((one["status"],one["value"],one["evidence"]["seq"]),("no_return",None,1))
        self.assertEqual(view.samples(2_000_000_000,view.signals[0])[0]["status"],"invalid")

    def test_clocks_source_rewind_fallback_and_active_rejected(self):
        cases=[[("mission",1,{}),("agent_trace",2,dict(clock_domain="agent_utc"))],
               [("clock",3,dict(clock_domain="sim")),("clock",1,dict(clock_domain="sim"))],[("custom",1,{})],
               [("imu",1,dict(topic="/robot0/imu")),("imu",2,dict(topic="/robot1/imu"))],
               [("imu",1,dict(clock_domain="robot-0")),("imu",2,dict(clock_domain="robot-1"))],
               [("imu",1,dict(stamp_origin="receipt",valid=False,clock_domain="sim"))]]
        for case in cases:
            sources={r[0] for r in case}
            with self.assertRaisesRegex(ValueError,"received_ns"):
                Showcase(self.record(case,sources),{},"stamp_ns")
            Showcase(self.record(case,sources,axis="received_ns"),{},"received_ns")
        with self.assertRaisesRegex(ValueError,"closed"):
            Showcase(self.record([("clock",1,{})],["clock"],status="ACTIVE"),{},"stamp_ns")

    def test_parent_operation_segment_and_temporal_events_remain_distinct(self):
        rows=[("bus",1,dict(event={"event_id":"gw","code":"gateway.accepted","operation_id":"leg/1/op","mission_id":"leg/1"})),
              ("mission",2,dict(phase="accepted",operation_id="T",mission_id="leg/1")),
              ("mission",3,dict(phase="stopped",operation_id="leg/1/op")),
              ("mission",4,dict(phase="link",parent_event_id="gw")),
              ("mission",5,dict(phase="unrelated",operation_id="T")),
              ("bus",6,dict(event={"event_id":"late","code":"later","mission_id":"leg/2"}))]
        rows=[(k,n,dict(v,clock_domain="sim")) for k,n,v in rows]
        view=Showcase(self.record(rows,["mission","bus"]),{},"stamp_ns")
        for row,relation in zip(view.events,["mission","operation","parent","timeline"]):
            link=view.chapter(row)["related"]
            self.assertEqual((link["relation"],link["source"]["event_id"],link["evidence"]["seq"]),(relation,"gw",1))
        self.assertEqual(view.activity(1_000_000_000),[])


if __name__ == "__main__":
    unittest.main()
