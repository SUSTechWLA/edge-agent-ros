"""SDD-044. Exercise the HTTP control CLI and inspect everything retained by its observer."""
from contextlib import contextmanager, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_recorder import cli
from task_recorder import agent_trace
from task_recorder.service import Archive, Handler


@contextmanager
def serving(handler):
    with ThreadingHTTPServer(("127.0.0.1",0),handler) as server:
        thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.01},daemon=True)
        thread.start()
        try:
            yield server,"http://127.0.0.1:"+str(server.server_port)
        finally:
            server.shutdown()
            thread.join()


class Agent(BaseHTTPRequestHandler):
    def log_message(self,*_):
        pass

    def do_GET(self):
        path=urlsplit(self.path)
        q=parse_qs(path.query)
        self.server.received.append((self.command,path.path,q.get("id")))
        states=self.server.states
        value=states.pop(0) if len(states)>1 else states[0]
        if isinstance(value,int):
            self.send_error(value)
            return
        if hasattr(self.server,"ledger"):
            after=int(q["after"][0])
            size=int(q["limit"][0])
            self.server.pages.append(after)
            if after and hasattr(self.server,"second_page"):
                self.server.second_page.set()
                self.server.resume.wait(timeout=2)
            rows=[row for row in self.server.ledger if row["seq"]>after]
            value={**value,"events":rows[:size],"events_has_more":len(rows)>size}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())


class CLI(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        server,self.rec=self.enterContext(serving(Handler))
        server.archive=Archive(Path(self.tmp.name))
        self.addCleanup(server.archive.close)
        self.agent,self.url=self.enterContext(serving(Agent))
        self.agent.received=[]

    def call(self,*args):
        with redirect_stdout(io.StringIO()) as output:
            cli.main(["--recorder",self.rec,*args])
        return json.loads(output.getvalue().splitlines()[-1])

    def test_future_task_finish_and_export(self):
        task="paint / report"
        hidden="private-content-for-this-test"
        def snapshot(state,cur):
            return dict(id=task,state=state,cursor=cur,text=hidden,steps=[dict(tool=hidden,args={"token":hidden})],
                        events=[dict(detail=hidden)],evidence={"credentials":hidden})
        self.agent.states=[404,503,snapshot("RUNNING",0),snapshot("RUNNING",0),snapshot("SUCCEEDED",1)]
        s=self.call("start","future","--task-id",task)
        self.assertEqual(s["sources"],["agent_state"])
        s=self.call("watch","future","--agent",self.url,"--wait-for-task","--finish","--interval",".005","--timeout","3")
        self.assertEqual((s["status"],s["task_status"],s["counts"]["agent_state"]),("FINISHED","SUCCEEDED",4))
        self.assertEqual(self.call("status","future")["task_id"],task)
        self.assertEqual(self.call("export","future")["status"],"FINISHED")
        self.assertEqual(self.call("stop","future")["task_status"],"SUCCEEDED")
        events=(Path(self.tmp.name)/"future/media/mission/timeline.jsonl").read_text("utf8")
        self.assertNotIn(hidden,events)
        self.assertEqual({tuple(x[:2]) for x in self.agent.received},{("GET","/agent/tasks")})
        self.assertTrue(all(x[2]==[task] for x in self.agent.received))
        self.assertTrue(all(row["clock_domain"]=="observer_unix_ns" for row in map(json.loads,events.splitlines())))

    def test_unclosed_watch_and_mismatch(self):
        self.agent.states=[{"id":"job","state":"BLOCKED","steps":[],"cursor":0}]
        self.call("start","operator","--task-id","job")
        self.assertEqual(self.call("watch","operator","--agent",self.url)["status"],"ACTIVE")
        self.assertEqual(self.call("stop","operator")["task_status"],"STOPPED")
        self.call("start","other","--task-id","other-job")
        with self.assertRaisesRegex(SystemExit,"2"):
            self.call("watch","other","--agent",self.url)
        self.assertEqual(self.call("status","other")["counts"],{})

    def test_operator_stop_interrupts_observer_during_long_task(self):
        self.agent.states=[{"id":"job","state":"RUNNING","steps":[{}],"cursor":0}]
        self.call("start","long","--task-id","job")
        with ThreadPoolExecutor(max_workers=1) as pool, redirect_stdout(io.StringIO()):
            result=pool.submit(cli.watch,self.rec,"long",self.url,wait_for_task=False,finish=True,interval=.01,timeout=3)
            start=time.monotonic()
            while not cli.endpoint(self.rec,"/recordings/long")["counts"]:
                if time.monotonic()-start>2: self.fail("watcher did not append a progress event")
                time.sleep(.01)
            self.call("stop","long")
            self.assertEqual(result.result(timeout=1)["task_status"],"STOPPED")
        self.assertEqual(self.call("status","long")["counts"]["agent_state"],1)

    def test_racing_finishers_keep_the_first_receipt(self):
        self.call("start","race","--task-id","job")
        barrier=threading.Barrier(2)
        def close(status):
            barrier.wait(timeout=2)
            return cli.finish_recording(self.rec,"/recordings/race","job",status,status)
        with ThreadPoolExecutor(max_workers=2) as pool:
            left,right=list(pool.map(close,("STOPPED","SUCCEEDED")))
        self.assertEqual(left["task_status"],right["task_status"])
        self.assertIn(left["task_status"],("STOPPED","SUCCEEDED"))
        self.assertEqual(left["ended_ns"],self.call("status","race")["ended_ns"])
        with self.assertRaises(cli.EndpointError):
            cli.finish_recording(self.rec,"/recordings/race","different","STOPPED","wrong task")

    def test_terminal_task_backfills_history_and_retries_are_idempotent(self):
        hidden="private-free-form fixture"
        stamp="2026-11-19T02:10:55.876543+00:00"
        task=dict(id="job",state="SUCCEEDED",cursor=1,steps=[{"args":hidden}],created=stamp,text=hidden,
                  trace_id="parent-task")
        self.agent.states=[task]
        self.agent.pages=[]
        self.agent.ledger=[]
        for n in range(1,39):
            source_id=f"{n:032x}"
            payload={"trace":{"event_id":source_id,"trace_id":"parent-task","module":"checksum","layer":"tool",
                              "outcome":"SUCCEEDED","provider":"api","code":"agent.tool.response",
                              "artifact":{"content":hidden},"freeform":hidden,"token":hidden},
                     "arguments":{"key":hidden}}
            self.agent.ledger.append(dict(seq=n*5,event_id=source_id,utc=stamp,task_id="job",step=0,
                                         kind="tool.response",operation_id="job/step/1",detail=hidden,
                                         payload=json.dumps(payload)))
        self.call("start","history","--task-id","job","--sources","agent_state","agent_trace")
        self.call("watch","history","--agent",self.url)
        result=self.call("watch","history","--agent",self.url,"--finish")
        self.assertEqual(result["counts"]["agent_trace"],38)
        self.assertEqual(self.agent.pages,[0,160,0,160])
        archive=(Path(self.tmp.name)/"history/media/mission/timeline.jsonl").read_text("utf8")
        self.assertNotIn(hidden,archive)
        traces=[r for r in map(json.loads,archive.splitlines()) if r["kind"]=="agent_trace"]
        self.assertEqual([r["source_seq"] for r in traces],list(range(5,191,5)))
        self.assertEqual(traces[0]["source_event_id"],self.agent.ledger[0]["event_id"])
        self.assertEqual((traces[0]["phase"],traces[0]["tool"],traces[0]["code"]),
                         ("tool.response","checksum","agent.tool.response"))
        self.assertEqual(traces[-1]["stamp_ns"],1795054255876543000)

    def test_request_text_requires_source_and_preserves_redaction(self):
        token="sk-"+"a"*32
        self.agent.states=[dict(id="job",state="SUCCEEDED",text="please check "+token,created="2026-11-19T02:00:00Z")]
        self.call("start","consent","--task-id","job","--sources","agent_state","agent_trace","agent_request")
        self.call("watch","consent","--agent",self.url)
        result=self.call("watch","consent","--agent",self.url,"--finish")
        self.assertEqual(result["missing_sources"],["agent_trace"])
        self.assertEqual(result["counts"]["agent_request"],1)
        body=(Path(self.tmp.name)/"consent/media/mission/timeline.jsonl").read_text("utf8")
        self.assertNotIn(token,body)
        self.assertIn("please check [REDACTED]",body)

    def test_bad_journal_rows_fail_closed_before_append(self):
        task={"id":"job","trace_id":"parent","state":"SUCCEEDED"}
        valid=dict(task_id="job",kind="tool.request",seq=11,step=0,operation_id="job/step/1",utc="2026-11-19T02:00:00Z")
        for change in ({"task_id":"another"},{"operation_id":"another/step/1"},{"operation_id":"job/step/2"},
                       {"seq":True},{"seq":2**63},{"step":-1},{"utc":"2026-11-19T02:00:00"},
                       {"kind":"free form"},{"payload":{"trace":{"trace_id":"another"}}},
                       {"payload":{"trace":{"event_id":"bad"}}}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                agent_trace.page({**task,"events":[{**valid,**change}]},0)
        for rows,more in (([valid,valid],False),([],True),([valid]*33,False),([],1)):
            with self.assertRaises(ValueError):
                agent_trace.page({**task,"events":rows,"events_has_more":more},0)
        self.agent.states=[{**task,"events":[valid,{**valid,"seq":12,"task_id":"another"}]}]
        self.call("start","mixed","--task-id","job","--sources","agent_state","agent_trace")
        with self.assertRaisesRegex(SystemExit,"2"):
            self.call("watch","mixed","--agent",self.url,"--finish")
        self.assertEqual(self.call("status","mixed")["counts"],{})

    def test_interrupted_backfill_keeps_operator_receipt(self):
        row=dict(task_id="job",kind="dispatch",step=0,utc="2026-11-19T02:00:00.000000001Z")
        self.agent.states=[dict(id="job",state="SUCCEEDED",steps=[{}],cursor=1)]
        self.agent.pages=[]
        self.agent.ledger=[{**row,"seq":i+1} for i in range(34)]
        self.agent.second_page=threading.Event()
        self.agent.resume=threading.Event()
        self.call("start","early","--task-id","job","--sources","agent_state","agent_trace")
        with ThreadPoolExecutor(max_workers=1) as pool, redirect_stdout(io.StringIO()):
            future=pool.submit(cli.watch,self.rec,"early",self.url,wait_for_task=False,finish=True,interval=.01,timeout=5)
            try:
                self.assertTrue(self.agent.second_page.wait(2))
                receipt=self.call("stop","early")
            finally:
                self.agent.resume.set()
            self.assertEqual(future.result(timeout=2)["task_status"],"STOPPED")
        self.assertEqual(receipt["counts"]["agent_trace"],agent_trace.PAGE_SIZE)
        self.assertEqual(receipt["latest"]["agent_trace"]%1_000_000_000,1)

    def test_optional_metrics_use_bounded_projected_fields(self):
        task={"id":"job"}
        row=dict(task_id="job",seq=1,step=0,kind="tool.error",utc="2026-11-19T02:00:00Z")
        trace={"duration_ms":12.4,"diagnosis":{"error_type":"ProviderTimeout","message":"private"}}
        def project(trace):
            return agent_trace.page({**task,"events":[{**row,"payload":{"trace":trace}}]},0)[0][0]
        event=project(trace)
        self.assertEqual((event["duration_ms"],event["error_type"]),(12.4,"ProviderTimeout"))
        self.assertNotIn("private",json.dumps(event))
        for duration in (float("nan"),-1,True,2**54):
            self.assertNotIn("duration_ms",project({"duration_ms":duration}))
        self.assertNotIn("error_type",project({"diagnosis":{"error_type":"arbitrary free text"}}))


if __name__=="__main__":
    unittest.main()
