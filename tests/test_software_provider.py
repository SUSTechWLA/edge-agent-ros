"""SDD-014 contract boundaries. Evaluate only fixed trusted test operations in unit tests."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"client"))
import software_provider as provider
import software_release as release


class SoftwareProviderTest(unittest.TestCase):
    def test_supported_text_and_numeric_types_preserve_input(self):
        source=b"def transform(text):\n    return text"
        value="  ｒｏｂｏｔ\trobot!  "
        self.assertEqual(provider.evaluate(provider.program(source),value),value)
        source=b"def transform(text):\n    return text.casefold().split().count('robot')"
        self.assertEqual(provider.evaluate(provider.program(source),"ｒｏｂｏｔ robot\tROBOT robotics"),2)
        self.assertEqual(provider.evaluate(provider.program(source),""),0)

    def test_syntax_types_calls_limits_and_budget(self):
        invalid=("return text","return len(text)","return text.split()","return text.split().strip()",
                 "return text.count('x').count('x')","return text.upper(1)","return text.split('a')",
                 "return text"+" .lower()"*9)
        for i,body in enumerate(invalid):
            source=("def transform(value):\n    "+body if i==0 else "def transform(text):\n    "+body)
            with self.subTest(body=body),self.assertRaises(provider.Invalid):
                provider.program(source.encode())
        with self.assertRaisesRegex(provider.Invalid,"input_invalid"):
            provider.evaluate([], "中"*1400)
        with self.assertRaisesRegex(provider.Invalid,"result_size"):
            provider.evaluate([("upper",None)], "ǰ"*2048)

    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        src=ROOT/"examples/software-provider"
        self.cases=(src/"cases.json").read_bytes()
        self.source,self.identity,self.version=provider.package(src)
        self.trace="source-task"
        self.draft={"schema_version":1,"status":"draft","design_id":"a"*64,"goal":"Count words.",
                    "source":{"task_id":"task","trace_id":self.trace}}
        draft_file=self.root/"design.json"
        draft_file.write_bytes(release.json_bytes(self.draft))
        self.event={"event_id":"draft-event","parent_event_id":"draft-start","task_id":"task","trace_id":self.trace,
                    "kind":"plugin.design.drafted","code":"design.drafted","source":"journal:plugin.design",
                    "module":"plugin.design","data":{"design_id":"a"*64,"draft_sha256":provider.digest(draft_file.read_bytes())}}
        event_file=self.root/"event.json"
        event_file.write_bytes(release.json_bytes(self.event))
        self.args=argparse.Namespace(action="verify",design=draft_file,event=event_file,package=src,
            archive="http://trusted-console:8080",endpoint="http://provider:8125/invoke",cases=self.root/"cases.json",
            registry=self.root/"plugins",store=self.root/"private",expect="absent",request_id="release-v1")
        self.args.cases.write_bytes(self.cases)
        self.archive_rows=[dict(self.event)]
        def read_archive(request,body,limit,timeout):
            self.assertTrue(request.startswith("http://trusted-console:8080/api/events?"))
            return release.json_bytes({"events":self.archive_rows,"has_more":False})
        def sample(_url,body,**kwargs):
            # Independent known-good algorithm belongs to the fixture endpoint.
            text=body["arguments"]["text"]
            return {"operation_id":body["operation_id"],"state":"SUCCEEDED","evidence":{
                "value":sum(w.casefold()=="robot" for w in text.split()),"definition_version":self.version,
                "input_sha256":provider.digest(text.encode())}}
        self.archive=self.enterContext(patch.object(release,"http_bytes",side_effect=read_archive))
        self.endpoint=self.enterContext(patch.object(release,"send",side_effect=sample))
        self.journal=self.enterContext(closing(release.ModuleJournal(self.root/"journal.db")))

    def test_source_export_and_forged_private_brief_fail_closed(self):
        self.archive_rows.clear()
        with self.assertRaisesRegex(provider.Invalid,"design_invalid"):
            release.run(self.args,self.journal)
        self.archive_rows.append(dict(self.event))
        self.args.design.write_bytes(b'{"status":"draft"}')
        with self.assertRaisesRegex(provider.Invalid,"design_invalid"):
            release.run(self.args,self.journal)
        self.endpoint.assert_not_called()
        rejected=self.journal.db.execute("SELECT event FROM module_bus_events").fetchall()
        self.assertEqual([json.loads(s[0])["code"] for s in rejected],["software.design_invalid"]*2)

    def test_approval_binds_design_cases_and_manifest_and_requires_own_verified_event(self):
        verified=release.run(self.args,self.journal)
        self.args.action="publish"
        self.args.approve=verified["approval_sha256"]
        self.args.cases.write_bytes(self.cases+b" ")
        with self.assertRaisesRegex(provider.Invalid,"approval_mismatch"):
            release.run(self.args,self.journal)
        self.args.cases.write_bytes(self.cases)
        self.args.endpoint="http://other:8125/invoke"
        with self.assertRaisesRegex(provider.Invalid,"approval_mismatch"):
            release.run(self.args,self.journal)
        self.args.endpoint="http://provider:8125/invoke"
        other=self.enterContext(closing(release.ModuleJournal(self.root/"other.db")))
        with self.assertRaisesRegex(provider.Invalid,"verification_required"):
            release.run(self.args,other)
        second=dict(self.event,event_id="other-valid-design-event")
        self.archive_rows[:]=[second]
        self.args.event.write_bytes(release.json_bytes(second))
        with self.assertRaisesRegex(provider.Invalid,"approval_mismatch"):
            release.run(self.args,self.journal)
        self.assertFalse(self.args.registry.exists())

    def test_invalid_evidence_and_request_replay_do_not_repeat_publication(self):
        self.args.cases.write_bytes(release.json_bytes([{"text":"robot","expected":4},{"text":"other","expected":0}]))
        with self.assertRaisesRegex(provider.Invalid,"case_mismatch"):
            release.run(self.args,self.journal)
        self.args.cases.write_bytes(self.cases)
        verified=release.run(self.args,self.journal)
        self.args.action="publish"
        self.args.approve=verified["approval_sha256"]
        first=release.run(self.args,self.journal)
        self.endpoint.reset_mock()
        repeated=release.run(self.args,self.journal)
        self.assertEqual(first["event_id"],repeated["event_id"])
        self.endpoint.assert_not_called()
        artifact=json.loads((self.args.store/(self.args.approve+".json")).read_bytes())
        self.assertEqual(artifact["source"],self.source.decode())
        self.assertEqual(artifact["cases_sha256"],provider.digest(self.cases))

    def test_interrupted_archive_and_timed_out_approval_are_recorded(self):
        self.archive.side_effect=release.HTTPException("closed")
        with self.assertRaisesRegex(provider.Invalid,"design_invalid"):
            release.run(self.args,self.journal)
        event=json.loads(self.journal.db.execute("SELECT event FROM module_bus_events").fetchone()[0])
        self.assertEqual(event["data"]["reason"],"HTTPException")
        self.archive.side_effect=None
        self.archive.return_value=release.json_bytes({"events":self.archive_rows,"has_more":False})
        verified=release.run(self.args,self.journal)
        self.args.action,self.args.approve="publish",verified["approval_sha256"]
        with patch.object(release,"monotonic",side_effect=[0,0,16]),self.assertRaisesRegex(provider.Invalid,"deadline"):
            release.run(self.args,self.journal)
        active=self.args.registry/(self.identity["contract"]["id"]+".json")
        self.assertFalse(active.exists())
        reasons=[json.loads(s[0])["data"].get("reason") for s in self.journal.db.execute("SELECT event FROM module_bus_events")]
        self.assertIn("software.deadline",reasons)

    def test_stale_removal_preserves_new_release(self):
        original=release.run(self.args,self.journal)
        self.args.action,self.args.approve="publish",original["approval_sha256"]
        first=release.run(self.args,self.journal)
        endpoint=self.args.endpoint
        self.args.action,self.args.endpoint="verify","http://provider-b:8125/invoke"
        updated=release.run(self.args,self.journal)
        self.args.action,self.args.approve,self.args.expect="publish",updated["approval_sha256"],first["version"]
        self.args.request_id="upgrade"
        release.run(self.args,self.journal)
        self.args.action,self.args.endpoint,self.args.request_id="remove",endpoint,"stale-removal"
        with self.assertRaises(provider.Invalid):
            release.run(self.args,self.journal)
        active=self.args.registry/(self.identity["contract"]["id"]+".json")
        self.assertEqual(json.loads(active.read_bytes())["endpoint"],"http://provider-b:8125/invoke")
        codes=[json.loads(s[0])["code"] for s in self.journal.db.execute("SELECT event FROM module_bus_events")]
        self.assertIn("release.version_conflict",codes)


class TimeBudgetTest(unittest.TestCase):
    def test_control_client_returns_when_connection_setup_is_delayed(self):
        entered, finish, closed=threading.Event(),threading.Event(),threading.Event()
        class PendingConnection:
            sock=None
            def __init__(self,*args,**kwargs): pass
            def connect(self):
                entered.set()
                finish.wait(5)
            def close(self): closed.set()
        with patch.object(release.bounded_http,"HTTPConnection",PendingConnection):
            try:
                with self.assertRaisesRegex(provider.Invalid,"software.deadline"):
                    release.http_bytes("http://local/test",None,1024,0.1)
                self.assertTrue(entered.is_set())
            finally:
                finish.set()
            self.assertTrue(closed.wait(2))

    def test_provider_reclaims_request_and_other_slot_serves_work(self):
        entered, resumed, normal=threading.Event(),threading.Event(),threading.Event()
        class PendingSocket:
            def settimeout(self,_timeout): pass
            def shutdown(self,_mode): resumed.set()
            def close(self): pass
        first=PendingSocket()
        class FixtureHandler:
            def __init__(self,request,address,server):
                if request is first:
                    entered.set()
                    resumed.wait(3)
                else:
                    normal.set()
        with provider.Server(("127.0.0.1",0),FixtureHandler) as server:
            server.request_seconds=.4
            server.process_request(first,("127.0.0.1",0))
            self.assertTrue(entered.wait(2))
            from unittest.mock import Mock
            server.process_request(Mock(),("127.0.0.1",0))
            self.assertTrue(normal.wait(.3))
            self.assertFalse(resumed.is_set())
            self.assertTrue(resumed.wait(2))


if __name__=="__main__":
    unittest.main()
