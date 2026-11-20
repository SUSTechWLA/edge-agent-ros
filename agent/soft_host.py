"""Isolated Python plugin host. A single event loop owns Fiber and registry reconciliation."""
import asyncio
from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import struct
from urllib.parse import urlsplit

from author_tool import public_reference
from module_journal import ModuleJournal, canonical
from plugin_descriptor import resolve_schema, trusted_code, validate_descriptor, validate_instance
from soft_client import LIMIT, encode
from soft_runtime import SoftRuntime, SoftRuntimeError
from tracing import clean_text
from v2_registry import V2Registry


def problem(code, detail):
    return SoftRuntimeError(code, detail)


def check_site_settings(values):
    """Site settings are non-secret JSON. Credentials travel by reference only."""
    if isinstance(values, dict):
        if set(values) == {"secret_ref"}:
            reference = values["secret_ref"]
            if not isinstance(reference,str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_./-]{0,127}", reference):
                raise problem("plugin.config_invalid", "expected an opaque site credential reference")
            return
        for key, value in values.items():
            sensitive = re.search(r"(password|passwd|secret|token|api.?key|authorization|credential|cookie|private.?key)$",
                                  key, re.I)
            if sensitive and not (isinstance(value, dict) and set(value) == {"secret_ref"}):
                raise problem("plugin.config_invalid", "site credentials require a reference")
            check_site_settings(value)
    elif isinstance(values, list):
        for element in values:
            check_site_settings(element)
    elif isinstance(values, str) and (clean_text(values) != values or "PRIVATE KEY-----" in values):
        raise problem("plugin.config_invalid", "raw credentials are forbidden in site settings")


def settings_for(item, site):
    schema = resolve_schema(item["config_schema"], site / "trust")
    # A migration route and its legacy wire contract belong to the immutable
    # descriptor. The optional operator file can change ordinary settings only.
    profile = schema.get("properties", {}).get("profile", {}).get("const")
    f = site / "config" / (item["id"] + ".json")
    try:
        with f.open("rb") as stream:
            raw = stream.read(131073)
        if len(raw) > 131072:
            raise ValueError("configuration size")
        config = json.loads(raw)
    except FileNotFoundError:
        config = {}
    except (OSError, ValueError) as error:
        raise problem("plugin.config_invalid", "invalid site configuration") from error
    check_site_settings(config)
    result = {"profile": profile, "settings": config} if profile is not None else config
    validate_instance(schema, result)
    return profile or {}, result


def http_exchange(address, payload, timeout):
    url = urlsplit(address)
    if url.scheme != "http" or not url.hostname or url.username or url.password or url.fragment:
        raise problem("plugin.permission_denied", "invalid private HTTP adapter address")
    conn = HTTPConnection(url.hostname, url.port, timeout=timeout)
    try:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        conn.request("GET" if body is None else "POST", url.path + ("?" + url.query if url.query else ""),
                     body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT or 300 <= response.status < 400:
            raise problem("plugin.receipt_invalid", "provider response size or route invalid")
        return response.status, json.loads(raw)
    finally:
        conn.close()


def adapter(item, site, profile):
    if item["runtime"] == "inproc":
        data = trusted_code(item, site / "trust")
        namespace = {"__name__": item["entrypoint"].split(":")[0]}
        def apply(ctx):
            exec(compile(data, "<approved:" + item["content_digest"] + ">", "exec"), namespace)
            return namespace["apply"](ctx)
        return apply
    for key in ("endpoint", "probe"):
        url = urlsplit(profile[key])
        if url.scheme != "http" or url.hostname not in item["network"]["egress"] or url.username or url.password or url.fragment:
            raise problem("plugin.permission_denied", "route outside approved soft network")
    async def invoke(wire):
        status, result = await asyncio.to_thread(http_exchange, profile["endpoint"], wire, 8)
        if item["kind"] == "service":
            return [status, result]
        if status != 200:
            raise problem("plugin.transport" if status >= 500 else "tool.provider_failed", f"provider HTTP {status}")
        return result
    def apply(ctx):
        for export in item["provides"]:
            if export["type"] == "tool":
                ctx.tools.register(export["name"], invoke)
            elif export["type"] == "service":
                ctx.provide(export["name"], invoke)
            else:
                raise problem("plugin.manifest_invalid", "unsupported HTTP contract")
    return apply


class Host:
    def __init__(self, registry, site, journal, image_digest):
        self.registry = registry
        self.site = site
        self.image_digest = image_digest
        self.journal = journal
        self.rt = SoftRuntime(self.record, lambda rule: resolve_schema(rule, site / "trust"))
        self.items = {}
        self.errors = {}
        self.probed = {}
        self.schemas = {}
        self.generations = {}
        self.loaded = set()
        self.invocations = {}
        self.lock = asyncio.Lock()
        self.journal.db.execute("""CREATE TABLE IF NOT EXISTS soft_operations(
            operation TEXT PRIMARY KEY, digest TEXT NOT NULL, input_hash TEXT NOT NULL, receipt TEXT)""")
        self.journal.db.execute("""CREATE TABLE IF NOT EXISTS soft_instances(
            plugin_id TEXT PRIMARY KEY, digest TEXT NOT NULL, publication INTEGER NOT NULL, lifecycle TEXT NOT NULL)""")
        # Immutable snapshots are keyed by the durable pin. Never put these values
        # into the public journal events, registration catalog or task's wire pin.
        self.journal.db.execute("""CREATE TABLE IF NOT EXISTS soft_config_snapshots(
            plugin_id TEXT NOT NULL, digest TEXT NOT NULL, config_digest TEXT NOT NULL, settings TEXT NOT NULL,
            PRIMARY KEY(plugin_id,digest,config_digest))""")

    def record(self, event):
        with self.journal.db:
            self.journal.emit({"module":"soft.runtime", "layer":"plugin", "kind":event["type"],
                               "code":event["type"], "trace_id":event.get("generation") or event["plugin_id"],
                               "data":event})
            if event["type"] == "plugin.lifecycle":
                key = event["plugin_id"]
                self.journal.db.execute("INSERT OR REPLACE INTO soft_instances VALUES(?,?,?,?)",
                    (key, event["content_digest"], self.generations[key], event["to"]))

    async def reconcile(self):
        async with self.lock:
            entries = self.registry.entries()
            for row in entries:
                item = row["descriptor"]
                key = item["id"]
                try:
                    await self.reconcile_one(row, item, key)
                except Exception as error:
                    code = getattr(error, "code", "plugin.manifest_invalid")
                    previous = self.errors.get(key)
                    self.errors[key] = code
                    if key in self.items:
                        await self.rt.set_health(key, False)
                        self.probed.pop(key, None)
                    if previous != code:
                        self.record({"type":"plugin.load_rejected", "plugin_id":key, "version":item["version"],
                                     "content_digest":item["content_digest"], "error":code})
                else:
                    self.errors.pop(key, None)

    async def reconcile_one(self, row, item, key):
        if row["disposed"]:
            return
        if item["class"] != "soft" or item["provenance"]["image_digest"] != self.image_digest:
            raise problem("plugin.permission_denied", "runtime image or class differs from deployment")
        self.generations[key] = row["generation"]
        history = self.journal.db.execute(
            "SELECT lifecycle FROM soft_instances WHERE plugin_id=? AND digest=? AND publication=?",
            (key, item["content_digest"], row["generation"])).fetchone()
        if key not in self.loaded and history and history[0] in ("LOADING", "UNLOADING", "FAILED"):
            raise problem("plugin.cleanup_incomplete", "previous instance needs operator reconciliation")
        if row["draining"] and not row["frozen"]:
            if key in self.loaded:
                await self.rt.remove(key)
            elif history and history[0] != "DISPOSED":
                raise problem("plugin.cleanup_incomplete", "old process did not confirm disposal")
            # No lifecycle record means apply never started. DISPOSED is durable proof.
            self.registry.ack_disposed(key, row["revision"])
            self.items.pop(key, None)
            self.probed.pop(key, None)
            self.schemas.pop(key, None)
            self.loaded.discard(key)
            return
        trust = self.site / "trust"
        await asyncio.to_thread(validate_descriptor, item, trust)
        profile, config = await asyncio.to_thread(settings_for, item, self.site)
        current = self.items.get(key)
        if not current or current[0]["content_digest"] != item["content_digest"]:
            schemas = {name:resolve_schema(item[name], trust) for name in ("parameters", "outcomes", "config_schema")}
            entry = adapter(item, self.site, profile)
            validate_descriptor(item, trust)
            if key in self.loaded:
                raise problem("plugin.cleanup_incomplete", "installed instance requires explicit handoff")
            try:
                await self.rt.install(dict(item, **schemas), entry, config, healthy=item["runtime"] != "process")
            finally:
                # A failed apply may already own partial resources.
                if any(r["id"] == key and r["lifecycle"] != "DISPOSED" for r in self.rt.catalog()):
                    self.loaded.add(key)
            self.items[key] = item, profile, config
            self.schemas[key] = schemas
        elif current[2] != config and not row["draining"]:
            await self.rt.configure(key, config)
            self.items[key] = item, profile, config
        if item["runtime"] == "process":
            now = asyncio.get_running_loop().time()
            last = self.probed.get(key)
            if last is None or now - last >= item["health"]["ttl_ms"] / 2000:
                try:
                    status, body = await asyncio.to_thread(http_exchange, profile["probe"], None,
                                                           item["health"]["timeout_ms"] / 1000)
                    healthy = status == 200 and isinstance(body, dict) and bool(body) and all(
                        name in body and canonical(body[name]) == canonical(expected)
                        for name, expected in profile.get("health_expected", {}).items())
                except Exception:
                    healthy = False
                await self.rt.set_health(key, healthy)
                self.probed[key] = now
        elif key in self.errors:
            await self.rt.set_health(key, True)
        # The durable registry bars new plans immediately. A process being restored
        # under drain may first need a healthy PENDING -> ACTIVE transition for old pins.
        if row["draining"] and any(r["id"] == key and r["lifecycle"] == "ACTIVE" for r in self.rt.catalog()):
            await self.rt.drain(key)

    def check_dependencies(self, pin):
        now = asyncio.get_running_loop().time()
        health = {row["id"]:row["health"] for row in self.rt.catalog()}
        for key in pin["dependencies"]:
            if key in self.errors or key not in self.items or health.get(key) != "ready":
                raise problem("plugin.dependency_unavailable", "required plugin is unavailable: " + key)
            item = self.items[key][0]
            if item["runtime"] == "process" and (
                    key not in self.probed or now - self.probed[key] > item["health"]["ttl_ms"] / 1000):
                raise problem("plugin.dependency_unavailable", "provider health is stale: " + key)

    def catalog(self):
        claims = self.registry.claimed_exports()
        rows = {row["id"]: row for row in self.rt.catalog()}
        tools = []
        services = {}
        entries = self.registry.entries()
        accepting = {e["descriptor"]["id"] for e in entries if not e["draining"] and not e["disposed"]}
        for entry in entries:
            item = entry["descriptor"]
            key = item["id"]
            live = rows.get(key)
            if not live:
                live = rows[key] = {"id":key, "version":item["version"], "kind":item["kind"], "class":item["class"],
                                    "content_digest":item["content_digest"], "lifecycle":"FAILED" if key in self.errors else "PENDING",
                                    "health":"unavailable", "availability":"unavailable"}
            live.update(revision=entry["revision"], frozen=entry["frozen"], draining=entry["draining"])
            if key in self.errors:
                live.update(last_error=self.errors[key], health="unavailable", availability="unavailable")
            if live["availability"] != "available" or entry["draining"] or key not in self.items:
                continue
            profile = self.items[key][1]
            for ref in item["provides"]:
                if ref["type"] not in ("tool", "service"):
                    continue
                pin = self.rt.preview(key, ref["name"])
                if not accepting.issuperset(pin["dependencies"]):
                    live.update(availability="unavailable")
                    continue
                try:
                    self.check_dependencies(pin)
                except SoftRuntimeError:
                    live.update(availability="unavailable", health="unavailable")
                    continue
                legacy = profile.get("legacy")
                if ref["type"] == "service":
                    services[ref["name"]] = {"pin":pin, "legacy":legacy}
                    continue
                schema = self.schemas[key]["parameters"]
                cap = dict(legacy or {"id":ref["name"], "version":item["version"],
                                     "description":schema.get("description", ref["name"]), "effect":item["effects"],
                                     "parameters":{k:v["type"] for k,v in schema.get("properties", {}).items()}})
                if cap["id"] != ref["name"] or cap["effect"] != item["effects"] or cap["id"] in ("inspect","status"):
                    raise problem("plugin.permission_denied", "invalid tool takeover")
                cap.update(_soft=pin, _legacy_contract=legacy, _parameter_schema=schema)
                tools.append(cap)
        return {"plugins":list(rows.values()), "tools":tools, "services":services, "claims":claims}

    async def freeze_plan(self, data):
        task = data["task"]
        previous = self.registry.saved_plan(task)
        if previous is not None:
            return previous
        if not public_reference(task):
            raise problem("plugin.manifest_invalid", "invalid task identity")
        tools = {cap["id"]:cap for cap in self.catalog()["tools"]}
        registered, bindings = [], []
        try:
            for index, step in enumerate(data["steps"], 1):
                pin = step.get("soft_pin")
                if not pin:
                    continue
                cap = tools.get(step["tool"])
                if (not cap or cap["_soft"] != pin or step["version"] != cap["version"]
                        or step["binding"] != pin["binding"]):
                    raise problem("tool.binding_unavailable", "step contract differs from catalog")
                self.validate_payload(pin["plugin_id"], "parameters", step["args"])
                step["soft_cap"] = {k:v for k,v in cap.items() if k not in ("_soft","_legacy_contract")}
                op = f"{task}/step/{index}"
                exact = dict(pin, reservation_id=op)
                got = await self.rt.freeze(pin["plugin_id"], pin["name"], op, expected_pin=exact)
                registered.append(op)
                self.save_settings(got)
                step["soft_pin"] = got
                bindings.extend((key, version[1], op) for key,version in got["dependencies"].items())
            return self.registry.freeze_plan(task, data["provider"], data["steps"], bindings)
        except BaseException:
            for op in registered:
                await self.rt.release(op)
            raise

    def save_settings(self, pin):
        # Commit immutable data first. A crash before plan admission can leave an
        # unreferenced snapshot but can never expose a plan missing its configuration.
        frozen = self.rt.frozen_settings(pin["reservation_id"])
        with self.journal.db:
            for key, (version, digest, config_digest) in pin["dependencies"].items():
                config = frozen[key]
                schema = self.schemas[key]["config_schema"]
                profile = schema.get("properties", {}).get("profile", {}).get("const")
                settings = config["settings"] if profile is not None else config
                check_site_settings(settings)
                self.journal.db.execute("INSERT OR IGNORE INTO soft_config_snapshots VALUES(?,?,?,?)",
                                        (key, digest, config_digest, canonical(settings)))

    def restored_settings(self, pin):
        values = {}
        for key, (version, digest, config_digest) in pin["dependencies"].items():
            item, profile, current = self.items[key]
            if item["version"] != version or item["content_digest"] != digest:
                raise problem("tool.binding_unavailable", "configuration owner differs from the durable pin")
            row = self.journal.db.execute("""SELECT settings FROM soft_config_snapshots
                WHERE plugin_id=? AND digest=? AND config_digest=?""", (key,digest,config_digest)).fetchone()
            if row is None:
                # Earlier P1 journals can still restore if the complete current
                # configuration has the exact hash in the saved plan.
                values[key] = current
                continue
            try:
                config = json.loads(row[0])
            except ValueError as error:
                raise problem("tool.binding_unavailable", "stored configuration is unreadable") from error
            check_site_settings(config)
            schema = self.schemas[key]["config_schema"]
            has_profile = schema.get("properties", {}).get("profile", {}).get("const") is not None
            values[key] = {"profile":profile, "settings":config} if has_profile else config
        # SoftRuntime recomputes and checks every transitive digest and validates
        # schema before binding the historical values to just this reservation.
        return values

    def pinned_step(self, pin):
        op = pin.get("reservation_id")
        if not public_reference(op):
            raise problem("tool.binding_unavailable", "invalid reservation")
        task, _, index = op.rpartition("/step/")
        plan = self.registry.saved_plan(task)
        if (not plan or not index.isascii() or not index.isdecimal() or not 1 <= int(index) <= len(plan[1])
                or index != str(int(index))):
            raise problem("tool.binding_unavailable", "no durable step owns this operation")
        step = plan[1][int(index)-1]
        if step.get("soft_pin") != pin:
            raise problem("tool.binding_unavailable", "pin differs from the durable plan")
        return task, step

    def validate_payload(self, key, field, value):
        try:
            validate_instance(self.schemas[key][field], value)
        except ValueError as exc:
            code = "tool.receipt_invalid" if field == "outcomes" else "tool.arguments_invalid"
            raise problem(code, "wire payload violates the pinned " + field + " schema") from exc

    def operation_event(self, state, task, pin, wire, *, reason=None):
        trace = wire.get("trace") or {}
        self.journal.emit({"module":"soft.runtime","layer":"plugin","kind":"plugin.operation." + state.lower(),
            "code":"plugin.operation." + state.lower(),"task_id":task,"operation_id":pin["reservation_id"],
            "trace_id":trace.get("trace_id") or task,"parent_event_id":trace.get("parent_event_id"),
            "data":{**{k:pin[k] for k in ("plugin_id","version","content_digest","binding")},
                    **({"reason":reason} if reason else {})}})

    async def invoke(self, pin, wire):
        op = pin["reservation_id"]
        task, step = self.pinned_step(pin)
        expected = {k:v[1] for k,v in pin["dependencies"].items()}
        trace = wire.get("trace")
        if (self.registry.reservations_for(op) != expected or wire.get("operation_id") != op
                or wire.get("version") != step["version"] or canonical(wire.get("arguments")) != canonical(step["args"])
                or set(wire) - {"operation_id","arguments","version","trace"}
                or (trace is not None and (not isinstance(trace,dict) or trace.get("task_id") != task
                    or any(x is not None and not public_reference(x)
                           for x in (trace.get("trace_id"),trace.get("parent_event_id")))))):
            raise problem("tool.binding_unavailable", "operation, arguments or scope differ from the frozen plan")
        self.check_dependencies(pin)
        await self.rt.freeze(pin["plugin_id"], pin["name"], op, expected_pin=pin, restore=True,
                             restored_settings=self.restored_settings(pin))
        item = self.items[pin["plugin_id"]][0]
        self.validate_payload(pin["plugin_id"], "parameters", wire["arguments"])
        if self.registry.reservations_for(op) != expected:
            # freeze() can yield. A never-issued compensation may have fenced
            # this invocation during that await, before it recorded its intent.
            await self.rt.release(op)
            raise problem("tool.binding_unavailable", "reservation was released before operation intent")
        # The persisted intent prevents retrying an unknown software write after a lost response.
        raw = json.dumps(wire, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()
        fingerprint = hashlib.sha256(raw).hexdigest()
        if op in self.invocations:
            active_hash, active = self.invocations[op]
            if active_hash != fingerprint:
                raise problem("tool.binding_unavailable", "operation payload changed")
            return await asyncio.shield(active)
        db = self.journal.db
        existing = db.execute("SELECT digest,input_hash,receipt FROM soft_operations WHERE operation=?", (op,)).fetchone()
        if existing:
            if existing[:2] != (pin["content_digest"], fingerprint):
                raise problem("tool.binding_unavailable", "operation payload changed")
            if existing[2]:
                previous = json.loads(existing[2])
                if previous.get("state") != "RUNNING":
                    return previous
            elif item["effects"] != "read":
                raise problem("tool.outcome_unknown", "an earlier write requires reconciliation")
        else:
            with db:
                db.execute("INSERT INTO soft_operations VALUES(?,?,?,NULL)", (op,pin["content_digest"],fingerprint))
                self.operation_event("intent", task, pin, wire)
        active = asyncio.create_task(self._call(task, pin, wire))
        self.invocations[op] = fingerprint, active
        def completed(result):
            self.invocations.pop(op, None)
            if not result.cancelled():
                result.exception()
        active.add_done_callback(completed)
        return await asyncio.shield(active)

    async def _call(self, task, pin, wire):
        op = pin["reservation_id"]
        db = self.journal.db
        try:
            receipt = await self.rt.call(pin, wire)
            if not isinstance(receipt,dict) or receipt.get("operation_id") != op:
                raise problem("tool.receipt_invalid", "provider returned a different operation")
            if receipt.get("state") == "SUCCEEDED":
                self.validate_payload(pin["plugin_id"], "outcomes", receipt.get("evidence"))
            if receipt.get("state") not in ("RUNNING", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELED"):
                raise problem("tool.receipt_invalid", "provider returned an unknown operation state")
        except Exception as error:
            code = getattr(error, "code", "plugin.provider_unavailable")
            with db:
                self.operation_event("error", task, pin, wire, reason=code)
            if isinstance(error, SoftRuntimeError):
                raise
            raise problem(code, "provider call failed; reconcile the original operation") from error
        with db:
            db.execute("UPDATE soft_operations SET receipt=? WHERE operation=?", (canonical(receipt),op))
            self.operation_event(receipt["state"], task, pin, wire)
        return receipt

    async def command(self, data, session_only=False):
        action = data["action"]
        if session_only and action not in ("session_pin", "session"):
            raise problem("plugin.permission_denied", "session consumer cannot change task reservations")
        if action == "catalog":
            return self.catalog()
        if action == "freeze_plan":
            return await self.freeze_plan(data)
        if action == "call":
            return await self.invoke(data["pin"], data["wire"])
        if action in ("settle","settle_unissued"):
            pin = data["pin"]
            self.pinned_step(pin)
            if pin["reservation_id"] in self.invocations:
                raise problem("plugin.draining", "managed operation is active")
            op = pin["reservation_id"]
            expected = {k:v[1] for k,v in pin["dependencies"].items()}
            if action == "settle_unissued":
                if self.journal.db.execute("SELECT 1 FROM soft_operations WHERE operation=?", (op,)).fetchone():
                    raise problem("plugin.handoff_unconfirmed", "operation already has a durable dispatch intent")
                # No await between checking the operation journal and durably
                # fencing admission. One deployed host owns both ledgers.
                self.registry.settle_operation(op, expected)
                await self.rt.release(op)
            else:
                await self.rt.release(op)
                self.registry.settle_operation(op, expected)
            return True
        if action == "session_pin":
            view = self.catalog()
            return {"claimed":"session.read" in view["claims"], "service":view["services"].get("session.read")}
        if action == "session":
            pin, wire = data["pin"], data["wire"]
            if pin["name"] != "session.read" or wire["binding"] != pin["binding"].removeprefix("sha256:"):
                raise problem("tool.binding_unavailable", "session binding differs")
            self.check_dependencies(pin)
            current = self.catalog()["services"].get("session.read")
            if not current or current["pin"] != pin:
                raise problem("tool.binding_unavailable", "session admission changed")
            op = "session/" + wire["request_id"]
            self.validate_payload(pin["plugin_id"], "parameters", wire)
            # Session queries are ephemeral reads. The open Fiber reference bars
            # local disposal until this read returns; a restart terminates the read.
            exact = await self.rt.freeze(pin["plugin_id"], pin["name"], op, expected_pin=dict(pin,reservation_id=op))
            try:
                response = await self.rt.call(exact, wire)
                current = self.catalog()["services"].get("session.read")
                if not current or current["pin"] != pin:
                    raise problem("session.binding_changed", "session changed during read")
                if response[0] == 200:
                    self.validate_payload(pin["plugin_id"], "outcomes", response[1])
                return response
            finally:
                await self.rt.release(op)
        raise problem("plugin.permission_denied", "unknown command")


@contextmanager
def socket_owner(directory):
    """An OS-backed exclusive lock expires with the process, across restarts."""
    lock = sqlite3.connect(directory / "agent" / ".host-lock.sqlite", timeout=0)
    try:
        lock.execute("BEGIN EXCLUSIVE")
        yield
    finally:
        lock.close()


async def serve(host, directory):
    with socket_owner(directory):
        await _serve(host, directory)


async def _serve(host, directory):
    async def handle(reader, writer, session_only):
        try:
            size, = struct.unpack("!I", await asyncio.wait_for(reader.readexactly(4), 3))
            if not 0 < size <= LIMIT:
                raise ValueError("invalid request size")
            data = json.loads(await asyncio.wait_for(reader.readexactly(size), 3))
            reply = {"value":await host.command(data, session_only)}
        except Exception as error:
            reply = {"error":getattr(error, "code", "plugin.request_invalid"), "detail":str(error)[:256]}
        try:
            writer.write(encode(reply))
            await writer.drain()
        finally:
            writer.close()
    async def watch():
        while True:
            try:
                await host.reconcile()
            except Exception:
                logging.exception("soft catalog reconciliation failed")
            await asyncio.sleep(.4)
    await host.reconcile()
    servers = []
    for name, limited in (("agent.sock",False), ("session.sock",True)):
        target = directory / ("session" if limited else "agent") / name
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_socket():
                raise problem("plugin.permission_denied", "unexpected object at managed socket path")
            target.unlink()
        server = await asyncio.start_unix_server(lambda r,w,flag=limited:handle(r,w,flag), path=target)
        target.chmod(0o660)
        servers.append(server)
    await asyncio.gather(watch(), *(server.serve_forever() for server in servers))


if __name__ == "__main__":
    with V2Registry(Path("/registry")) as registry:
        journal = ModuleJournal("/journal/module-events.sqlite")
        try:
            asyncio.run(serve(Host(registry, Path("/site"), journal, os.environ["EDGE_SOFT_PLATFORM_DIGEST"]),
                              Path("/sockets")))
        finally:
            journal.close()
