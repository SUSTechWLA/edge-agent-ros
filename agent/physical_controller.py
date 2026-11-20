"""SDD-045 P2. Three disjoint Unix sockets; deployment commands run only against the private volume."""
import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
import time

from physical_ledger import PhysicalError, PhysicalLedger, digest, number, require
from plugin_descriptor import load_descriptor
from soft_client import LIMIT, encode


def load_deployment(site, image):
    item = load_descriptor(site / "camera.plugin.json", site / "trust")
    cfg = json.loads((site / "deployment.json").read_bytes())
    # The read-only trust catalog verifies the platform manifest; Docker's container
    # inspection exposes that manifest's *config* digest, a different identifier.
    image_bytes = (site/"trust"/"image.json").read_bytes()
    require("sha256:"+hashlib.sha256(image_bytes).hexdigest()==image, "plugin.permission_denied", "untrusted platform manifest")
    manifest = json.loads(image_bytes)
    sensor = cfg.get("sensor", {})
    require(isinstance(sensor,dict) and set(sensor)=={"source_id","topic","ros_domain_id"} and
            isinstance(sensor["source_id"],str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,95}",sensor["source_id"]) and
            isinstance(sensor["topic"],str) and len(sensor["topic"])<=240 and
            re.fullmatch(r"(?:/[A-Za-z_][A-Za-z0-9_]*)+",sensor["topic"]) and
            type(sensor["ros_domain_id"]) is int and 0 <= sensor["ros_domain_id"] <= 232 and
            cfg.get("image_config_digest") == manifest["config"]["digest"], "plugin.permission_denied",
            "missing or invalid deployed sensor scope or image identity")
    require(item["class"] == "physical" and item["runtime"] == "process" and item["effects"] == "read" and
            item["provides"] == [{"type":"tool","name":"capture_camera_frame","schema_ref":"camera/capture@1"}] and
            not item["requires"] and item["drain_required"] and not item["hot_reload"],
            "plugin.class_violation","camera pilot requires the pinned physical read process")
    require(item["provenance"]["image_digest"] == image == cfg["image_digest"] == cfg["verifier"]["image_digest"] and
            item["provenance"]["source_commit"] == cfg["deployment_commit"] and item["content_digest"] == cfg["descriptor_digest"] and
            item["policy"] == {"install":["deployment"],"enable":["deployment"],"site_approval":True},
            "plugin.permission_denied","site approval does not cover this image and release")
    require(item["device_lease"] == cfg["resource_id"] == "camera.rgb" and
            item["stop_protocol"] == "camera.independent_quiescence" and item["operation_ledger"] == "camera.operations"
            and number(cfg["lease_ms"],1000) and cfg["lease_ms"] <= 120000
            and number(cfg["min_quiet_ns"],400000000), "plugin.physical_contract_missing","unknown lease or stop protocol")
    require(cfg["provider_id"] == "camera.adapter" and cfg["verifier"]["id"] == item["verifier"] == "camera.verify" and
            item["id"] == "camera.capture" and item["entrypoint"] == ["python3","-B","/opt/edge/perception/camera/physical.py"]
            and cfg["verifier"]["entrypoint"] == ["python3","-B","/opt/edge/perception/camera/physical_verifier.py"],
            "plugin.verifier_invalid","camera and independent verifier require distinct deployment identities")
    pin = {"class":"physical","plugin_id":item["id"],"name":"capture_camera_frame","version":item["version"],
           "content_digest":item["content_digest"],"image_digest":image,"deployment_commit":cfg["deployment_commit"],
           "resource_id":cfg["resource_id"],"provider_id":cfg["provider_id"],"verifier_id":item["verifier"],
           "sensor":sensor,"binding":digest([item["content_digest"], cfg])}
    return {"pin":pin,"lease_ms":cfg["lease_ms"],"min_quiet_ns":cfg["min_quiet_ns"],"health_ttl_ms":item["health"]["ttl_ms"],
            "image_config_digest":cfg["image_config_digest"],"camera_argv":item["entrypoint"]}


@contextmanager
def deployment_owner(state):
    lock = sqlite3.connect(state / ".controller-lock.sqlite",timeout=0)
    try:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise PhysicalError("plugin.physical_hot_reload_forbidden","stop the old controller before installing code") from exc
        yield
    finally:
        lock.close()


class Controller:
    socket_roles = ("agent", "camera", "verifier")
    verification_error = "camera.verification_failed"

    def __init__(self, ledger, deployment):
        self.ledger = ledger
        self.site = deployment
        self.pin = deployment["pin"]
        self.resource = self.pin["resource_id"]
        self.heartbeat = {}
        require(ledger.device(self.resource)["deployment"] == deployment, "tool.binding_unavailable",
                "deploy the approved site offline before starting the controller")

    def capability(self):
        return {"effect":"read","description":"采集一帧相机图像并独立核验","parameters":{}}

    def scope(self):
        return self.pin["sensor"]

    def pending_stop(self, row):
        return False

    def catalog(self):
        device = self.ledger.device(self.resource)
        pin = self.pin
        matches = device["deployment"] == self.site
        healthy = matches and all(time.monotonic() - self.heartbeat.get(role, -float("inf")) <
                                  self.site["health_ttl_ms"]/1000 for role in ("camera","verifier"))
        available = healthy and device["phase"] == "ACTIVE"
        cap = {"id":pin["name"],"version":pin["version"],**self.capability(),"_physical":pin}
        row = {"id":pin["plugin_id"],"kind":"tool","class":"physical","version":pin["version"],
               "content_digest":pin["content_digest"],"lifecycle":device["phase"],"health":"ready" if healthy else "unavailable",
               "availability":"available" if available else "unavailable","draining":device["phase"] != "ACTIVE"}
        return {"plugins":[row], "tools":[cap] if available else [], "claims":{pin["name"]:pin["plugin_id"]}}

    def command(self, role, data):
        action = data["action"]
        ledger = self.ledger
        resource = self.resource
        require(ledger.device(resource)["deployment"] == self.site, "tool.binding_unavailable",
                "controller identity differs from active deployment")
        if role == "agent":
            if action == "catalog":
                return self.catalog()
            if action == "reconcile_denial":
                return ledger.reconcile_denial(data["task"],data["provider"],data["steps"],self.site)
            if action == "freeze_plan":
                # Always return an already admitted immutable plan, even while draining.
                prior = ledger.db.execute("SELECT plan FROM physical_plans WHERE task=?", (data["task"],)).fetchone()
                if prior:
                    return json.loads(prior[0])
                ledger.check_not_denied(data["task"])
                require(self.catalog()["tools"],"plugin.dependency_unavailable","provider or independent verifier unavailable")
                return ledger.freeze_plan(data["task"],data["provider"],data["steps"])
            if action == "call":
                pin, wire = data["pin"],data["wire"]
                row = ledger.begin(pin,wire)
                op = row["operation_id"]
                if row["state"] == "SUCCEEDED":
                    return {"operation_id":op,"state":"SUCCEEDED",
                            "evidence":{**row["candidate"],"verifier_id":pin["verifier_id"],"verification":row["proof"]}}
                if row["state"] == "RECOVERY_REQUIRED":
                    if self.pending_stop(row):
                        return {"operation_id":op,"state":"RUNNING"}
                    raise PhysicalError("tool.outcome_unknown", row["reason"] + ": reconcile original physical operation")
                if row["state"] in ("FAILED","CANCELED"):
                    return {"operation_id":op,"state":row["state"],"error":{"code":self.verification_error}}
                return {"operation_id":op,"state":"RUNNING"}
            if action == "settle":
                return ledger.settle(data["pin"])
        if role in ("camera","verifier") and action == "next":
            expected = self.scope()
            require(all(k in data and type(data[k]) is type(v) and data[k]==v for k,v in expected.items()),
                    "plugin.permission_denied","process scope differs from deployment")
            result = ledger.poll(resource, role, data["instance_id"])
            self.heartbeat[role] = time.monotonic()
            return result
        if role in ("camera","verifier"):
            attached = ledger.device(resource)[role+"_instance"]
            require(attached and data.get("instance_id") == attached,"plugin.fence_rejected",
                    "operation must be reported by the original attached instance")
        if role in ("camera","verifier") and action == "status":
            return ledger.status(data["operation_id"])
        if role == "camera":
            if action == "check":
                return ledger.assert_active(data["lease"])
            if action == "candidate":
                return ledger.candidate(data["lease"],data["measurement"])
            if action == "unknown":
                return ledger.unknown(data["lease"],data["code"])
        if role == "verifier":
            if action == "verify":
                return ledger.verify(data["proof"],source=self.pin["verifier_id"])
            if action == "stopped":
                return ledger.confirm_stopped(resource,data["proof"],source=self.pin["verifier_id"])
        raise PhysicalError("plugin.permission_denied","role cannot perform this operation")


async def serve(controller, directory, state, site):
    async def handle(reader, writer, role):
        try:
            size, = struct.unpack("!I",await asyncio.wait_for(reader.readexactly(4),3))
            require(0 < size <= LIMIT,"plugin.manifest_invalid","invalid message length")
            request = json.loads(await asyncio.wait_for(reader.readexactly(size),3))
            result = {"value":controller.command(role,request)}
        except Exception as error:
            result = {"error":getattr(error,"code","plugin.request_invalid"),"detail":str(error)[:256]}
        try:
            writer.write(encode(result))
            await writer.drain()
        finally:
            writer.close()

    async def observe():
        last_report = time.monotonic()
        last_config_check = 0
        interval = 60000
        while True:
            controller.ledger.expire()
            now = time.monotonic()
            if now - last_config_check >= 1:
                try:
                    with (site / "runtime-config.json").open("rb") as stream:
                        raw = stream.read(4097)
                except FileNotFoundError:
                    raw = b'{"diagnostic_interval_ms":60000}'
                except OSError:
                    raw = b"unreadable"
                interval = controller.ledger.diagnostic_config(controller.resource, raw)
                last_config_check = now
            if now - last_report >= interval/1000:
                with controller.ledger.write():
                    controller.ledger.event(controller.resource,"diagnostic", phase=controller.ledger.device(controller.resource)["phase"])
                last_report = now
            await asyncio.sleep(.2)

    servers = []
    with deployment_owner(state):
        try:
            for role in controller.socket_roles:
                root = directory / role
                root.mkdir(exist_ok=True,parents=True)
                target = root / "physical.sock"
                if target.exists() or target.is_symlink():
                    require(not target.is_symlink() and target.is_socket(),"plugin.permission_denied","unexpected socket path")
                    target.unlink()
                server = await asyncio.start_unix_server(lambda r,w,role=role:handle(r,w,role),path=target)
                target.chmod(0o660)
                servers.append(server)
            await asyncio.gather(observe(),*(server.serve_forever() for server in servers))
        finally:
            for server in servers:
                server.close()
                await server.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",nargs="?",default="serve",
                        choices=("install","serve","status","drain","release","process-stopped"))
    parser.add_argument("--site",type=Path,default=Path("/site"))
    parser.add_argument("--state",type=Path,default=Path("/ledger"))
    parser.add_argument("--sockets",type=Path,default=Path("/sockets"))
    parser.add_argument("--image-digest",default=os.environ.get("EDGE_PHYSICAL_PLATFORM_DIGEST"))
    parser.add_argument("--operation")
    parser.add_argument("--inspection-file",type=Path)
    args = parser.parse_args()
    deployment = load_deployment(args.site, args.image_digest)
    resource = deployment["pin"]["resource_id"]
    ledger = PhysicalLedger(args.state,Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    try:
        if args.action == "serve":
            asyncio.run(serve(Controller(ledger,deployment),args.sockets,args.state,args.site))
            return
        if args.action == "install":
            current = ledger.db.execute("SELECT deployment,phase FROM physical_device WHERE resource=?", (resource,)).fetchone()
            if current and current[1]=="ACTIVE" and json.loads(current[0]) == deployment:
                result = ledger.device(resource)
            else:
                with deployment_owner(args.state):
                    result = ledger.install(deployment)
        else:
            require(ledger.device(resource)["deployment"] == deployment,"tool.binding_unavailable","wrong deployed site")
            if args.action == "status":
                ledger.expire()
                result = ledger.status(args.operation) if args.operation else {
                    **ledger.device(resource),"operations":[{"operation_id":op,"state":state,"settled":bool(settled)}
                        for op,state,settled in ledger.db.execute("SELECT op,state,settled FROM physical_ops ORDER BY rowid")]}
            elif args.action == "drain":
                result = ledger.drain(resource)
            elif args.action == "release":
                result = ledger.release(resource)
            else:
                require(args.inspection_file,"plugin.handoff_unconfirmed","provide a trusted Docker exit inspection file")
                proof = json.loads(args.inspection_file.read_bytes())
                if isinstance(proof,list) and len(proof)==1:
                    proof = proof[0]
                result = ledger.process_stopped(resource,proof)
        print(json.dumps(result,ensure_ascii=False))
    finally:
        ledger.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error":getattr(error,"code","plugin.deployment_failed"),"detail":str(error)[:300]}),file=sys.stderr)
        raise SystemExit(1)
