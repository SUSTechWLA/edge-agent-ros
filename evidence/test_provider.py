"""SDD-010 public-HTTP contract and adversarial SQL/evidence boundaries."""
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evidence.provider import EvidenceProvider, Handler, MAX_RESULT_BYTES
from evidence.public_source import PublicSource


def event(seq, task="target", **changes):
    return {"seq":seq,"event_id":f"event-{seq}","task_id":task,"mission_id":task,
            "trace_id":"trace-target","utc":"2026-11-17T00:00:00Z","kind":"gateway.blocked",
            "code":"navigation.obstacle","level":"warn","detail":"前方净空不足，已制动",
            "data":{"nearest_m":0.18},**changes}


class Upstream(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def do_GET(self):
        source = self.server.fixture
        url = urlsplit(self.path)
        query = {key:value[0] for key,value in parse_qs(url.query).items()}
        source.calls.append(("GET",url.path,query))
        if source.fail:
            self.send_error(503)
            return
        if source.delay:
            time.sleep(source.delay)
        if url.path=="/api/session":
            task = query["task_id"]
            if task=="absent":
                self.send_error(404)
                return
            data = {"scope":{"task_id":task,"trace_id":"trace-target"},
                    "markdown":"# 任务复盘\n巡检因前方净空不足受阻；机器人已制动。",
                    "coverage":{"archive_id":"archive-1","through_seq":2,"truncated":False},
                    "messages":[{"role":"result","text":"not duplicated","refs":[{"seq":1,"event_id":"event-1"}]}],
                    "private_debug":"NOT-PUBLIC-SESSION-FIELD"}
            if source.session_payload is not None:
                data = source.session_payload
        else:
            after,before = int(query["after"]),int(query.get("before",2**63-1))
            limit = int(query["limit"])
            rows = [row for row in source.rows if (row["task_id"]==query["task_id"]
                    or row["mission_id"]==query["task_id"]+"/step/1") and after<row["seq"]<before]
            data = {"archive_id":source.archive,"cursor":source.rows[-1]["seq"] if source.rows else 0,
                    "events":rows[:limit],"has_more":len(rows)>limit,"pruned_through_seq":source.pruned}
            if source.rotate:
                source.archive="archive-2"
            if source.empty_more:
                data.update(events=[],has_more=True)
        raw = json.dumps(data,ensure_ascii=False).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
            pass


class EvidenceContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/"module-events.sqlite"
        self.rows = [event(1),event(2,task="unrelated")]
        self.calls = []
        self.archive = "archive-1"
        self.fail = self.rotate = self.empty_more = False
        self.delay = 0
        self.pruned = 0
        self.session_payload = None
        upstream = ThreadingHTTPServer(("127.0.0.1",0),Upstream)
        upstream.fixture = self
        self.launch(upstream)
        self.source = PublicSource(f"http://127.0.0.1:{upstream.server_port}")
        self.provider = EvidenceProvider(self.source,self.path)
        self.addCleanup(lambda:self.provider.close())
        http = ThreadingHTTPServer(("127.0.0.1",0),partial(Handler,provider=self.provider))
        self.launch(http)
        self.url = f"http://127.0.0.1:{http.server_port}"
        self.counter = 0

    def launch(self, server):
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def body(self,args,op=None):
        self.counter += 1
        return {"operation_id":op or f"query/{self.counter}","version":"1","arguments":args,
                "trace":{"trace_id":"caller-trace","task_id":"caller-task","parent_event_id":"dispatch-1"}}

    def query(self, sql="SELECT seq,code,detail FROM events", **changes):
        return {"task_id":"target","sql":sql,"after_seq":0,"limit":50,"archive_id":"",**changes}

    def invoke(self, tool, args, op=None, route=None):
        body = self.body(args,op)
        request = Request(self.url+(route or "/invoke?tool="+tool),
                          data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
        try:
            response = urlopen(request,timeout=6)
        except HTTPError as error:
            response = error
        with response:
            return response.status,json.load(response)

    def facts(self):
        with self.provider.lock:
            return [json.loads(row[0]) for row in self.provider.journal.db.execute("SELECT event FROM module_bus_events ORDER BY seq")]

    def test_schema_session_scalar_contract_and_durable_receipt(self):
        with urlopen(self.url+"/schema") as response:
            self.assertEqual(json.load(response)["table"],"events")
        status,schema = self.invoke("evidence_schema",{})
        self.assertEqual((status,schema["state"]),(200,"SUCCEEDED"))
        self.assertIn("data_json",schema["evidence"]["columns"])
        self.assertIn("json_extract",schema["evidence"]["allowed_functions"])
        self.assertIn("->>",schema["evidence"]["allowed_functions"])
        self.assertNotIn("load_extension",schema["evidence"]["allowed_functions"])
        self.assertEqual(self.calls,[])
        status,session = self.invoke("read_session",{"task_id":"target"},op="stable",route="/session")
        self.assertEqual((status,session["state"]),(200,"SUCCEEDED"))
        self.assertNotIn("NOT-PUBLIC-SESSION-FIELD",json.dumps(session))
        self.assertNotIn("messages",session["evidence"])
        before = len(self.calls)
        self.assertEqual(self.invoke("read_session",{"task_id":"target"},op="stable")[1],session)
        self.assertEqual(len(self.calls),before)
        reopened = EvidenceProvider(self.source,self.path)
        try:
            self.assertEqual(reopened.invoke("read_session",self.body({"task_id":"target"},"stable"))[1],session)
        finally:
            reopened.close()
        facts = self.facts()
        self.assertEqual(len(facts),4)
        self.assertEqual(facts[-1]["parent_event_id"],facts[-2]["event_id"])
        self.assertEqual(facts[-1]["trace_id"],"caller-trace")
        self.assertEqual(facts[-1]["task_id"],"caller-task")

    def test_source_timeout_and_concurrent_operation_identity(self):
        self.delay = 1.8
        started = time.monotonic()
        status,result = self.invoke("read_session",{"task_id":"target"},op="delayed")
        self.assertEqual((status,result["error"]["code"]),(503,"evidence.source_unavailable"))
        self.assertLess(time.monotonic()-started,2.5)
        self.delay = 0
        ready = threading.Semaphore(0)
        gate = threading.Event()
        results = []
        def pause(task,deadline):
            ready.release()
            if not gate.wait(3):
                raise AssertionError("test failed to release source read")
            return {"markdown":"completed","coverage":{},"refs":[],"truncated":False}
        with patch.object(self.source,"session",side_effect=pause):
            first_body = self.body({"task_id":"target"},"simultaneous-a")
            jobs = [threading.Thread(target=lambda body=body: results.append(self.provider.invoke("read_session",body)))
                    for body in (first_body,self.body({"task_id":"target"},"simultaneous-b"))]
            try:
                jobs[0].start()
                self.assertTrue(ready.acquire(timeout=1))
                duplicate = self.provider.invoke("read_session",first_body)
                self.assertEqual(duplicate[1]["error"]["code"],"evidence.busy")
                jobs[1].start()
                self.assertTrue(ready.acquire(timeout=1))
                status,busy = self.provider.invoke("read_session",self.body({"task_id":"target"},"simultaneous-c"))
                self.assertEqual((status,busy["error"]["code"]),(503,"evidence.busy"))
            finally:
                gate.set()
                for job in jobs:
                    if job.ident is not None:
                        job.join(timeout=2)
        self.assertEqual([result[0] for result in results],[200,200])

    def test_public_task_scope_parameterized_json_and_audit(self):
        sql = "SELECT seq,json_extract(data_json,'$.nearest_m') AS metres FROM events WHERE task_id=:task_id"
        status,result = self.invoke("query_evidence",self.query(sql),op="specific")
        self.assertEqual((status,result["state"]),(200,"SUCCEEDED"))
        self.assertEqual(result["evidence"]["rows"],[[1,0.18]])
        self.assertEqual(result["evidence"]["refs"][0]["event_id"],"event-1")
        self.assertEqual(self.calls[0][1:],("/api/events",{"task_id":"target","after":"0","limit":"100"}))
        _,conflict = self.invoke("query_evidence",self.query("SELECT count(*) FROM events"),op="specific")
        self.assertEqual(conflict["error"]["code"],"evidence.operation_conflict")
        facts = self.facts()
        self.assertEqual(len(facts),2)
        self.assertNotIn(sql,json.dumps(facts))
        self.assertEqual(facts[-1]["data"]["coverage"]["loaded_events"],1)

    def test_json_arrow_and_delegated_events_without_task_id(self):
        self.rows = [event(1,kind="agent.dispatch",operation_id="target/step/1"),
                     event(2,task=None,mission_id="target/step/1",kind="action.braking",data={"front_range_m":0.18}),
                     event(3,task="unrelated",kind="action.braking",data={"front_range_m":7.5})]
        _,kinds = self.invoke("query_evidence",self.query("SELECT kind,count(*) FROM events GROUP BY kind"))
        self.assertEqual(kinds["evidence"]["rows"],[["action.braking",1],["agent.dispatch",1]])
        _,reading = self.invoke("query_evidence",self.query(
            "SELECT seq,data_json->>'front_range_m' AS distance FROM events WHERE kind='action.braking'"))
        self.assertEqual(reading["evidence"]["rows"],[[2,0.18]])

    def test_window_pagination_fixed_cutoff_and_archive_identity(self):
        self.rows = [event(seq) for seq in range(1,522)]
        _,first = self.invoke("query_evidence",self.query("SELECT count(*) FROM events"))
        evidence = first["evidence"]
        self.assertEqual(evidence["rows"],[[500]])
        self.assertEqual(evidence["coverage"]["next_after"],500)
        self.assertTrue(evidence["coverage"]["has_more"])
        self.assertEqual(len(self.calls),5)
        self.assertTrue(all(call[2]["before"]=="522" for call in self.calls[1:]))
        _,second = self.invoke("query_evidence",self.query("SELECT count(*) FROM events",after_seq=500,archive_id="archive-1"))
        self.assertEqual(second["evidence"]["rows"],[[21]])
        self.assertIsNone(second["evidence"]["coverage"]["next_after"])
        _,missing = self.invoke("query_evidence",self.query(after_seq=500))
        self.assertEqual(missing["error"]["code"],"evidence.invalid_request")
        self.archive = "replacement"
        _,changed = self.invoke("query_evidence",self.query(after_seq=500,archive_id="archive-1"))
        self.assertEqual(changed["error"]["code"],"evidence.archive_changed")

    def test_change_during_load_and_invalid_progress_never_succeed(self):
        self.rows = [event(seq) for seq in range(1,102)]
        self.rotate = True
        _,result = self.invoke("query_evidence",self.query())
        self.assertEqual(result["error"]["code"],"evidence.archive_changed")
        self.rotate = False
        self.empty_more = True
        _,result = self.invoke("query_evidence",self.query())
        self.assertEqual(result["error"]["code"],"evidence.source_invalid")

    def test_malformed_session_is_audited_and_retention_gap_is_disclosed(self):
        valid = {"scope":{"task_id":"target"},"markdown":"ok","coverage":{}}
        for changed in ({"scope":[]},{"messages":[None]},{"messages":[{"refs":[None]}]},
                        {"messages":{}},{"scope":{"task_id":"wrong"}}):
            self.session_payload = {**valid,**changed}
            status,result = self.invoke("read_session",{"task_id":"target"})
            self.assertEqual((status,result["error"]["code"]),(503,"evidence.source_invalid"))
            self.assertEqual(self.facts()[-1]["kind"],"evidence.query_failed")
        self.pruned = 1
        _,result = self.invoke("query_evidence",self.query())
        coverage = result["evidence"]["coverage"]
        self.assertEqual(coverage["pruned_through_seq"],1)
        self.assertTrue(coverage["retention_gap_possible"])
        self.assertTrue(any("历史清理" in note for note in coverage["notes"]))

    def test_sql_write_file_system_schema_and_functions_are_denied(self):
        blocked = ["DELETE FROM events", "SELECT * FROM sqlite_master", "PRAGMA database_list",
                   "ATTACH DATABASE '/tmp/not-allowed' AS other", "SELECT load_extension('anything')",
                   "SELECT readfile('/etc/passwd')", "SELECT * FROM pragma_database_list",
                   "SELECT 1; DELETE FROM events", "WITH x AS (SELECT 1) DELETE FROM events",
                   "SELECT randomblob(1000000000)", "SELECT printf('%1000000000s','x')"]
        for sql in blocked:
            with self.subTest(sql=sql):
                _,result = self.invoke("query_evidence",self.query(sql))
                self.assertEqual(result["state"],"BLOCKED")
                self.assertIn(result["error"]["code"],("evidence.sql_denied","evidence.invalid_sql"))
        self.assertEqual(self.calls,[])
        _,valid = self.invoke("query_evidence",self.query("SELECT count(*) FROM events"))
        self.assertEqual(valid["evidence"]["rows"],[[1]])

    def test_recursive_queries_and_large_cells_are_bounded(self):
        sql = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n"
        started = time.monotonic()
        _,result = self.invoke("query_evidence",self.query(sql))
        self.assertEqual(result["error"]["code"],"evidence.query_budget")
        self.assertLess(time.monotonic()-started,2)

    def test_large_model_record_preserves_small_diagnostics_in_same_scope(self):
        self.rows = [event(1,kind="agent.model.request",data={"large":"x"*100000}),event(2)]
        _,count = self.invoke("query_evidence",self.query("SELECT count(*) FROM events"))
        self.assertEqual(count["evidence"]["rows"],[[2]])
        _,distance = self.invoke("query_evidence",self.query(
            "SELECT seq,json_extract(data_json,'$.nearest_m') FROM events WHERE kind='gateway.blocked'"))
        self.assertEqual(distance["evidence"]["rows"],[[2,0.18]])
        _,large = self.invoke("query_evidence",self.query("SELECT data_json FROM events WHERE seq=1"))
        self.assertEqual(large["state"],"SUCCEEDED")
        self.assertEqual(large["evidence"]["rows"],[])
        self.assertTrue(large["evidence"]["truncated"])
        self.assertIn("超过文本预算",large["evidence"]["markdown"])
        self.rows = [event(1,data={"large":"x"*(2*1024*1024)})]
        _,exceeded = self.invoke("query_evidence",self.query())
        self.assertEqual(exceeded["error"]["code"],"evidence.source_budget")

    def test_result_budget_row_limit_empty_scope_and_nonfinite_values(self):
        self.rows = [event(seq,data={"large":"x"*5000}) for seq in range(1,4)]
        _,result = self.invoke("query_evidence",self.query("SELECT seq,data_json FROM events"))
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False,separators=(",",":")).encode()),MAX_RESULT_BYTES)
        self.assertTrue(result["evidence"]["truncated"])
        self.assertEqual(len(result["evidence"]["rows"]),2)
        _,limited = self.invoke("query_evidence",self.query(limit=1))
        self.assertTrue(limited["evidence"]["truncated"])
        self.assertEqual(len(limited["evidence"]["rows"]),1)
        _,empty = self.invoke("query_evidence",self.query(task_id="absent"))
        self.assertEqual(empty["evidence"]["rows"],[])
        self.assertEqual(empty["evidence"]["coverage"]["loaded_events"],0)
        _,invalid = self.invoke("query_evidence",self.query("SELECT 1e999"))
        self.assertEqual(invalid["error"]["code"],"evidence.invalid_result")

    def test_unavailable_retry_and_missing_session(self):
        self.fail = True
        status,failed = self.invoke("read_session",{"task_id":"target"},op="retry")
        self.assertEqual(status,503)
        self.assertEqual(failed["error"]["code"],"evidence.source_unavailable")
        self.fail = False
        _,recovered = self.invoke("read_session",{"task_id":"target"},op="retry")
        self.assertEqual(recovered["state"],"SUCCEEDED")
        self.assertEqual(failed["diagnostic"]["query_id"],recovered["evidence"]["query_id"])
        _,missing = self.invoke("read_session",{"task_id":"absent"})
        self.assertEqual(missing["error"]["code"],"evidence.task_not_found")
        self.assertEqual([fact["kind"] for fact in self.facts()][:4],
                         ["evidence.query_requested","evidence.query_failed"]+
                         ["evidence.query_requested","evidence.query_completed"])

    def test_audit_failure_stops_reads_and_completion_delivery(self):
        with patch.object(self.provider.journal,"emit",side_effect=sqlite3.OperationalError("disk full")):
            status,result = self.invoke("read_session",{"task_id":"target"})
        self.assertEqual((status,result["error"]["code"]),(503,"evidence.audit_unavailable"))
        self.assertEqual(self.calls,[])
        original = self.provider.journal.emit
        count = 0
        def fail_completion(*args,**kwargs):
            nonlocal count
            count += 1
            if count==2:
                raise sqlite3.OperationalError("disk full")
            return original(*args,**kwargs)
        with patch.object(self.provider.journal,"emit",side_effect=fail_completion):
            status,result = self.invoke("read_session",{"task_id":"target"},op="completion")
        self.assertEqual((status,result["error"]["code"]),(503,"evidence.audit_unavailable"))
        self.assertNotIn("evidence",result)
        _,retried = self.invoke("read_session",{"task_id":"target"},op="completion")
        self.assertEqual(retried["state"],"SUCCEEDED")

    def test_scalar_validation_scope_and_redaction(self):
        for change in ({"after_seq":True},{"limit":0},{"limit":1.5},{"archive_id":False},
                       {"task_id":""},{"task_id":"bad?scope"},{"sql":""}):
            with self.subTest(change=change):
                _,result = self.invoke("query_evidence",self.query(**change))
                self.assertEqual(result["state"],"BLOCKED")
        self.assertEqual(self.calls,[])
        self.rows = [event(1,data={"api_key":"not-public","measurement":0.18},detail="Bearer dummyvalue")]
        _,result = self.invoke("query_evidence",self.query("SELECT detail,data_json FROM events"))
        serialized = json.dumps(result)
        self.assertNotIn("not-public",serialized)
        self.assertNotIn("dummyvalue",serialized)
        self.assertIn("0.18",serialized)

    def test_concurrency_is_bounded_and_http_body_is_checked(self):
        self.provider.slots.acquire()
        self.provider.slots.acquire()
        try:
            status,result = self.invoke("read_session",{"task_id":"target"})
            self.assertEqual((status,result["error"]["code"]),(503,"evidence.busy"))
        finally:
            self.provider.slots.release()
            self.provider.slots.release()
        request = Request(self.url+"/query",data=b"{}",headers={"Content-Type":"text/plain"})
        with self.assertRaises(HTTPError) as failure:
            urlopen(request,timeout=2)
        self.assertEqual(failure.exception.code,400)
        failure.exception.close()
        self.assertEqual(self.calls,[])


if __name__=="__main__":
    unittest.main()
