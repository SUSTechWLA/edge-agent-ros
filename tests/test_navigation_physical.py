"""SDD-051. Exercise Agent discovery/admission, independent ground fixture and physical handoff."""
from copy import deepcopy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT)]

from edge_contracts.invoke import candidate
from edge_contracts.navigation import map_reference, route_reference
from edge_contracts.soft_transport import request_at
from navigation.planner import plan_route
from physical_ledger import PhysicalError
from physical_navigation import CONTEXT, NavigationController, NavigationLedger, fixture_deployment
from agent.kernel.worker import task_plan
from agent import planning, plugin_tracks


def fixture_profile():
    world = {"site_id":"bench", "map_id":"hall", "revision":"v1", "frame_id":"map",
             "kind":"graph", "geometry":{"nodes":{"a":[0,0],"b":[1,0]},
                "edges":[{"from":"a","to":"b","tags":["level"]}]}}
    catalog = {"maps":{"hall":world}, "actors":{"rover":{"map_key":"hall","profile_ref":"wheel"}},
               "profiles":{"wheel":{"allowed_tags":["level"],"dimensions":2, "route_type":"ground","algorithm_ref":"astar"}},
               "destinations":{"office":[{"map_key":"hall","profiles":["wheel"],"position_m":[1,0]}]}}
    pose = {"actor_ref":"rover","map_ref":map_reference(world),"frame_id":"map","position_m":[0,0],
            "localization_epoch":"e1","clock_id":"test","stamp_ns":100}
    route = plan_route(catalog, "rover", "office", pose)
    return {"schema_version":1, "resource_id":"fixture.rover",
            "context":{k:route[k] for k in (*CONTEXT, "profile_digest")}, "routes":[route]}


class FixtureProtocol(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.now = 2_000_000_000
        self.profile = fixture_profile()
        self.site = fixture_deployment(self.profile)
        self.route = self.profile["routes"][0]
        self.open()
        self.assertFalse(self.ctrl.catalog()["tools"])
        self.next("executor")
        self.next("verifier")

    def open(self):
        self.ledger = NavigationLedger(self.state, "boot-fixture", clock=lambda:self.now)
        self.addCleanup(self.ledger.close)
        self.ledger.install(self.site)
        self.ctrl = NavigationController(self.ledger, self.site)

    def invoke(self, role, action, **fields):
        if role in ("executor", "verifier"):
            fields = {**fields, "instance_id":"test-" + role}
        return self.ctrl.command(role, {"action":action, **fields})

    def next(self, role):
        return self.invoke(role, "next", binding=self.site["pin"]["binding"])

    def plan(self, task_id):
        caps = plugin_tracks.catalog({}, ({"tools":[],"plugins":[],"claims":{}}, self.ctrl.catalog()))
        task = {"id":task_id, "text":"在仿真中执行路线 " + self.route["route_ref"]}
        class Intent:
            def soft_intent(self, _, value=None):
                return value
        with patch.object(plugin_tracks, "physical_request", side_effect=lambda action, **kw: self.invoke("agent", action, **kw)):
            _, steps = task_plan(Intent(), task, None, registry=lambda:caps, planner=planning.plan,
                                 read_only_planner=None, freeze=plugin_tracks.freeze)
        return steps[0]

    def call(self, step, **changes):
        pin = step["physical_pin"]
        return self.invoke("agent", "call", pin=pin, wire={"operation_id":pin["reservation_id"], "version":pin["version"],
                                                            "arguments":step["args"], **changes})

    def window(self):
        start = self.now + 1
        self.now = start + 500_000_000
        return {"first_ns":start, "last_ns":self.now, "sample_count":12, "max_linear_mps":0.0,
                "max_angular_radps":0.0, "clock_id":"host_monotonic","evidence_ref":"fixture/world-states"}

    def proof(self, work, position):
        lease = work["lease"]
        return {**self.window(), **self.site["context"], "route_ref":self.route["route_ref"], "position":position,
                **{k:lease[k] for k in ("operation_id","epoch","boot_id")}}

    def claim(self, step):
        self.assertEqual(self.call(step)["state"], "RUNNING")
        work = self.next("executor")
        self.assertEqual(work["kind"], "execute")
        self.assertEqual(work["route"], self.route)
        self.assertIsNone(self.next("executor"))
        return work

    def arrive(self, work):
        self.invoke("executor", "candidate", lease=work["lease"],
                    measurement={"outcome":"arrival", "route_ref":self.route["route_ref"]})
        self.assertEqual(self.next("verifier")["operation_id"], work["operation_id"])

    def test_public_agent_plan_once_only_and_observed_arrival(self):
        step = self.plan("demo")
        work = self.claim(step)
        self.open()
        self.assertIsNone(self.next("executor"))
        self.next("verifier")
        self.assertEqual(self.call(step)["state"], "RUNNING")
        self.arrive(work)
        proof = self.proof(work, [1.02,0])
        with self.assertRaises(PhysicalError):
            self.invoke("executor", "verify", proof=proof)
        for changes in ({"epoch":True}, {"map_ref":"other"}, {"first_ns":1}, {"max_angular_radps":0.3},
                        {"position":[float("nan"),0]}, {"profile_digest":"wrong"}):
            with self.assertRaises(PhysicalError):
                self.invoke("verifier","verify",proof={**proof,**changes})
        self.invoke("verifier","verify",proof=proof)
        reply = self.call(step)
        self.assertEqual(candidate(reply,work["operation_id"],step,step["physical_cap"])["verification"], proof)
        self.assertEqual(reply, self.call(step))
        with self.assertRaises(PhysicalError):
            self.call(step, arguments={"route_ref":"sha256:"+"0"*64})
        self.assertIsNone(self.next("executor"))

    def test_false_arrival_and_unapproved_route(self):
        step = self.plan("wrong-target")
        work = self.claim(step)
        self.arrive(work)
        self.invoke("verifier", "verify", proof=self.proof(work, [0,0]))
        self.assertEqual(self.call(step)["state"], "FAILED")
        altered = deepcopy(step)
        altered["physical_pin"].pop("reservation_id")
        altered["args"]["route_ref"] = "sha256:"+"0"*64
        for task in ("wrong-target", "new-approval"):
            with self.assertRaises(PhysicalError):
                self.invoke("agent","freeze_plan",task=task,provider="local",steps=[altered])
        self.assertIsNone(self.next("executor"))

    def test_cancel_after_claim_owns_resource_until_independent_stop(self):
        first = self.plan("first")
        second = self.plan("second")
        work = self.claim(first)
        self.assertEqual(self.invoke("agent","cancel",pin=first["physical_pin"])["state"],"RECOVERY_REQUIRED")
        self.assertEqual(self.call(first)["state"],"RUNNING")
        self.assertEqual(self.invoke("agent","operation_status",pin=first["physical_pin"])["reason"],"navigation.cancel_requested")
        with self.assertRaises(PhysicalError):
            self.invoke("executor","check",lease=work["lease"])
        with self.assertRaises(PhysicalError):
            self.call(second)
        self.assertIsNone(self.next("executor"))
        self.invoke("verifier","verify",proof=self.proof(work,[0.2,0]))
        self.assertEqual(self.call(first)["state"],"CANCELED")
        self.assertEqual(self.call(second)["state"],"RUNNING")
        self.assertEqual(self.invoke("agent","cancel",pin=second["physical_pin"])["state"],"CANCELED")
        self.assertIsNone(self.next("executor"))

    def test_expired_lease_recovery_and_drain_require_new_motion_witness(self):
        step = self.plan("lost")
        work = self.claim(step)
        self.now = work["lease"]["valid_until_ns"] + 1
        with self.assertRaises(PhysicalError):
            self.call(step)
        self.assertIsNone(self.next("executor"))
        resource = self.site["pin"]["resource_id"]
        self.ledger.drain(resource)
        self.assertFalse(self.ctrl.catalog()["tools"])
        stop = self.next("verifier")
        challenge = {k:stop[k] for k in ("resource_id","boot_id","epoch","stop_id")}
        with self.assertRaises(PhysicalError):
            self.invoke("verifier","stopped",proof={**challenge,"subscription_count":0})
        proof = {**self.window(), **challenge}
        self.invoke("verifier","stopped",proof=proof)
        self.assertEqual(self.ledger.release(resource)["phase"],"RELEASED")
        with self.assertRaises(PhysicalError):
            self.invoke("executor","check",lease=work["lease"])
        self.assertEqual(self.invoke("agent","operation_status",pin=step["physical_pin"])["state"],"RECOVERY_REQUIRED")

    def test_fixture_rejects_live_resources_guidance_and_wrong_map(self):
        for key,value in (("map_ref","sha256:" + "0"*64), ("actor_ref","other"), ("localization_epoch","e2")):
            profile = deepcopy(self.profile)
            profile["context"][key] = value
            with self.assertRaises(ValueError):
                fixture_deployment(profile)
        profile = deepcopy(self.profile)
        profile["routes"][0]["route_type"] = "guidance"
        profile["routes"][0]["route_ref"] = route_reference(profile["routes"][0])
        with self.assertRaises(PhysicalError):
            fixture_deployment(profile)
        with self.assertRaises(PhysicalError):
            fixture_deployment(dict(self.profile, resource_id="rover.base"))


def replay_observer(socket_path, binding, context, recorded_world, ready):
    """A separate client sees only its witness socket and independently supplied fixture samples."""
    def request(action, **fields):
        return request_at(socket_path, action, instance_id="world-observer", **fields)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pending = request("next", binding=binding)
        ready.set()
        if not pending:
            time.sleep(0.025)
            continue
        world = json.loads(Path(recorded_world).read_bytes())
        samples = []
        for position in world["observations"]:
            samples.append((time.monotonic_ns(), position))
            time.sleep(0.15)
        # The replay world is a bounded ground translation fixture with constant heading.
        speed = max(abs(b[1][0] - a[1][0]) / ((b[0] - a[0]) / 1e9) for a,b in zip(samples,samples[1:]))
        proof = {**context, "clock_id":"host_monotonic", "first_ns":samples[0][0], "last_ns":samples[-1][0],
                 "sample_count":len(samples), "max_linear_mps":speed, "max_angular_radps":0.0,
                 "position":samples[-1][1], "route_ref":pending["args"]["route_ref"],
                 "evidence_ref":"fixture/independent-world",
                 **{k:pending["lease"][k] for k in ("operation_id","boot_id","epoch")}}
        request("verify", proof=proof)
        return
    raise AssertionError("observer timed out waiting for candidate")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux deployment sockets and boot identity")
class SocketAcceptance(unittest.TestCase):
    def test_three_real_role_sockets_and_separate_observer_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = fixture_profile()
            site = fixture_deployment(profile)
            raw = json.dumps(profile).encode()
            (root / "fixture.json").write_bytes(raw)
            (root / "world.json").write_text(json.dumps({"observations":[[1,0]]*5}), encoding="utf-8")
            cli = [sys.executable, "-B", str(ROOT/"agent/physical_navigation.py")]
            flags = ["--profile", str(root/"fixture.json"), "--expect-profile", "sha256:"+hashlib.sha256(raw).hexdigest(),
                     "--state", str(root/"state"), "--sockets", str(root/"sockets")]
            subprocess.run(cli + ["install", *flags], check=True, stdout=subprocess.DEVNULL)
            server = subprocess.Popen(cli + ["serve", *flags])
            try:
                def path(role):
                    return str(root/"sockets"/role/"physical.sock")
                deadline = time.monotonic() + 10
                while not Path(path("verifier")).is_socket():
                    self.assertIsNone(server.poll())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
                ctx = multiprocessing.get_context("spawn")
                ready = ctx.Event()
                observer = ctx.Process(target=replay_observer, args=(path("verifier"), site["pin"]["binding"],
                                         profile["context"], str(root/"world.json"), ready))
                observer.start()
                try:
                    self.assertTrue(ready.wait(10))
                    def next_command():
                        return request_at(path("executor"), "next", instance_id="fixture-executor", binding=site["pin"]["binding"])
                    self.assertIsNone(next_command())
                    with patch.dict(os.environ, {"EDGE_PHYSICAL_SOCKET":path("agent"),"EDGE_SOFT_SOCKET":""}):
                        caps = plugin_tracks.catalog({})
                        self.assertEqual(caps["nav_execute"]["effect"], "actuation")
                        route = profile["routes"][0]
                        provider, steps = planning.plan("在仿真中执行路线 " + route["route_ref"], caps)
                        steps[0]["physical_pin"] = caps["nav_execute"]["_physical"]
                        _, steps = plugin_tracks.freeze("socket-demo", [provider, steps])
                        pin = steps[0]["physical_pin"]
                        wire = {"operation_id":pin["reservation_id"],"version":pin["version"],"arguments":steps[0]["args"]}
                        self.assertEqual(plugin_tracks.call_for(pin,"call",wire=wire)["state"],"RUNNING")
                        work = next_command()
                        request_at(path("executor"), "candidate", instance_id="fixture-executor", lease=work["lease"],
                                   measurement={"outcome":"arrival","route_ref":work["route"]["route_ref"]})
                        observer.join(8)
                        self.assertEqual(observer.exitcode, 0)
                        reply = plugin_tracks.call_for(pin,"call",wire=wire)
                        self.assertEqual(reply["state"], "SUCCEEDED")
                        self.assertEqual(reply["evidence"]["verification"]["position"], [1,0])
                        self.assertTrue(plugin_tracks.call_for(pin,"settle"))
                        self.assertIsNone(next_command())
                finally:
                    if observer.is_alive():
                        observer.terminate()
                    observer.join(3)
            finally:
                server.terminate()
                server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
