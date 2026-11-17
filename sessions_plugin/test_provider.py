"""Default Session plugin: real HTTP facts, provenance and replacement boundaries."""
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
from sessions_plugin.provider import Handler, SessionProvider
from sessions_plugin.source import SessionSource


class FactsHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def do_GET(self):
        fixture = self.server.fixture
        url = urlsplit(self.path)
        params = {key:value[0] for key,value in parse_qs(url.query).items()}
        fixture.calls.append(("GET",url.path,params))
        if fixture.delay:
            time.sleep(fixture.delay)
        if fixture.unavailable==url.path:
            self.send_error(503)
            return
        if url.path=="/api/task-context":
            task = fixture.tasks.get(params.get("task_id"))
            if "trace_id" in params:
                task = next((task for task in reversed(list(fixture.tasks.values())) if task["trace_id"]==params["trace_id"]),None)
            if fixture.context_unavailable:
                task = None
            scope = {"task_id":task["id"] if task else params.get("task_id"),
                     "trace_id":task["trace_id"] if task else params.get("trace_id")}
            related = [{key:t[key] for key in ("id","text","state")} for t in fixture.tasks.values()
                       if task and t["trace_id"]==task["trace_id"] and t["id"]!=task["id"]]
            body = {"schema_version":1,"scope":scope,"task":task,"related_tasks":related[:8],
                    "related_tasks_has_more":len(related)>8,"task_available":bool(task),
                    "notes":["任务账本暂不可读"] if fixture.context_unavailable else []}
        else:
            def match(row):
                if "task_id" in params and params["task_id"] not in (row.get("task_id"),row.get("mission_id")):
                    return False
                if "trace_id" in params and row.get("trace_id")!=params["trace_id"]:
                    return False
                if any(row.get(key)!=params[key] for key in ("source","kind") if key in params):
                    return False
                return not any(row["kind"].startswith(prefix) for prefix in params.get("exclude_kind_prefixes","").split(",") if prefix)
            all_rows = [row for row in fixture.events if match(row)]
            cursor = max((row["seq"] for row in fixture.events),default=0)
            if url.path=="/api/event-stats":
                body = {"archive_id":fixture.archive,"cursor":cursor,"pruned_through_seq":fixture.pruned,
                        "count":len(all_rows),"first_seq":all_rows[0]["seq"] if all_rows else None,
                        "last_seq":all_rows[-1]["seq"] if all_rows else None,
                        "public_payload_bytes":sum(len(json.dumps(row["data"]).encode())+len(row["detail"].encode()) for row in all_rows)}
                if fixture.rotate:
                    fixture.archive="new-archive"
            else:
                before,after = int(params.get("before",2**63)),int(params.get("after",0))
                rows = [row for row in all_rows if after<row["seq"]<before]
                if "after" not in params:
                    rows.reverse()
                limit = int(params.get("limit",80))
                body = {"archive_id":fixture.archive,"cursor":cursor,"pruned_through_seq":fixture.pruned,
                        "events":rows[:limit],"has_more":len(rows)>limit}
        if fixture.malformed==url.path:
            body = {"schema_version":1,"scope":[]}
        raw = json.dumps(body,ensure_ascii=False).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
            pass


class SessionPluginTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/"only-plugin-state"/"module-events.sqlite"
        self.tasks,self.events,self.calls = {},[],[]
        self.archive,self.pruned = "archive-1",0
        self.context_unavailable,self.rotate,self.delay,self.malformed,self.unavailable = False,False,0,None,None
        upstream = ThreadingHTTPServer(("127.0.0.1",0),FactsHandler)
        upstream.fixture = self
        self.launch(upstream)
        self.source = SessionSource(f"http://127.0.0.1:{upstream.server_port}")
        self.provider = SessionProvider(self.source,self.path)
        self.addCleanup(self.provider.close)
        http = ThreadingHTTPServer(("127.0.0.1",0),partial(Handler,provider=self.provider))
        self.launch(http)
        self.url = f"http://127.0.0.1:{http.server_port}/session"
        self.counter = 0

    def launch(self,server):
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def task(self,key="route",state="BLOCKED",trace="work",steps=None):
        self.tasks[key] = {"id":key,"text":"前往门口巡检","state":state,"detail":"原任务结果",
                           "created":"2026-01-01T00:00:00Z","updated":"2026-01-01T00:01:00Z","provider":"local",
                           "trace_id":trace,"steps":steps if steps is not None else [{"tool":"inspect","args":{"points":[[2,1]]},"status":"RUNNING"}]}

    def event(self,kind,**values):
        row = {"seq":len(self.events)+1,"event_id":"event-"+str(len(self.events)+1),"kind":kind,
               "utc":"2026-01-01T00:00:30Z","source":"mission_agent/tasks","module":"agent","source_id":"agent-source",
               "task_id":"route","trace_id":"work","mission_id":"route","operation_id":None,"level":"info",
               "code":kind,"detail":"fixture","data":{},**values}
        self.events.append(row)
        return row["seq"]

    def request(self,**scope):
        self.counter += 1
        return {"schema_version":1,"request_id":"request-"+str(self.counter),"plugin_id":"session.default",
                "plugin_version":"1","binding":"a"*64,**(scope or {"task_id":"route"})}

    def fetch(self,request=None):
        request = request or self.request()
        wire = Request(self.url,data=json.dumps(request).encode(),headers={"Content-Type":"application/json"})
        try:
            response = urlopen(wire,timeout=8)
        except HTTPError as error:
            response = error
        with response:
            reply = json.load(response)
            for field in ("schema_version","request_id","plugin_id","plugin_version","binding"):
                self.assertEqual(reply.get(field),request.get(field))
            return response.status,reply

    def document(self,**scope):
        status,result = self.fetch(self.request(**scope))
        self.assertEqual(status,200,result)
        return result["document"]

    def test_braking_story_and_more_than_thousand_audits_preserve_true_sources(self):
        self.task()
        self.event("agent.accepted")
        self.event("agent.planned")
        braking = self.event("action.braking",source="edge_core/ledger",module="gateway",data={"front_range_m":0.395},level="warn")
        stopped = self.event("action.result",source="edge_core/ledger",code="navigation.path_blocked",
                             detail="stationary odometry confirmed [code:navigation.path_blocked]")
        blocked = self.event("agent.blocked",level="warn",code="navigation.path_blocked")
        self.event("camera.unknown_fault",source="journal:camera",module="camera",level="error",detail="相机遮挡")
        before = self.document()
        for i in range(1001):
            self.event("session.query_completed" if i%2 else "evidence.query_failed",
                       source="journal:query",module="session.default" if i%2 else "evidence.query",level="error",detail="audit noise")
        after = self.document()
        self.assertEqual(before["messages"],after["messages"])
        self.assertIn("0.395",after["markdown"])
        self.assertIn("停稳",after["markdown"])
        self.assertIn("相机遮挡",after["markdown"])
        self.assertNotIn("audit noise",after["markdown"])
        self.assertEqual(after["coverage"]["archived_events"],1007)
        refs = {ref["seq"] for message in after["messages"] for ref in message["refs"]}
        self.assertTrue({braking,stopped,blocked}<=refs)
        self.assertEqual(after["diagnosis"]["code"],"navigation.path_blocked")
        self.assertEqual(after["coverage"]["loaded_events"],6)
        self.assertTrue(all(call[0]=="GET" for call in self.calls))
        self.assertEqual({call[1] for call in self.calls},{"/api/task-context","/api/event-stats","/api/events"})

    def test_owner_guards_and_noise_window_pin_terminal_and_plan(self):
        self.task(state="SUCCEEDED",steps=[{"tool":"status","status":"SUCCEEDED"}])
        accepted = self.event("agent.accepted")
        planned = self.event("agent.planned")
        terminal = self.event("agent.verified")
        for i in range(550):
            self.event("sensor.custom_failure",source="journal:camera",module="camera",level="error",detail="failure "+str(i))
        self.event("agent.blocked",source="journal:foreign",module="foreign",level="warn",detail="非任务所有者的独立告警")
        self.event("agent.planned",source="journal:foreign",module="foreign",data={"payload":{"steps":[{"tool":"invented_tool"}]}})
        document = self.document()
        self.assertEqual(document["task"]["state"],"SUCCEEDED")
        self.assertIn("已完成步骤 1 / 1",document["headline"])
        self.assertTrue(document["diagnosis"]["historical"])
        self.assertEqual(document["messages"][-1]["refs"][0]["seq"],terminal)
        self.assertNotIn("invented_tool",document["markdown"])
        refs = {ref["seq"] for message in document["messages"] for ref in message["refs"]}
        self.assertTrue({accepted,planned,terminal}<=refs)
        self.assertTrue(document["coverage"]["truncated"])
        self.assertTrue(document["coverage"]["window_has_more"])
        self.assertLessEqual(document["coverage"]["loaded_events"],500)
        self.assertLess(len(document["messages"]),12)

    def test_distinct_sources_reasons_operations_and_repeated_facts(self):
        self.task(state="RUNNING")
        common = {"kind":"device.warning","level":"warn","code":"device.unavailable","operation_id":"route/step/1"}
        self.event(**common,source="journal:gripper",module="gripper",detail="夹爪温度过高")
        gripper = self.event(**common,source="journal:gripper",module="gripper",detail="夹爪温度过高")
        camera = self.event(**common,source="journal:camera",module="camera",detail="相机遮挡")
        timeout = self.event(**common,source="journal:camera",module="camera",detail="相机采样超时")
        self.event("agent.retry",level="warn",operation_id="route/step/1")
        self.event("agent.retry",level="warn",operation_id="route/step/2")
        self.event("agent.retry",level="warn",operation_id="route/step/2")
        document = self.document()
        rows = {message["text"]:message for message in document["messages"]}
        self.assertEqual(rows["夹爪温度过高"]["occurrences"],2)
        self.assertEqual(rows["夹爪温度过高"]["refs"][0]["seq"],gripper)
        self.assertEqual(rows["相机遮挡"]["refs"][0]["seq"],camera)
        self.assertEqual(rows["相机采样超时"]["refs"][0]["seq"],timeout)
        retries = [message for message in document["messages"] if message["text"]=="曾因暂时问题进入重试。"]
        self.assertEqual([message["occurrences"] for message in retries],[1,2])

    def test_foreign_same_name_terminal_cannot_replace_blocked_owner(self):
        self.task()
        owner = self.event("agent.blocked",level="warn",code="navigation.path_blocked")
        foreign = self.event("agent.blocked",source="journal:custom",module="custom",level="warn",detail="独立模块另一个告警",code="custom.failure")
        document = self.document()
        self.assertEqual(document["diagnosis"]["code"],"navigation.path_blocked")
        self.assertEqual(document["messages"][-1]["refs"][0]["seq"],owner)
        self.assertTrue(any(foreign in [ref["seq"] for ref in message["refs"]] for message in document["messages"]))

    def test_missing_cancelled_unknown_and_trace_related_tasks(self):
        status,reply = self.fetch()
        self.assertEqual((status,reply["code"]),(404,"session.not_found"))
        self.context_unavailable = True
        status,reply = self.fetch()
        self.assertEqual((status,reply["code"]),(503,"session.source_unavailable"))
        self.event("sensor.custom_failure",level="error",detail="设备未响应")
        orphan = self.document()
        self.assertFalse(orphan["coverage"]["task_available"])
        self.assertIn("设备未响应",orphan["markdown"])
        self.context_unavailable = False
        self.task(state="CANCELED")
        self.task("followup",state="RUNNING")
        cancelled = self.document()
        self.assertIn("任务账本已记录取消",cancelled["headline"])
        selected = self.document(trace_id="work")
        self.assertEqual(selected["scope"],{"task_id":"followup","trace_id":"work"})
        self.assertEqual(selected["related_tasks"][0]["id"],"route")

    def test_missing_session_does_not_become_a_task_when_query_audit_is_imported(self):
        imported = 0

        def import_query_audit():
            nonlocal imported
            rows = self.provider.journal.db.execute(
                "SELECT seq,event FROM module_bus_events WHERE seq>? ORDER BY seq",(imported,)).fetchall()
            for seq,raw in rows:
                event = json.loads(raw)
                kind = event.pop("kind")
                self.event(kind,source="journal:session.default",**event)
                imported = seq

        # Reproduce the normal asynchronous module-journal -> Console import.
        # Repeated reads must not manufacture a session from their own audit.
        for unavailable,expected in ((False,404),(False,404),(True,503),(True,503)):
            self.context_unavailable = unavailable
            status,reply = self.fetch()
            self.assertEqual(status,expected,reply)
            self.assertEqual(reply["code"],"session.source_unavailable" if unavailable else "session.not_found")
            import_query_audit()
        self.assertFalse(self.tasks)
        self.assertEqual(len(self.events),8)
        self.event("sensor.custom_failure",source="journal:sensor",module="sensor",level="error",detail="设备未响应")
        orphan = self.document()
        self.assertEqual(orphan["task"]["state"],"UNKNOWN")
        self.assertFalse(orphan["coverage"]["task_available"])
        self.assertEqual(orphan["coverage"]["loaded_events"],1)
        self.assertEqual(orphan["coverage"]["archived_events"],9)
        self.assertIn("设备未响应",orphan["markdown"])

    def test_protocol_timeout_source_change_and_retention_coverage(self):
        self.task()
        self.event("agent.accepted")
        for change in ({"trace_id":"work"},{"plugin_version":"2"},{"schema_version":2},{"binding":"invalid"},{"task_id":""}):
            request = {**self.request(),**change}
            status,_ = self.fetch(request)
            self.assertEqual(status,400)
        self.assertEqual(self.calls,[])
        self.rotate = True
        status,reply = self.fetch()
        self.assertEqual((status,reply["code"]),(503,"session.archive_changed"))
        self.rotate = False
        self.pruned = 1
        document = self.document()
        self.assertEqual(document["coverage"]["pruned_through_seq"],1)
        self.assertTrue(any("历史清理" in note for note in document["coverage"]["notes"]))
        self.delay = 1.8
        started = time.monotonic()
        status,reply = self.fetch()
        self.assertEqual(status,503)
        self.assertLess(time.monotonic()-started,2.5)

    def test_invalid_source_and_audit_failure_never_deliver_document(self):
        self.task()
        for endpoint in ("/api/task-context","/api/event-stats","/api/events"):
            self.malformed = endpoint
            status,reply = self.fetch()
            self.assertEqual(status,503,reply)
            self.assertNotIn("document",reply)
        self.malformed = None
        calls = len(self.calls)
        with patch.object(self.provider.journal,"emit",side_effect=sqlite3.OperationalError("disk full")):
            status,reply = self.fetch()
        self.assertEqual((status,reply["code"]),(503,"session.audit_unavailable"))
        self.assertEqual(len(self.calls),calls)

    def test_independent_lifecycle_identity_and_only_owned_sqlite(self):
        self.task()
        self.event("agent.accepted")
        document = self.document()
        before = json.dumps(self.tasks),json.dumps(self.events)
        replacement = SessionProvider(self.source,self.path.parent/"replacement.sqlite",plugin_id="session.replacement",version="2")
        try:
            request = {**self.request(),"plugin_id":"session.replacement","plugin_version":"2"}
            status,reply = replacement.invoke(request)
            self.assertEqual(status,200)
            self.assertEqual(reply["document"]["headline"],document["headline"])
            self.assertEqual(reply["plugin_id"],"session.replacement")
        finally:
            replacement.close()
        self.assertEqual((json.dumps(self.tasks),json.dumps(self.events)),before)
        reopened = SessionProvider(self.source,self.path)
        try:
            status,reply = reopened.invoke(self.request())
            self.assertEqual(status,200)
            facts = [json.loads(row[0]) for row in reopened.journal.db.execute("SELECT event FROM module_bus_events ORDER BY seq")]
            self.assertEqual([fact["kind"] for fact in facts],
                             ["session.query_requested","session.query_completed"]*2)
            self.assertEqual(facts[-1]["parent_event_id"],facts[-2]["event_id"])
        finally:
            reopened.close()
        self.assertEqual({path.name for path in self.path.parent.iterdir()},{"module-events.sqlite","replacement.sqlite"})
        self.assertEqual(self.source.url.scheme,"http")


if __name__=="__main__":
    unittest.main()
