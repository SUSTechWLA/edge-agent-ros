"""SDD-049 AC-3. Run with --http in the ROS image to exercise the actual Agent Handler and sockets."""
from contextlib import ExitStack, contextmanager
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import runtime_plugins
from edge_contracts.soft_transport import encode
from kernel import catalog


def put(folder, name, value):
    (folder / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")


def track(source, name):
    owner = source + ".provider"
    pin = {"plugin_id":owner, "version":"2", "name":name, "binding":source + "-binding"}
    return {"claims":{name:owner}, "plugins":[{"id":owner, "version":"2", "kind":"tool", "class":source,
            "lifecycle":"ACTIVE", "health":"ready", "availability":"available"}], "tools":[{
            "id":name, "version":"2", "description":name, "effect":"read", "parameters":{},
            "_" + source:pin}]}


class CatalogContract(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.services = self.root / "runtime"
        self.services.mkdir()
        (self.root / "derived").mkdir()
        env = patch.dict(os.environ, {"EDGE_PLUGIN_DIR":temp.name, "EDGE_EXTRA_PLUGIN_DIR":"",
                                     "EDGE_SOFT_SOCKET":"", "EDGE_PHYSICAL_SOCKET":""})
        env.start()
        self.addCleanup(env.stop)
        inputs = json.loads((ROOT / "tests/fixtures/plugin-tracks/v1-input.json").read_bytes())
        self.tool = inputs["tool"]
        self.service = inputs["services"][0]
        self.derived = inputs["series"]["id"]
        put(self.root, "tool", self.tool)
        put(self.services, self.service["id"], self.service)
        put(self.root / "derived", self.derived, inputs["series"])
        self.views = track("soft", "text.read"), track("physical", "camera.read")

    def view(self):
        with patch.object(catalog.host, "views", return_value=self.views) as read:
            response = catalog.capability_view(runtime_plugins.snapshot)
            read.assert_called_once_with()
        return response

    def test_draft_motion_matches_grant_policy_and_active_distinct_witness(self):
        policy = self.root / 'feedback.json'
        put(self.root, 'feedback', {'commissioned_motion_tools':['drive'], 'goals':{
            'goal':{'tool':'verify', 'tools':['verify','drive']}}})
        pin = dict(name='drive', version='1', plugin_id='chassis', resource_id='base', provider_id='driver',
                   verifier_id='witness', binding='sha256:'+'b'*64, content_digest='sha256:'+'c'*64, **{'class':'physical'})
        cap = dict(id='drive', version='1', effect='actuation', _physical=pin)
        with patch.dict(os.environ, EDGE_FEEDBACK_POLICY=str(policy)), patch.object(
                catalog.authorization,'station_motion_grant',return_value={'tools':['drive']},create=True) as grant:
            lookup = lambda: catalog.goal_motion_options('verify','calibrate',{'drive':cap})['tools']
            self.assertEqual(lookup(), ['drive'])
            grant.return_value=None
            self.assertEqual(lookup(), [])
            grant.return_value={'tools':['drive']}
            pin['verifier_id']='driver'
            self.assertEqual(lookup(), [])
            pin['verifier_id']='witness'
            put(self.root, 'feedback', {'commissioned_motion_tools':[], 'goals':{}})
            self.assertEqual(lookup(), [])

    def test_five_sources_and_legacy_fields(self):
        result = self.view()
        legacy = catalog.registry(legacy_only=True)
        self.assertEqual(result["capabilities"], catalog.catalog(catalog.host.catalog(legacy, self.views)))
        rows = {r["id"]:r for r in result["records"]}
        self.assertEqual(result["conflicts"], [])
        expected = {"verbatim":"v1_tool","session.read":"v1_service",self.derived:"derived",
                    "text.read":"v2_soft","camera.read":"physical"}
        for name, source in expected.items():
            with self.subTest(name=name):
                row = rows[name]
                self.assertEqual(row["source"], source)
                self.assertTrue(set("id version class effects parameters outcomes health binding source lifecycle".split())
                                <= row.keys())
                self.assertTrue(row["binding"])
        self.assertEqual(rows[self.derived]["class"], "soft")
        self.assertEqual(rows["verbatim"]["parameters"], self.tool["parameters"])
        self.assertEqual(rows["verbatim"]["binding"], catalog.binding(self.tool))
        self.assertEqual(rows["session.read"]["provider"], self.service["id"])
        self.assertEqual(rows["session.read"]["binding"], runtime_plugins.snapshot().pin("session.read")["binding"])
        self.assertEqual(rows["session.read"]["health"], "unknown")
        self.assertIsNone(rows["session.read"]["effects"])
        self.assertEqual((rows["camera.read"]["lifecycle"],rows["camera.read"]["health"]), ("ACTIVE","ready"))
        self.assertNotIn("http://", json.dumps(result))
        self.assertNotIn("_physical", json.dumps(result))

    def test_precedence_and_claimed_name_without_an_admitted_tool(self):
        name = self.derived
        put(self.root, "override", {**self.tool, "id":name})
        def winner():
            return next(row for row in self.view()["records"] if row["id"] == name)
        self.assertEqual(winner()["source"], "v1_tool")
        put(self.services, self.service["id"], {**self.service, "provides":[name]})
        self.assertEqual(winner()["source"], "v1_service")
        self.views = track("soft", name), self.views[1]
        self.assertEqual((winner()["source"],winner()["availability"]), ("v2_soft","unavailable"))
        self.views[0]["plugins"].clear()
        self.views[0]["tools"].clear()
        self.assertEqual((winner()["lifecycle"],winner()["binding"]), ("claimed",None))

    def test_equal_priority_conflicts_are_local_and_explicit(self):
        second = {**self.service, "id":"another"}
        put(self.services, second["id"], second)
        put(self.root, "other-tool", {**self.tool, "version":"different"})
        self.views = track("soft","shared"), track("physical","shared")
        resp = self.view()
        self.assertEqual([row["id"] for row in resp["conflicts"]], ["session.read","shared","verbatim"])
        self.assertTrue(all(row["code"] == "catalog.conflict" for row in resp["conflicts"]))
        ids = {r["id"] for r in resp["records"]}
        self.assertTrue({"status",self.derived} <= ids)
        self.assertFalse({"session.read","shared","verbatim"} & ids)

    def test_service_directory_read_failure_keeps_old_tools(self):
        # A regular file in place of the service directory fails the real metadata read.
        (self.services / (self.service["id"] + ".json")).unlink()
        self.services.rmdir()
        self.services.write_text("unavailable", encoding="utf-8")
        result = self.view()
        self.assertIn("verbatim", {cap["id"] for cap in result["capabilities"]})
        self.assertEqual(result["records"], [])
        self.assertEqual(result["records_error"]["code"], "catalog.source_unavailable")


@contextmanager
def serve(server):
    with server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()


def real_http_contract():
    """The only synthetic components are the two owners publishing their v2 views."""
    from service import Handler

    class Owner(socketserver.StreamRequestHandler):
        def handle(self):
            size = int.from_bytes(self.rfile.read(4), "big")
            if json.loads(self.rfile.read(size)) != {"action":"catalog"}:
                raise ValueError("unexpected operation during read-only discovery")
            self.server.calls += 1
            self.wfile.write(encode({"value":self.server.view}))

    with ExitStack() as stack:
        temp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        inputs = json.loads((ROOT / "tests/fixtures/plugin-tracks/v1-input.json").read_bytes())
        (temp / "runtime").mkdir()
        (temp / "derived").mkdir()
        put(temp, "verbatim", inputs["tool"])
        service = inputs["services"][0]
        put(temp / "runtime", service["id"], service)
        put(temp / "derived", inputs["series"]["id"], inputs["series"])
        env = {"EDGE_PLUGIN_DIR":str(temp), "EDGE_EXTRA_PLUGIN_DIR":""}
        owners = []
        for kind in ("soft","physical"):
            sock = str(temp / (kind + ".sock"))
            owner = socketserver.UnixStreamServer(sock, Owner)
            owner.view, owner.calls = track(kind, kind+".read"), 0
            owners.append(owner)
            stack.enter_context(serve(owner))
            env["EDGE_" + kind.upper() + "_SOCKET"] = sock
        stack.enter_context(patch.dict(os.environ, env))
        http = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        stack.enter_context(serve(http))
        with build_opener(ProxyHandler({})).open(
                f"http://127.0.0.1:{http.server_port}/agent/capabilities", timeout=5) as response:
            data = json.load(response)
        ids = {r["id"] for r in data["records"]}
        if ids != {"status","inspect","soft.read","physical.read","session.read","verbatim","derived.sample_gap"
                  } or any(o.calls != 1 for o in owners):
            raise AssertionError((ids, [o.calls for o in owners]))
        if {r["id"] for r in data["capabilities"]} != ids - {"session.read"} or data["conflicts"]:
            raise AssertionError(data)
        with build_opener(ProxyHandler({})).open(
                f"http://127.0.0.1:{http.server_port}/agent/capabilities?goal_tool=verify&text=calibrate", timeout=5) as response:
            if json.load(response)["goal_motion"] != {"goal_tool":"verify","tools":[]}:
                raise AssertionError("uncommissioned draft advertised motion")
        print("Agent HTTP capability catalog and both socket snapshots passed")


if __name__ == "__main__":
    if "--http" in sys.argv:
        sys.argv.remove("--http")
        real_http_contract()
    unittest.main()
