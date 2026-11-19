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


if __name__=="__main__":
    unittest.main()
