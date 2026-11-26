"""SDD-051. Physical-track executor for a bounded replay world, without a device transport."""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from physical_controller import Controller, deployment_owner, serve
from physical_ledger import InvalidPhysicalArguments, PhysicalLedger, digest, number, ref, require
from plugin_descriptor import SHA
from edge_contracts import navigation as route_contract


CONTEXT = ("actor_ref", "map_ref", "frame_id", "localization_epoch", "profile_ref")
WINDOW = ("first_ns", "last_ns", "sample_count", "max_linear_mps", "max_angular_radps")


def finite(value):
    return type(value) in (float, int) and -1e7 <= value <= 1e7


def fixture_deployment(profile):
    """Local simulation approval covers exact routes and this protocol implementation."""
    require(isinstance(profile, dict) and set(profile) == {"schema_version", "resource_id", "context", "routes"}
            and type(profile["schema_version"]) is int and profile["schema_version"] == 1,
            "plugin.config_invalid", "expected a version 1 replay profile")
    resource = profile["resource_id"]
    require(isinstance(resource, str) and resource.startswith("fixture.") and ref(resource),
            "plugin.permission_denied", "the fixture accepts only fixture resources")
    context = profile["context"]
    require(isinstance(context, dict) and set(context) == {*CONTEXT, "profile_digest"}
            and isinstance(profile["routes"], list) and 0 < len(profile["routes"]) <= 128,
            "plugin.config_invalid", "expected a bound local context and at most 128 approved routes")
    routes = {}
    for route in profile["routes"]:
        route_contract.validate_route(route, **{key:context[key] for key in CONTEXT})
        require(route["route_type"] in ("ground", "terrain") and route["route_ref"] not in routes
                and route["profile_digest"] == context["profile_digest"],
                "navigation.route_unsupported", "this fixture witnesses stationary ground arrival")
        routes[route["route_ref"]] = deepcopy(route)
    sources = [Path(__file__), Path(__file__).with_name("physical_controller.py"),
               Path(__file__).with_name("physical_ledger.py"), Path(route_contract.__file__)]
    content = digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    binding = digest([content, profile])
    pin = {"class":"physical", "plugin_id":"navigation.fixture", "name":"nav_execute", "version":"1.1.0",
           "resource_id":resource, "provider_id":"navigation.fixture.executor",
           "verifier_id":"navigation.fixture.observer", "content_digest":content, "binding":binding}
    return {"pin":pin, "context":deepcopy(context), "routes":routes, "lease_ms":30000,
            "min_quiet_ns":400_000_000, "health_ttl_ms":8000}


class NavigationLedger(PhysicalLedger):
    claim_kind = "execute"

    def check_arguments(self, device, arguments):
        if not (isinstance(arguments, dict) and set(arguments) == {"route_ref"}
                and isinstance(arguments["route_ref"], str) and SHA.fullmatch(arguments["route_ref"])):
            raise InvalidPhysicalArguments("tool.binding_unavailable", "expected a content-addressed route_ref")
        site = device["deployment"]
        if arguments["route_ref"] not in site["routes"]:
            raise InvalidPhysicalArguments("navigation.route_unapproved", "route has no local approval for this resource")

    def cancel(self, pin):
        """Withdraw unclaimed work atomically. An accepted stop request keeps the lease outstanding."""
        op = pin["reservation_id"]
        with self.write():
            row = self.status(op)
            require(pin == row["pin"], "tool.binding_unavailable", "cancel original binding")
            if row["state"] in ("RESERVED", "OFFERED"):
                self.db.execute("UPDATE physical_ops SET state='CANCELED' WHERE op=?", (op,))
                self.event(pin["resource_id"], "cancel.unclaimed", op)
            elif row["state"] in ("CLAIMED", "CANDIDATE", "RECOVERY_REQUIRED") and row["reason"] != "navigation.cancel_requested":
                lease = row["lease"]
                device = self._match(pin)
                require(lease and lease["epoch"] == device["epoch"] and lease["boot_id"] == self.boot,
                        "plugin.fence_rejected", "operation belongs to an earlier owner")
                candidate = {"route_ref":row["args"]["route_ref"], "observe_after_ns":self.clock(), "outcome":"cancel"}
                self.db.execute("""UPDATE physical_ops SET state='RECOVERY_REQUIRED',candidate=?,
                    reason='navigation.cancel_requested' WHERE op=?""", (json.dumps(candidate),op))
                self.event(pin["resource_id"], "cancel.requested", op)
        return self.status(op)

    def candidate(self, lease, measurement):
        op = lease["operation_id"]
        with self.write():
            row = self.status(op)
            expected = {"route_ref":row["args"]["route_ref"], "outcome":"arrival"}
            require(row["lease"] == lease and measurement == expected, "tool.receipt_invalid",
                    "executor must identify its original bound route and outcome")
            if row["candidate"] is None:
                self.assert_active(lease)
                report = {**expected, "observe_after_ns":self.clock()}
                self.db.execute("UPDATE physical_ops SET state='CANDIDATE',candidate=? WHERE op=?", (json.dumps(report),op))
                self.event(lease["resource_id"], "execute.candidate", op)
            else:
                require(row["candidate"]["outcome"] == "arrival", "plugin.fence_rejected", "operation canceled")
        return self.status(op)

    def quiet_window(self, proof, site, since):
        now = self.clock()
        require(proof.get("clock_id") == "host_monotonic" and number(proof.get("sample_count"), 2)
                and number(proof.get("first_ns")) and number(proof.get("last_ns"))
                and since < proof["first_ns"] < proof["last_ns"] <= now
                and now - proof["last_ns"] <= 1_000_000_000
                and proof["last_ns"] - proof["first_ns"] >= site["min_quiet_ns"]
                and finite(proof.get("max_linear_mps")) and 0 <= proof["max_linear_mps"] < 0.025
                and finite(proof.get("max_angular_radps")) and 0 <= proof["max_angular_radps"] < 0.05
                and ref(proof.get("evidence_ref")), "plugin.stop_unconfirmed", "fresh stationary witness required")

    def verify(self, proof, *, source):
        op = proof["operation_id"]
        with self.write():
            row = self.status(op)
            pin, lease = row["pin"], row["lease"]
            device = self._match(pin)
            site = device["deployment"]
            require(source == pin["verifier_id"] and device["verifier_instance"] and device["camera_instance"]
                    and device["verifier_instance"] != device["camera_instance"],
                    "plugin.verifier_invalid", "independent attached observer required")
            if row["proof"] == proof:
                return row
            sample = row["candidate"]
            fields = {"operation_id", "epoch", "boot_id", "route_ref", *site["context"], "clock_id",
                      *WINDOW, "position", "evidence_ref"}
            require(set(proof) == fields and row["state"] in ("CANDIDATE", "RECOVERY_REQUIRED") and sample and lease
                    and number(proof["epoch"]) and proof["epoch"] == device["epoch"] == lease["epoch"]
                    and proof["boot_id"] == device["boot_id"] == lease["boot_id"]
                    and proof["route_ref"] == row["args"]["route_ref"]
                    and all(proof[k] == v for k, v in site["context"].items()),
                    "plugin.verifier_invalid", "witness differs from original operation, frame, map or localization epoch")
            self.quiet_window(proof, site, sample["observe_after_ns"])
            target = site["routes"][proof["route_ref"]]["waypoints"][-1]
            position = proof["position"]
            require(isinstance(position, list) and len(position) == len(target) and all(finite(v) for v in position),
                    "plugin.verifier_invalid", "invalid observed position")
            state = "CANCELED" if sample["outcome"] == "cancel" else (
                "SUCCEEDED" if math.dist(position, target) <= 0.12 else "FAILED")
            self.db.execute("UPDATE physical_ops SET state=?,proof=?,reason=NULL WHERE op=?", (state,json.dumps(proof),op))
            self.event(pin["resource_id"], "execute.verified", op, outcome=state, evidence_ref=proof["evidence_ref"])
        return self.status(op)

    def confirm_stopped(self, resource, proof, *, source):
        """A motion handoff requires stationary world measurements after the stop challenge."""
        with self.write():
            device = self.device(resource)
            site = device["deployment"]
            token = self.db.execute("""SELECT challenge,issued_ns FROM physical_stop_requests
                WHERE resource=? AND epoch=? AND boot=?""", (resource,device["epoch"],self.boot)).fetchone()
            pending = self.db.execute("""SELECT 1 FROM physical_ops WHERE resource=? AND settled=0
                AND state<>'RECOVERY_REQUIRED' LIMIT 1""", (resource,)).fetchone()
            require(source == site["pin"]["verifier_id"] and device["verifier_instance"] and device["camera_instance"]
                    and device["verifier_instance"] != device["camera_instance"]
                    and device["phase"] == "DRAINING" and token and not pending
                    and set(proof) == {"resource_id","boot_id","epoch","stop_id","clock_id","evidence_ref",*WINDOW}
                    and proof["resource_id"] == resource and proof["boot_id"] == self.boot and number(proof["epoch"])
                    and proof["epoch"] == device["epoch"] and proof["stop_id"] == token[0],
                    "plugin.stop_unconfirmed", "original independent stop witness required")
            self.quiet_window(proof, site, token[1])
            self.db.execute("UPDATE physical_device SET phase='QUIESCENT',stop_proof=? WHERE resource=?",
                            (json.dumps(proof),resource))
            self.event(resource, "stop.confirmed", evidence_ref=proof["evidence_ref"])
        return self.device(resource)


class NavigationController(Controller):
    socket_roles = ("agent", "executor", "verifier")
    verification_error = "navigation.arrival_failed"

    def capability(self):
        return {"effect":"actuation", "parameters":{"route_ref":"string"},
                "description":"在隔离的地面运动夹具中执行已批准的路线并验证停稳",
                "utterances":["在仿真中执行路线 {route_ref}"]}

    def scope(self):
        return {"binding":self.pin["binding"]}

    def pending_stop(self, row):
        return row["reason"] == "navigation.cancel_requested" and self.ledger.clock() < row["lease"]["valid_until_ns"]

    def command(self, role, data):
        require(role in self.socket_roles, "plugin.permission_denied", "unknown fixture role")
        require(self.ledger.device(self.resource)["deployment"] == self.site, "tool.binding_unavailable",
                "controller differs from deployed fixture")
        action = data["action"]
        if role == "agent" and action == "freeze_plan":
            prior = self.ledger.db.execute("SELECT plan FROM physical_plans WHERE task=?", (data["task"],)).fetchone()
            if prior:
                previous = json.loads(prior[0])
                original = self.ledger.original_plan
                require(original([data["provider"], data["steps"]]) == original(previous), "tool.binding_unavailable",
                        "task already bound to another plan")
                return previous
        if role == "agent" and action == "cancel":
            pin = data["pin"]
            self.ledger.expire()
            row = self.ledger.status(pin["reservation_id"])
            require(row["pin"] == pin and pin["resource_id"] == self.resource,
                    "tool.binding_unavailable", "original operation binding required")
            return self.ledger.cancel(pin)
        # The deployed camera schema named the provider slot 'camera'. Only the
        # executor socket is mapped to that persisted slot in this fixture.
        result = super().command("camera" if role == "executor" else role, data)
        if role == "executor" and action == "next" and result and result.get("kind") == "execute":
            return {**result, "route":self.site["routes"][result["args"]["route_ref"]]}
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "serve", "status", "drain", "release"))
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--expect-profile", required=True, help="sha256 of the exact fixture profile file")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--sockets", required=True, type=Path)
    args = parser.parse_args()
    with args.profile.open("rb") as stream:
        raw = stream.read(2_000_001)
    require(len(raw) <= 2_000_000 and "sha256:" + hashlib.sha256(raw).hexdigest() == args.expect_profile,
            "plugin.permission_denied", "fixture profile differs from authorized file digest")
    site = fixture_deployment(json.loads(raw))
    resource = site["pin"]["resource_id"]
    ledger = NavigationLedger(args.state, Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    try:
        if args.action == "install":
            with deployment_owner(args.state):
                result = ledger.install(site)
        elif args.action == "serve":
            asyncio.run(serve(NavigationController(ledger, site), args.sockets, args.state, args.profile.parent))
            return
        else:
            require(ledger.device(resource)["deployment"] == site, "tool.binding_unavailable", "wrong deployment")
            result = ledger.device(resource) if args.action == "status" else getattr(ledger, args.action)(resource)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
