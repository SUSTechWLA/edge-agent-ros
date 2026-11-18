"""SDD-017. Durable design handoff, crash windows, isolation, and a bounded model budget."""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"client"))
from test_plugin_author_watch import Fixture
import plugin_design as design
from plugin_design_watch import DesignWatch, FILTER


class Interrupted(BaseException):
    """Inject sudden loss of the process before its next durable write."""


class TestDesignWatch(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.decisions = self.root/"decisions"
        self.decisions.mkdir()
        self.source = Fixture()
        self.addCleanup(self.source.close)
        self.watch = None
        self.addCleanup(lambda:self.watch and self.watch.close())

    def open(self, **kwargs):
        if self.watch:
            self.watch.close()
        self.watch = DesignWatch(self.source.url,self.decisions,self.root/"private",
                                 self.root/"public/plugin.design/module-events.sqlite",retry_delay=0,**kwargs)
        return self.watch

    def add(self, task):
        seq = len(self.source.events)+1
        parent,trace = "blocked-"+task,"trace-"+task
        source = {"archive_id":"original","seq":seq,"event_id":parent,"task_id":task,"trace_id":trace,
                  "reason_code":"planner.capability_missing"}
        receipt = {"status":"declined","reason_code":"proposal_rejected","source":source,
                   "model":{"author_protocol":"ollama.chat","request":"private goal for "+task,
                            "source_task":{k:source[k] for k in ("task_id","trace_id","reason_code")}}}
        receipt_id = design.sha(design.json_bytes(["original",seq]))
        raw = design.json_bytes(receipt)
        path = self.decisions/(receipt_id+".json")
        path.write_bytes(raw)
        row = dict(FILTER,task_id=task,trace_id=trace,seq=seq,event_id="declined-"+task,parent_event_id=parent,
                   data={"archive_id":"original","source_seq":seq,"receipt_id":receipt_id,
                         "receipt_sha256":design.sha(raw)})
        self.source.events.append(row)
        return path,row

    def drain(self):
        for _ in range(40):
            more, _ = self.watch.poll()
            if not more:
                return
        self.fail("design subscription failed to drain")

    def events(self,kind):
        return [json.loads(r[0]) for r in self.watch.journal.db.execute(
            "SELECT event FROM module_bus_events WHERE json_extract(event,'$.kind')=? ORDER BY seq",(kind,))]

    def test_subscription_and_archive_rebuild_preserve_one_design_and_privacy(self):
        self.open()
        _, source = self.add("alpha")
        self.drain()
        completed, = self.events("plugin.design_watch.completed")
        drafted, = self.events("plugin.design.drafted")
        requested, = self.events("plugin.design.requested")
        self.assertEqual(requested["parent_event_id"],source["event_id"])
        self.assertEqual(completed["parent_event_id"],drafted["event_id"])
        self.assertEqual(completed["trace_id"],source["trace_id"])
        private, = (self.root/"private/drafts").glob("*.json")
        self.assertEqual(design.sha(private.read_bytes()),drafted["data"]["draft_sha256"])
        self.assertEqual(json.loads(private.read_bytes())["goal"],"private goal for alpha")
        self.assertNotIn("private goal",str(self.watch.journal.db.execute("SELECT event FROM module_bus_events").fetchall()))
        self.open()
        page = self.watch.sink.page
        with patch.object(self.watch.sink,"page",side_effect=lambda a,n:dict(page(a,n),archive_id="restored")):
            self.drain()
        self.assertEqual(len(self.events("subscription.archive_changed")),1)
        self.assertEqual(len(self.events("plugin.design_watch.completed")),1)
        self.assertEqual(len(self.events("plugin.design.drafted")),1)

    def test_crash_after_draft_file_and_after_fact_uses_last_attempt_to_reconcile(self):
        self.open(attempts=1)
        self.add("saved-file")
        emit = design.emit
        def fail_before_fact(journal,stage,*args,**kwargs):
            if stage=="drafted":
                raise Interrupted()
            return emit(journal,stage,*args,**kwargs)
        with patch.object(design,"emit",side_effect=fail_before_fact),self.assertRaises(Interrupted):
            self.watch.poll()
        self.open(attempts=1)
        self.drain()
        self.assertEqual(len(self.events("plugin.design.drafted")),1)
        self.add("saved-fact")
        deliver = self.watch.emit
        def lose_ack(stage,*args,**kwargs):
            if stage=="completed":
                raise Interrupted()
            return deliver(stage,*args,**kwargs)
        with patch.object(self.watch,"emit",side_effect=lose_ack),self.assertRaises(Interrupted):
            self.watch.poll()
        self.open(attempts=1)
        self.drain()
        for kind in ("plugin.design_watch.attempt","plugin.design_watch.completed","plugin.design.drafted",
                     "plugin.design.requested"):
            self.assertEqual(len(self.events(kind)),2,kind)
        self.assertFalse(self.events("plugin.design_watch.intervention_required"))

    def test_legacy_queue_survives_two_readers_using_one_state_directory(self):
        self.open()
        self.add("legacy")
        self.watch.sink.copy()
        self.watch.intake()
        with self.watch.db:
            self.watch.db.execute("DROP TABLE worker_owner")
        self.watch.close()
        self.watch=None
        def poll():
            with closing(DesignWatch(self.source.url,self.decisions,self.root/"private",
                         self.root/"public/plugin.design/module-events.sqlite")) as consumer:
                consumer.poll()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(poll) for _ in range(2)]
            for future in futures:
                future.result(10)
        self.open()
        self.assertEqual(len(self.events("plugin.design_watch.completed")),1)
        self.assertEqual(len(self.events("plugin.design_watch.attempt")),1)
        self.assertIsNotNone(self.watch.journal.db.execute("SELECT pulse FROM module_bus_heartbeat").fetchone())

    def test_model_crash_and_transient_errors_have_a_durable_budget(self):
        self.open(attempts=2,model_url=self.source.url,model="fixture")
        self.add("timeout")
        with patch.object(design,"propose",side_effect=Interrupted) as first,self.assertRaises(Interrupted):
            self.watch.poll()
        self.open(attempts=2,model_url=self.source.url,model="fixture")
        with patch.object(design,"propose",side_effect=design.DesignError("model_unavailable")) as second:
            self.drain()
        self.assertEqual((first.call_count,second.call_count),(1,1))
        retry, = self.events("plugin.design_watch.retry")
        self.assertEqual(retry["data"]["reason"],"attempt_interrupted")
        handoff, = self.events("plugin.design_watch.intervention_required")
        self.assertEqual(handoff["data"]["attempt"],2)
        self.assertEqual(handoff["data"]["reason"],"model_unavailable")
        self.assertTrue(handoff["data"]["steps"])
        self.assertFalse(self.events("plugin.design.drafted"))
        self.open(attempts=8,model_url=self.source.url,model="fixture")
        with patch.object(design,"propose",side_effect=AssertionError("unexpected new model attempt")):
            self.drain()
        self.assertEqual(len(self.events("plugin.design_watch.intervention_required")),1)

    def test_missing_tampered_and_untrusted_receipts_are_isolated(self):
        self.open(attempts=2)
        self.add("missing")[0].unlink()
        self.add("changed")[0].write_bytes(b"{}")
        _, unsafe = self.add("path")
        unsafe["data"]["receipt_id"]="../unsafe"
        path,row = self.add("missing-goal")
        receipt = json.loads(path.read_bytes())
        del receipt["model"]
        raw = design.json_bytes(receipt)
        path.write_bytes(raw)
        row["data"]["receipt_sha256"] = design.sha(raw)
        self.add("healthy")
        self.drain()
        failures = self.events("plugin.design_watch.intervention_required")
        self.assertEqual({e["data"]["reason"] for e in failures},
                         {"receipt_missing","receipt_digest_mismatch","receipt_id_invalid","goal_unavailable"})
        self.assertEqual([e["task_id"] for e in self.events("plugin.design_watch.completed")],["healthy"])
        self.assertEqual(len(self.events("plugin.design_watch.retry")),1)

    def test_slow_streams_do_not_monopolize_model_or_archive_reader(self):
        stopped=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass

            def drip(self):
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(100):
                        if stopped.wait(.035):
                            break
                        self.wfile.write(b" ")
                        self.wfile.flush()
                except OSError:
                    pass

            do_GET=drip

            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "slow" in body["messages"][1]["content"]:
                    return self.drip()
                fields={k:"concrete field" for k in design.DESIGN_FIELDS}
                fields["interface"]="Tool"
                wire=json.dumps({"done":True,"message":{"content":json.dumps(fields)}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(wire)
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.05})
        thread.start()
        try:
            endpoint="http://127.0.0.1:"+str(server.server_port)
            self.open(attempts=1,model_url=endpoint,model="fixture")
            self.add("slow")
            self.add("fast")
            with patch("plugin_design.MODEL_TIMEOUT_SECONDS",.18):
                start=time.monotonic()
                self.watch.poll()
                self.assertLess(time.monotonic()-start,2)
            deadline,=self.events("plugin.design.failed")
            self.assertEqual(deadline["data"]["transport_reason"],"deadline")
            self.watch.sink.url=endpoint
            with patch("event_sink.PAGE_TIMEOUT_SECONDS",.18):
                start=time.monotonic()
                self.assertEqual(self.watch.poll(),(True,True))
                self.assertLess(time.monotonic()-start,2)
            self.assertEqual(self.events("subscription.blocked")[0]["data"]["reason"],"subscription.deadline")
            self.assertEqual(self.events("plugin.design_watch.completed")[0]["task_id"],"fast")
        finally:
            stopped.set()
            server.shutdown()
            thread.join()
            server.server_close()

    def test_one_conflicting_source_and_one_offline_interval_are_visible(self):
        self.open()
        _,row = self.add("first")
        self.add("second")
        self.watch.poll()
        with patch.object(self.watch.sink,"page",side_effect=URLError("offline")):
            self.assertEqual(self.watch.poll(),(True,True))
            self.assertEqual(self.watch.poll(),(False,True))
        self.assertEqual(len(self.events("plugin.design_watch.completed")),2)
        self.source.events[:] = [dict(row,seq=1,task_id="conflict")]
        page = self.watch.sink.page
        with patch.object(self.watch.sink,"page",side_effect=lambda a,n:dict(page(a,n),archive_id="rebuilt")):
            self.drain()
        self.assertEqual(len(self.events("plugin.design_watch.source_conflict")),1)
        self.assertEqual(len(self.events("subscription.blocked")),1)
        self.assertEqual(len(self.events("subscription.restored")),1)
        self.assertEqual(len(self.events("plugin.design.drafted")),2)


if __name__=="__main__":
    unittest.main()
