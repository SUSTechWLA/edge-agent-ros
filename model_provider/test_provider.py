"""SDD-021 adapter verification using synthetic credentials and isolated fixtures."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import provider as p
import upstream


class Endpoint(BaseHTTPRequestHandler):
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path,body,self.headers.get("Authorization")))
        if body["model"]=="pause":
            self.server.entered.set()
            self.server.release.wait(5)
        result={"done":True,"done_reason":"stop","eval_count":3,"message":{"content":"{\"ok\":true}"},"response":"hello"}
        wire=p.encoded(result)
        self.send_response(200)
        self.send_header("Content-Length",str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)

    def log_message(self,*args):
        pass


@contextmanager
def serving(server):
    thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.02},daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:"+str(server.server_port)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class ModelAdapters(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.journal=p.ModuleJournal(self.root/"journal.sqlite")
        self.addCleanup(self.journal.close)
        self.adapter=p.Adapter(self.root,self.journal)
        self.route()
        self.schema={"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":False}
        self.body={"model":"local-qwen","stream":False,"messages":[{"role":"user","content":"private prompt"}],
                   "options":{"temperature":0,"num_predict":12,"num_ctx":4096,"num_thread":3},"format":self.schema}
        self.cloud=dict(self.body,model="deepseek-flash")

    def route(self,local="qwen2.5:1.5b"):
        routes={"schema_version":1,"routes":{"local-qwen":{"provider":"ollama","model":local},
                                            "deepseek-flash":{"provider":"deepseek","model":"deepseek-flash"}}}
        path=self.root/"new.json"
        path.write_bytes(p.encoded(routes))
        path.replace(self.root/"routes.json")

    def events(self):
        return [json.loads(s[0]) for s in self.journal.db.execute("SELECT event FROM module_bus_events ORDER BY seq")]

    def test_live_local_http_hot_swap_inflight_and_generate(self):
        local=ThreadingHTTPServer(("127.0.0.1",0),Endpoint)
        local.calls,local.entered,local.release=[],threading.Event(),threading.Event()
        with serving(local) as base, patch.dict(os.environ,EDGE_MODEL_LOCAL_URL=base), serving(p.Server(("127.0.0.1",0),self.adapter)) as endpoint:
            def post(path,body):
                with urlopen(Request(endpoint+path,data=p.encoded(body),headers={"Content-Type":"application/json"}),timeout=6) as response:
                    return json.load(response)
            self.route("pause")
            old=[]
            worker=threading.Thread(target=lambda:old.append(post("/api/chat",self.body)))
            worker.start()
            try:
                self.assertTrue(local.entered.wait(3))
                self.route("new-local")
                gen=post("/api/generate",{"model":"local-qwen","prompt":"hello","stream":False})
                self.assertEqual(gen["response"],"hello")
                self.assertEqual(json.load(urlopen(endpoint+"/health"))["status"],"ready")
                self.assertEqual(len(json.load(urlopen(endpoint+"/api/tags"))["models"]),2)
            finally:
                local.release.set()
                worker.join(6)
            self.assertEqual(old[0]["message"]["content"],'{"ok":true}')
            self.assertEqual([(a,b["model"],auth) for a,b,auth in local.calls],
                             [("/api/chat","pause",None),("/api/generate","new-local",None)])
            self.assertEqual(local.calls[0][1],dict(self.body,model="pause"))
            with self.assertRaises(HTTPError) as broken:
                post("/api/chat",dict(self.body,stream=True))
            self.assertEqual(broken.exception.code,400)
        events=self.events()
        req=[e for e in events if e["kind"].endswith("requested")]
        fin=[e for e in events if e["kind"].endswith("completed")]
        self.assertNotEqual(req[0]["data"]["route_sha256"],req[1]["data"]["route_sha256"])
        self.assertEqual({r["event_id"] for r in req},{f["parent_event_id"] for f in fin})
        self.assertNotIn("private prompt",json.dumps(events))

    def test_cloud_translation_credentials_error_status_and_no_redirect(self):
        sent=[]
        def transport(provider,path,payload):
            sent.append(payload)
            return {"model":"deepseek-flash","choices":[{"message":{"content":'{"ok":true}',"reasoning_content":"private reasoning"},"finish_reason":"stop"}],
                    "usage":{"prompt_tokens":10,"completion_tokens":12}}
        result=self.adapter.invoke("/api/chat",self.cloud,transport)
        self.assertEqual(result["eval_count"],12)
        self.assertEqual(result["message"],{"role":"assistant","content":'{"ok":true}'})
        self.assertEqual(sent[0]["max_tokens"],12)
        self.assertEqual(sent[0]["response_format"],{"type":"json_object"})
        self.assertEqual(sent[0]["thinking"],{"type":"disabled"})
        self.assertIn(p.encoded(self.schema).decode(),sent[0]["messages"][0]["content"])
        self.assertNotIn("options",sent[0])
        key=self.root/"synthetic"
        key.write_text("fixture-key",encoding="ascii")
        with patch.object(upstream,"Path",return_value=key),patch.object(upstream,"fetch",return_value={"error":"auth_failed","upstream_status":401}) as send:
            out=upstream.exchange("deepseek","/api/chat",sent[0])
            self.assertEqual(send.call_args.args[0],"https://api.deepseek.com/chat/completions")
            self.assertEqual(send.call_args.args[2]["Authorization"],"Bearer fixture-key")
            self.assertEqual(out,{"error":"auth_failed","upstream_status":401})
        for reason in ("auth_failed","rate_limit","timeout","upstream_status"):
            row=p.encoded({"error":reason,"upstream_status":429})
            completed=subprocess.CompletedProcess([],0,stdout=row)
            with patch.object(p.subprocess,"run",return_value=completed):
                with self.assertRaises(p.Fault) as failed:
                    self.adapter.invoke("/api/chat",self.cloud)
                self.assertEqual(failed.exception.reason,reason)
        public=json.dumps(self.events())
        for text in ("fixture-key","private prompt","private reasoning","Bearer"):
            self.assertNotIn(text,public)

    def test_one_attempt_no_fallback_roundtrip_budget_and_configuration_errors(self):
        def timeout(*args,**kwargs):
            raise subprocess.TimeoutExpired("worker",1)
        with patch.object(p.subprocess,"run",side_effect=timeout) as send:
            with self.assertRaises(p.Fault) as error:
                self.adapter.invoke("/api/chat",self.body)
            self.assertEqual(error.exception.reason,"timeout")
            self.assertEqual(send.call_count,1)
            self.assertEqual(json.loads(send.call_args.kwargs["input"])["provider"],"ollama")
        (self.root/"routes.json").write_text('{"schema_version":1,"routes":{"local-qwen":{"provider":"deepseek","model":"deepseek-flash"}}}')
        with patch.object(p.subprocess,"run") as none:
            with self.assertRaises(p.Fault) as error:
                self.adapter.invoke("/api/chat",self.body)
            self.assertEqual(error.exception.reason,"config_invalid")
            none.assert_not_called()

    def test_malformed_blank_schema_length_and_binding(self):
        for reply,code in (({"content":'{"ok":1}'}, "schema_mismatch"), ({"content":'{"ok":true,"extra":1}'}, "schema_mismatch"),
                           ({"content":'{"ok":true,"ok":false}'}, "reply_invalid"), ({"content":"   "},"empty_reply"),
                           ({"reasoning_content":"keep private"},"reply_invalid")):
            with self.assertRaises(p.Fault) as error:
                self.adapter.invoke("/api/chat",self.cloud,lambda *a:{"choices":[{"message":reply,"finish_reason":"stop"}]})
            self.assertEqual(error.exception.reason,code)
        def truncated(*_):
            return {"choices":[{"message":{"content":"partial"},"finish_reason":"length"}],"usage":{"completion_tokens":12}}
        result=self.adapter.invoke("/api/chat",self.cloud,truncated)
        self.assertEqual((result["done_reason"],result["eval_count"],result["message"]["content"]),("length",12,""))
        for bad in (dict(self.cloud,images=[]),dict(self.cloud,options={"num_predict":-1}),
                    dict(self.cloud,format={"$ref":"file:///private"}),dict(self.cloud,model="missing")):
            with self.assertRaises(p.Fault):
                self.adapter.invoke("/api/chat",bad,lambda *args:self.fail("invalid input contacted upstream"))

    def test_missing_key_is_not_an_api_request(self):
        with patch.object(upstream,"Path",return_value=self.root/"absent"),patch.object(upstream,"fetch") as send:
            self.assertEqual(upstream.exchange("deepseek","/api/chat",{}),{"error":"key_unavailable"})
            send.assert_not_called()

    def test_real_redirect_is_not_followed_and_slow_exchange_expires(self):
        target=ThreadingHTTPServer(("127.0.0.1",0),Endpoint)
        target.calls=[]
        with serving(target) as destination:
            class Redirect(BaseHTTPRequestHandler):
                def do_POST(self):
                    self.send_response(307)
                    self.send_header("Location",destination+"/forbidden-receiver")
                    self.end_headers()

                def log_message(self,*args):
                    pass
            with serving(ThreadingHTTPServer(("127.0.0.1",0),Redirect)) as base:
                denied=upstream.fetch(base+"/api/chat",{},{"Authorization":"Bearer fixture-key"})
                self.assertEqual(denied,{"error":"upstream_status","upstream_status":307})
                self.assertEqual(target.calls,[])
        class Slow(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.server.started.set()
                self.server.release.wait(5)

            def log_message(self,*args):
                pass
        local=ThreadingHTTPServer(("127.0.0.1",0),Slow)
        local.started,local.release=threading.Event(),threading.Event()
        try:
            with serving(local) as base, patch.dict(os.environ,EDGE_MODEL_LOCAL_URL=base),patch.object(p,"TIMEOUT",1):
                start=time.monotonic()
                with self.assertRaises(p.Fault) as error:
                    self.adapter.invoke("/api/chat",self.body)
                self.assertTrue(local.started.is_set())
                self.assertLess(time.monotonic()-start,3)
                self.assertEqual(error.exception.reason,"timeout")
                self.assertEqual(self.events()[-1]["data"]["reason"],"timeout")
        finally:
            local.release.set()

    def test_restart_records_interrupted_call_without_resubmission(self):
        class SimulatedExit(BaseException):
            pass
        def interrupted(*args):
            raise SimulatedExit()
        with self.assertRaises(SimulatedExit):
            self.adapter.invoke("/api/chat",self.body,interrupted)
        pending=self.events()[-1]
        recovered=p.Adapter(self.root,self.journal)
        terminal=self.events()[-1]
        self.assertEqual((terminal["parent_event_id"],terminal["call_id"],terminal["data"]["reason"]),
                         (pending["event_id"],pending["call_id"],"outcome_unknown"))
        again=p.Adapter(self.root,self.journal)
        self.assertEqual(self.events()[-1],terminal)
        info=again.invoke("/api/chat",self.body,lambda *args:{"done":True,"message":{"content":'{"ok":true}'}})
        self.assertEqual(info["adapter"]["event_id"],self.events()[-1]["event_id"])

    def test_think_and_keep_alive_forwarded_locally_and_sanitized_for_cloud(self):
        forwarded=[]
        local_req=dict(self.body,think=False,keep_alive="30m")
        self.adapter.invoke("/api/chat",local_req,lambda prov,path,payload:(
            forwarded.append((prov,path,payload)) or {"done":True,"message":{"content":'{"ok":true}'}}))
        self.assertEqual(forwarded[0][2]["think"],False)
        self.assertEqual(forwarded[0][2]["keep_alive"],"30m")

        cloud_sent=[]
        cloud_req=dict(self.cloud,think=True,keep_alive="30m")
        self.adapter.invoke("/api/chat",cloud_req,lambda prov,path,payload:(
            cloud_sent.append(payload) or {"choices":[{"message":{"content":'{"ok":true}'},"finish_reason":"stop"}]}))
        self.assertEqual(cloud_sent[0]["thinking"],{"type":"enabled"})
        self.assertNotIn("keep_alive",cloud_sent[0])

        for bad in (dict(self.body,think="no"),dict(self.body,keep_alive="forever"),dict(self.body,keep_alive=-2)):
            with self.assertRaises(p.Fault) as err:
                self.adapter.invoke("/api/chat",bad,lambda *a:self.fail("invalid think/keep_alive reached upstream"))
            self.assertEqual(err.exception.reason,"options_invalid")


if __name__=="__main__":
    unittest.main()
