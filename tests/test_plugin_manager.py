"""SDD-008 offline manager protocol: both adapters, graph admission, CAS and crash reconstruction."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_author_tool import speed_case

AGENT = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(AGENT))
import author_tool as release
import plugin_manager as manager
import runtime_plugins as runtime


class ManagerContract(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base/"plugins"
        self.inputs = self.base/"input"
        self.inputs.mkdir()

    def make(self, name, provides=("clock",), requires=(), **updates):
        path = self.inputs/(name+".json")
        path.write_bytes(release.json_bytes({"id":name, "version":"1.0", "schema_version":1,
            "requires":list(requires), "provides":list(provides), "endpoint":"http://private-host/verify", **updates}))
        return path

    def command(self, action, target, expect=None, kind="runtime", **options):
        args = argparse.Namespace(kind=kind, root=self.root, action=action, expect=expect,
                                  id=target if action == "remove" else None,
                                  definition=target if action != "remove" else None, **options)
        return manager.change(args)

    def events(self, kind="runtime"):
        with closing(sqlite3.connect(self.root/kind/".history"/"module-events.sqlite")) as db:
            return [json.loads(raw) for raw, in db.execute("SELECT event FROM module_bus_events ORDER BY seq")]

    def test_version_hash_normalizes_order_and_changes_on_drain(self):
        sensor = self.make("sensor", ["clock","sample"])
        first = self.command("check", sensor)
        self.assertFalse(self.root.exists())
        self.assertEqual(first, self.command("check", self.make("sensor", ["sample","clock"],draining=False)))
        live = self.command("publish",sensor,"absent")
        self.assertEqual(first["version"], live["version"])
        binding = runtime.ServiceSnapshot(self.root/"runtime").pin("sample")["binding"]
        new = self.command("publish",self.make("sensor",["sample","clock"],draining=True),live["version"])
        self.assertNotEqual(new["version"], live["version"])
        self.assertEqual(runtime.ServiceSnapshot(self.root/"runtime").pin("sample", binding)["binding"], binding)
        old_file = self.root/"runtime"/".history"/"sensor"/(live["version"]+".json")
        sensor.write_bytes(old_file.read_bytes())
        self.assertEqual(self.command("publish",sensor,new["version"])["version"],live["version"])
        self.assertEqual(self.command("remove","sensor",live["version"])["previous"],live["version"])
        rows = self.events()
        self.assertEqual({r["module"] for r in rows},{"plugin.manager"})
        self.assertNotIn("private-host",json.dumps(rows))
        for req, result in zip(rows[::2], rows[1::2]):
            self.assertEqual(result["parent_event_id"],req["event_id"])
            self.assertEqual(result["data"]["plugin_id"],"sensor")

    def test_admission_conflict_dependencies_cycle_unrelated_and_dependent_removal(self):
        source = self.command("publish",self.make("source"),"absent")
        self.command("publish",self.make("reader",["report"],["clock"]),"absent")
        before = runtime.ServiceSnapshot(self.root/"runtime").pin("report")
        for path, code in ((self.make("duplicate"),"service_conflict"),
                           (self.make("missing",["new"],["absent"]),"dependency_unavailable"),
                           (self.make("cycle",["loop"],["loop"]),"dependency_unavailable")):
            with self.assertRaises(release.ReleaseRejected):
                self.command("publish",path,"absent")
            self.assertEqual(self.events()[-1]["code"],"release."+code)
        with self.assertRaises(release.ReleaseRejected):
            self.command("remove","source",source["version"])
        with self.assertRaises(release.ReleaseRejected):
            self.command("publish",self.make("source",["other"]),source["version"])
        self.assertEqual(runtime.ServiceSnapshot(self.root/"runtime").pin("report"),before)
        # An independent malformed entry cannot prevent admission of an unrelated service.
        (self.root/"runtime"/"broken.json").write_text("bad",encoding="utf-8")
        self.command("publish",self.make("independent",["time"]),"absent")
        self.assertEqual(runtime.ServiceSnapshot(self.root/"runtime").pin("time")["plugin_id"],"independent")

    def test_rejects_invalid_id_unknown_executable_field_and_private_endpoint(self):
        path = self.make("sensor", executable="/bin/hidden")
        with self.assertRaises(ValueError):
            self.command("publish",path,"absent")
        self.make("sensor").rename(self.inputs/"wrong.json")
        with self.assertRaises(ValueError):
            self.command("publish",self.inputs/"wrong.json","absent")
        with self.assertRaises(ValueError):
            self.command("publish",self.make("sensor",endpoint="https://a:secret@private-host/svc"),"absent")
        with self.assertRaises(ValueError):
            self.command("remove","../other","absent")
        public = json.dumps(self.events())
        for private in ("private-host","secret","/bin/hidden","../other"):
            self.assertNotIn(private,public)
        self.assertFalse((self.root/"runtime"/"sensor.json").exists())

    def test_retry_and_competing_requests_preserve_current_state(self):
        path = self.make("sensor")
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.command,"publish",path,"absent",request_id="decision-57") for _ in range(2)]
            first, duplicate = [f.result(timeout=10) for f in futures]
        self.assertEqual(first,duplicate)
        rows = self.events()
        self.assertEqual(len(rows), 2)
        latest = self.command("publish",self.make("sensor",version="2"),first["version"],request_id="decision-58")
        with self.assertRaisesRegex(release.ReleaseRejected,"different publication"):
            self.command("publish",path,"absent",request_id="decision-57")
        self.assertEqual(self.events()[-1]["data"]["prior_event_id"], first["event_id"])
        self.assertEqual(self.events()[-1]["code"], "release.request_conflict")
        self.make("sensor")
        self.assertEqual(self.command("publish",path,"absent",request_id="decision-57"),first)
        with self.assertRaisesRegex(ValueError,"current version"):
            self.command("publish",path,"absent",request_id="obsolete")
        count = len(self.events())
        with self.assertRaisesRegex(release.PriorRequestError,"release.version_conflict"):
            self.command("publish",path,"absent",request_id="obsolete")
        self.assertEqual(len(self.events()),count)
        self.assertEqual(manager.RuntimeRelease.installed(self.root/"runtime","sensor")[1],latest["version"])

    def test_crash_after_file_replace_reports_observed_version_without_reapplying(self):
        path = self.make("sensor")
        with patch.object(release, "finish_release", side_effect=SystemExit(42)), self.assertRaises(SystemExit):
            self.command("publish",path,"absent",request_id="trial")
        self.assertEqual(len(self.events()),1)
        active = self.root/"runtime"/"sensor.json"
        actual = active.read_bytes()
        with self.assertRaisesRegex(release.PriorRequestError,"release.target_present"):
            self.command("publish",path,"absent",request_id="trial")
        records = self.events()
        self.assertEqual([r["kind"] for r in records], ["plugin.release.requested","plugin.release.reconciled"])
        self.assertEqual(records[-1]["data"]["writer_attribution"],"unknown")
        self.assertEqual(active.read_bytes(),actual)

    def test_derived_python_and_runtime_cli_share_layout_without_sharing_authority(self):
        candidate = self.inputs/"speed.json"
        candidate.write_bytes(release.json_bytes({"schema_version":1, "id":"derived.speed", "field":"speed",
                                                  "unit":"km/h", "description":"report speed"}))
        cases = self.inputs/"cases.json"
        cases.write_bytes(release.json_bytes([speed_case(1,3.6),speed_case(-.5,-1.8)]))
        first = self.command("publish",candidate,"absent","derived",cases=cases,request_id="measure")
        self.assertEqual(first,self.command("publish",candidate,"absent","derived",cases=cases,request_id="measure"))
        self.assertEqual(self.events("derived")[-1]["module"],"plugin.publisher")
        self.command("remove","derived.speed",first["version"],"derived")
        path = self.make("sample")
        result = subprocess.run([sys.executable, str(AGENT/"plugin_manager.py"),"publish",str(path),
                                 "--kind","runtime","--root",str(self.root),"--expect","absent",
                                 "--trace-id","t1","--parent-event-id","author-1"],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        receipt = json.loads(result.stdout)
        request, applied = self.events()
        self.assertEqual((request["trace_id"],request["parent_event_id"]),("t1","author-1"))
        self.assertEqual(receipt["event_id"],applied["event_id"])


if __name__ == "__main__":
    unittest.main()
