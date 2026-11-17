"""Session service lifecycle and raw source contracts; narrative tests belong to the plugin."""
from contextlib import closing
import json
import socket
import sqlite3
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen
import session_service
import test_run_review


def document(task="route", trace="work"):
    return {"schema_version":1,"scope":{"task_id":task,"trace_id":trace},
            "task":{"id":task,"state":"BLOCKED","text":"巡检"},"headline":"路径受阻。",
            "messages":[{"role":"user","title":"目标","text":"巡检","source":"task_journal","refs":[]}],
            "diagnosis":None,"related_tasks":[],"related_tasks_has_more":False,
            "coverage":{"through_seq":0,"archived_events":0,"selected_events":0,"collapsed_events":0,
                        "public_payload_bytes":0,"notes":[],"truncated":False,"task_available":True},
            "links":{"events":"/api/events?task_id=route","export":"/api/export?task_id=route",
                     "markdown":"/api/session?task_id=route&format=markdown"},"markdown":"# 会话\n路径受阻。\n"}


class SessionTest(unittest.TestCase):
    fetch = test_run_review.RunReviewTest.fetch

    def setUp(self):
        test_run_review.RunReviewTest.setUp(self)
        self.plugins=self.root/"plugins"
        (self.plugins/"runtime").mkdir(parents=True)
        environment=patch.dict("os.environ",{"EDGE_PLUGIN_DIR":str(self.plugins)})
        environment.start()
        self.addCleanup(environment.stop)
        self.calls=[]
        self.response=lambda req:(200,{**req,"document":document()})
        owner=self
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append((self.path,request))
                status,response=owner.response(request)
                if status==0:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                raw=json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(raw)))
                self.end_headers()
                try: self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError): pass
        self.provider=ThreadingHTTPServer(("127.0.0.1",0),Provider)
        worker=threading.Thread(target=self.provider.serve_forever,daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(self.provider.server_close)
        self.addCleanup(self.provider.shutdown)

    def install(self, version="1", name="session.default", draining=False):
        path=self.plugins/"runtime"/(name+".json")
        path.write_text(json.dumps({"schema_version":1,"id":name,"version":version,
            "endpoint":f"http://127.0.0.1:{self.provider.server_port}/read", "provides":["session.read"],
            "requires":[],"draining":draining}),encoding="utf-8")
        return path

    def failure(self, code, status=503, **scope):
        with self.assertRaises(HTTPError) as raised:
            self.fetch("/api/session",**(scope or {"task_id":"route"}))
        self.assertEqual(raised.exception.code,status)
        body=json.load(raised.exception)
        self.assertEqual(body["code"],code)
        return body

    def test_hot_install_replace_withdraw_drain_and_raw_read_remain(self):
        self.failure("session.service_missing")
        path=self.install()
        first=self.fetch("/api/session",task_id="route")
        self.assertEqual(first["service"]["plugin_version"],"1")
        self.assertIn("session.default @ 1",first["markdown"])
        request=self.calls[-1][1]
        self.assertEqual(self.calls[-1][0],"/read")
        self.assertNotIn("endpoint",request)
        self.assertEqual(request["binding"],first["service"]["binding"])
        self.assertEqual(self.fetch("/api/session",trace_id="work")["scope"]["task_id"],"route")
        self.install(version="2")
        second=self.fetch("/api/session",task_id="route")
        self.assertNotEqual(second["service"]["binding"],first["service"]["binding"])
        self.install(version="2",draining=True)
        self.failure("session.draining")
        path.unlink()
        self.failure("session.service_missing")
        seq=self.history.record({"session_id":"run"},kind="fixture",source="test",detail="still here")
        self.assertEqual(self.fetch("/api/events")["events"][0]["seq"],seq)

    def test_service_conflict_and_dependency(self):
        self.install()
        path=self.install(name="session.alternative")
        self.failure("session.service_conflict")
        path.unlink()
        path=self.plugins/"runtime"/"session.default.json"
        manifest=json.loads(path.read_text())
        manifest["requires"]=["missing.service"]
        path.write_text(json.dumps(manifest))
        self.failure("session.service_missing")
        self.assertEqual(self.calls,[])

    def test_mid_request_change_or_withdraw_is_not_mislabeled(self):
        self.install()
        for action in (lambda:self.install(version="2"),lambda:self.install(draining=True),
                       lambda:(self.plugins/"runtime"/"session.default.json").unlink()):
            self.install()
            def changed(request):
                action()
                return 200,{**request,"document":document()}
            self.response=changed
            self.failure("session.binding_changed")

    def test_bad_metadata_scope_and_document_never_reach_consumer(self):
        self.install()
        for field,value in (("request_id","stale"),("binding","old"),("plugin_version","0"),
                            ("plugin_id","other"),("schema_version",True)):
            self.response=lambda req,f=field,v=value:(200,{**req,f:v,"document":document()})
            with self.subTest(field=field): self.failure("session.response_invalid")
        for key,value in (("scope",{"task_id":"other"}),("messages",[None]),("coverage",[]),
                          ("diagnosis",{"historical":True,"refs":[{"seq":True}]}),
                          ("markdown",None),("related_tasks",[None])):
            self.response=lambda req,k=key,v=value:(200,{**req,"document":{**document(),k:v}})
            with self.subTest(field=key): self.failure("session.response_invalid")
        self.response=lambda req:(200,{**req,"document":document(trace="another")})
        self.failure("session.response_invalid",trace_id="work")

    def test_not_found_unavailable_disconnect_size_and_deadline(self):
        self.install()
        self.response=lambda req:(404,{**req,"code":"session.not_found"})
        self.failure("session.not_found",404)
        self.response=lambda req:(503,{**req,"code":"session.source_unavailable",
                                     "error":{"code":"session.source_unavailable","message":"source down"}})
        body=self.failure("session.source_unavailable")
        self.assertEqual(body["details"]["request_id"],self.calls[-1][1]["request_id"])
        self.assertEqual(body["details"]["origin"],"session.read")
        self.response=lambda req:(400,{**req,"code":"session.identity_mismatch",
                                     "error":{"code":"session.identity_mismatch","message":"old provider"}})
        self.failure("session.identity_mismatch")
        self.response=lambda req:(0,{})
        self.failure("session.transport")
        self.response=lambda req:(200,{**req,"document":{**document(),"markdown":"x"*session_service.MAX_REPLY_BYTES}})
        self.failure("session.response_invalid")
        def slow(req):
            time.sleep(.25)
            return 200,{**req,"document":document()}
        self.response=slow
        with patch.object(session_service,"TIMEOUT",.05):
            start=time.monotonic()
            self.failure("session.timeout")
            self.assertLess(time.monotonic()-start,.23)
        with patch.object(session_service,"_SLOTS",threading.BoundedSemaphore(0)):
            self.failure("session.busy")

    def test_validation_and_markdown(self):
        self.install()
        for query in ({},{"task_id":""},{"task_id":"a","trace_id":"b"},{"task_id":"a\n"},
                      {"task_id":"a"*513},{"task_id":"route","format":"html"}):
            with self.subTest(query=query),self.assertRaises(HTTPError) as raised:
                self.fetch("/api/session",**query)
            self.assertEqual(raised.exception.code,400)
        with self.assertRaises(HTTPError) as raised:
            with urlopen(self.url+"/api/session?task_id=a&task_id=b"): pass
        self.assertEqual(raised.exception.code,400)
        with urlopen(self.url+"/api/session?task_id=route&format=markdown") as response:
            self.assertEqual(response.headers.get_content_type(),"text/markdown")
            self.assertIn("session.default @ 1",response.read().decode())

    def test_task_context_missing_unavailable_related_and_legacy(self):
        missing=self.fetch("/api/task-context",task_id="route")
        self.assertFalse(self.tasks.exists())
        self.assertFalse(missing["task_available"])
        self.assertTrue(missing["notes"])
        with closing(sqlite3.connect(self.tasks)) as writer:
            writer.executescript("""CREATE TABLE tasks(id TEXT PRIMARY KEY,text,state,detail,created,updated,steps);
                CREATE TABLE task_triggers(task_id TEXT PRIMARY KEY,trace_id);""")
            for i in range(11):
                writer.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)",
                               (str(i),"goal","SUCCEEDED","ok","t","t",'[{"tool":"inspect"}]'))
                writer.execute("INSERT INTO task_triggers VALUES(?,?)",(str(i),"work"))
            writer.commit()
        absent=self.fetch("/api/task-context",task_id="route")
        self.assertEqual(absent["notes"],[])
        self.assertIsNone(absent["task"])
        scoped=self.fetch("/api/task-context",trace_id="work")
        self.assertEqual(scoped["scope"],{"task_id":"10","trace_id":"work"})
        self.assertTrue(scoped["task_available"])
        self.assertEqual(scoped["task"]["steps"],[{"tool":"inspect"}])
        self.assertEqual(len(scoped["related_tasks"]),8)
        self.assertTrue(scoped["related_tasks_has_more"])
        with closing(sqlite3.connect(self.tasks)) as writer:
            writer.executescript("DROP TABLE task_triggers; UPDATE tasks SET steps='bad' WHERE id='1';")
        legacy=self.fetch("/api/task-context",task_id="1")
        self.assertEqual(legacy["scope"]["trace_id"],"1")
        self.assertEqual(legacy["task"]["steps"],[])
        self.assertTrue(legacy["notes"])

    def test_task_context_redacts_secrets_and_explicitly_bounds_large_steps(self):
        with closing(sqlite3.connect(self.tasks)) as writer:
            writer.executescript("CREATE TABLE tasks(id TEXT PRIMARY KEY,text,state,detail,created,updated,steps);")
            steps=[{"tool":"inspect","evidence":{"api_key":"private-value","log":"Bearer secret-token"}}]
            writer.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)",
                           ("route","password=do-not-show","SUCCEEDED","ok","t","t",json.dumps(steps)))
            writer.commit()
        result=self.fetch("/api/task-context",task_id="route")
        raw=json.dumps(result)
        for secret in ("private-value","secret-token","do-not-show"):
            self.assertNotIn(secret,raw)
        self.assertEqual(result["task"]["steps"][0]["evidence"]["api_key"],"[REDACTED]")
        with closing(sqlite3.connect(self.tasks)) as writer:
            writer.execute("UPDATE tasks SET steps=?",(json.dumps([{"value":"x"*300000}]),))
            writer.commit()
        limited=self.fetch("/api/task-context",task_id="route")
        self.assertEqual(limited["task"]["steps"],[])
        self.assertTrue(limited["notes"])
        self.assertLess(len(json.dumps(limited).encode()),256*1024)

    def test_event_stats_cutoff_filters_unicode_bytes_and_audit_exclusion(self):
        def record(kind,task="route",detail="中文",data=None):
            return self.history.record({"session_id":"run"},kind=kind,source="test",detail=detail,
                trace={"task_id":task},data=data)
        first=record("action.braking",data={"value":0.3})
        second=record("custom.warn")
        record("action.braking",task="other")
        for _ in range(20): record("session.query_completed")
        record("session.queryXcompleted")
        stats=self.fetch("/api/event-stats",task_id="route",before=second)
        self.assertEqual((stats["count"],stats["first_seq"],stats["last_seq"]),(1,first,first))
        event=self.fetch("/api/events",task_id="route",before=second)["events"][0]
        expected=len(json.dumps(event["data"],ensure_ascii=False,separators=(",",":")).encode())+len("中文".encode())
        self.assertEqual(stats["public_payload_bytes"],expected)
        filters={"task_id":"route","exclude_kind_prefixes":"evidence.query_,session.query_"}
        self.assertEqual(self.fetch("/api/event-stats",**filters)["count"],3)
        self.assertEqual([e["kind"] for e in self.fetch("/api/events",limit=3,**filters)["events"]],
                         ["session.queryXcompleted","custom.warn","action.braking"])
        self.assertEqual(len(self.fetch("/api/export",**filters)),3)
        self.assertEqual(stats["cursor"],self.history.cursor)
        self.assertEqual(stats["archive_id"],self.history.archive_id)
        self.assertEqual(self.fetch("/api/event-stats",task_id="absent")["count"],0)


if __name__=="__main__":
    unittest.main()
