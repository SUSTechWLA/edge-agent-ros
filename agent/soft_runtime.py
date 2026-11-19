"""SDD-045. Single-event-loop Cordis-style host for already admitted soft plugins.

The caller owns artifact verification, durable plan/operation records and process
isolation. Only the trusted host supplies apply functions. JSON cannot import code.
"""
import asyncio
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import uuid

from plugin_descriptor import validate_instance


class SoftRuntimeError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


async def _invoke(callback, *args):
    result = callback(*args)
    return await result if inspect.isawaitable(result) else result


@dataclass(eq=False)
class Fiber:
    descriptor: dict
    apply: object
    settings: dict
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "PENDING"
    healthy: bool = True
    draining: bool = False
    available: bool = False
    error: str | None = None
    cleanup_failed: list = field(default_factory=list)
    config_checks: list = field(default_factory=list)
    effects: list = field(default_factory=list)
    exports: dict = field(default_factory=dict)
    listeners: list = field(default_factory=list)
    references: set = field(default_factory=set)
    active: int = 0

    @property
    def id(self):
        return self.descriptor["id"]

    def declared(self, field_name, kind):
        return {ref["name"]: ref for ref in self.descriptor[field_name] if ref["type"] == kind}


@dataclass
class Reservation:
    pin: dict
    closure: dict
    settings: dict
    task: asyncio.Task | None = None
    argument_digest: str | None = None
    running: bool = False


class _Tools:
    def __init__(self, ctx):
        self._ctx = ctx

    def register(self, name, handler):
        self._ctx._register("tool", name, handler)


class Context:
    def __init__(self, runtime, fiber):
        self._runtime = runtime
        self._fiber = fiber
        self.tools = _Tools(self)

    def effect(self, disposer):
        if self._fiber.state not in ("LOADING", "ACTIVE") or self._fiber.draining:
            raise SoftRuntimeError("plugin.draining", "registration on an inactive context")
        if not callable(disposer):
            raise TypeError("effect requires a disposer")
        self._fiber.effects.append(disposer)

    def _register(self, kind, name, handler):
        fiber = self._fiber
        if name not in fiber.declared("provides", kind) or not callable(handler):
            raise SoftRuntimeError("plugin.permission_denied", "undeclared or invalid export", name=name)
        if name in fiber.exports:
            raise SoftRuntimeError("plugin.service_conflict", "duplicate registration", name=name)
        # Only loading can change the public contract of this generation.
        if fiber.state != "LOADING":
            raise SoftRuntimeError("plugin.manifest_invalid", "exports are frozen on activation")
        self.effect(lambda: fiber.exports.pop(name))
        fiber.exports[name] = handler

    def provide(self, name, handler):
        self._register("service", name, handler)

    def require(self, name):
        if name not in self._fiber.declared("requires", "service"):
            raise SoftRuntimeError("plugin.permission_denied", "undeclared injection", name=name)

        async def invoke(arguments):
            reservation = self._runtime._scope.get()
            if reservation is None or not reservation.running or self._fiber not in reservation.closure.values():
                raise SoftRuntimeError("tool.binding_unavailable", "service call outside bound work", name=name)
            provider = self._runtime._provider(self._fiber, name)
            if provider is None or provider not in reservation.closure.values():
                raise SoftRuntimeError("plugin.dependency_unavailable", "bound service unavailable", name=name)
            self._runtime._closure(provider, allow_draining=True)
            provider.active += 1
            try:
                return await _invoke(provider.exports[name], arguments)
            finally:
                provider.active -= 1

        return invoke

    def on(self, event, handler):
        if event not in self._fiber.declared("requires", "event") or not callable(handler):
            raise SoftRuntimeError("plugin.permission_denied", "undeclared event subscription", name=event)
        entry = (event, handler)
        self.effect(lambda: self._fiber.listeners.remove(entry))
        self._fiber.listeners.append(entry)

    def config(self, schema=None):
        scope = self._runtime._scope.get()
        settings = (scope.settings[self._fiber.id] if scope and self._fiber.id in scope.settings
                    else self._fiber.settings)
        if schema is not None:
            self._runtime._validate(schema, settings)
            if self._fiber.state == "LOADING" and schema not in self._fiber.config_checks:
                self._fiber.config_checks.append(deepcopy(schema))
        # No plugin can mutate the live configuration or another call's snapshot.
        return deepcopy(settings)


class SoftRuntime:
    """Use on one event loop. Management transactions serialize across await points."""

    def __init__(self, on_event, resolve_schema=lambda schema: schema):
        self._sink = on_event
        self._resolve_schema = resolve_schema
        self._fibers = {}
        self._versions = {}
        self._reservations = {}
        self._manage = asyncio.Lock()
        self._scope = ContextVar("soft_plugin_call", default=None)
        self._audit_fault = False

    def _emit(self, fiber, kind, **details):
        try:
            self._sink({"type": kind, "plugin_id": fiber.id, "version": fiber.descriptor["version"],
                        "content_digest": fiber.descriptor["content_digest"],
                        "generation": fiber.generation, **details})
        except Exception as exc:
            # No further admission or execution without auditable ownership.
            self._audit_fault = True
            raise SoftRuntimeError("plugin.audit_unavailable", "lifecycle sink unavailable") from exc

    def _audited(self):
        if self._audit_fault:
            raise SoftRuntimeError("plugin.audit_unavailable", "audit recovery required")

    def _transition(self, fiber, state, **details):
        self._emit(fiber, "plugin.lifecycle", **{"from": fiber.state, "to": state}, **details)
        fiber.state = state

    def _validate(self, schema, value):
        try:
            validate_instance(self._resolve_schema(schema), value)
        except ValueError as exc:
            raise SoftRuntimeError("plugin.config_invalid", "configuration rejected by schema") from exc

    def _find(self, plugin_id):
        fiber = self._fibers.get(plugin_id)
        if fiber is None:
            raise SoftRuntimeError("plugin.service_missing", "unknown plugin", plugin_id=plugin_id)
        return fiber

    def _provider(self, consumer, name):
        required = consumer.declared("requires", "service")[name]
        for fiber in self._fibers.values():
            provided = fiber.declared("provides", "service").get(name)
            if (fiber.state == "ACTIVE" and name in fiber.exports and provided
                    and required["schema_ref"] == provided["schema_ref"]):
                return fiber
        return None

    def _closure(self, root, allow_draining, *, loading=False):
        closure = {}

        def visit(fiber, path):
            if fiber.id in path:
                raise SoftRuntimeError("plugin.dependency_cycle", "cyclic injection", plugin_id=fiber.id)
            if fiber.id in closure:
                return
            if not fiber.healthy or (fiber.state != "ACTIVE" and not (loading and fiber is root)):
                raise SoftRuntimeError("plugin.dependency_unavailable", "plugin unavailable", plugin_id=fiber.id)
            if fiber.draining and not allow_draining:
                raise SoftRuntimeError("plugin.draining", "plugin is draining", plugin_id=fiber.id)
            closure[fiber.id] = fiber
            for name in fiber.declared("requires", "service"):
                provider = self._provider(fiber, name)
                if provider is None:
                    raise SoftRuntimeError("plugin.dependency_unavailable", "required service unavailable", name=name)
                visit(provider, path | {fiber.id})

        visit(root, set())
        return closure

    def _availability(self):
        for fiber in self._fibers.values():
            if fiber.state != "ACTIVE":
                continue
            try:
                self._closure(fiber, allow_draining=False)
                available, reason = True, {}
            except SoftRuntimeError as exc:
                available, reason = False, {"code": exc.code, **exc.details}
            if fiber.available != available:
                self._emit(fiber, "plugin.availability", available=available, **reason)
                fiber.available = available

    def _row(self, fiber):
        descriptor = fiber.descriptor
        return {"id": fiber.id, "version": descriptor["version"], "content_digest": descriptor["content_digest"],
                "class": "soft", "kind": descriptor["kind"], "generation": fiber.generation,
                "lifecycle": fiber.state, "availability": "available" if fiber.available else "unavailable",
                "health": "ready" if fiber.healthy and fiber.state == "ACTIVE" else "unavailable",
                "draining": fiber.draining, "reservations": len(fiber.references), "inflight": fiber.active,
                "last_error": fiber.error, "cleanup_failed": list(fiber.cleanup_failed),
                "provides": deepcopy(descriptor["provides"]), "requires": deepcopy(descriptor["requires"])}

    def catalog(self):
        return [self._row(self._fibers[key]) for key in sorted(self._fibers)]

    def _candidate(self, descriptor, apply, config):
        self._audited()
        if descriptor["class"] != "soft" or descriptor["effects"] not in ("read", "idempotent"):
            raise SoftRuntimeError("plugin.class_violation", "only admitted software may run in this host")
        if not callable(apply):
            raise SoftRuntimeError("plugin.permission_denied", "a host-approved adapter is required")
        if any(ref["type"] not in ("service", "event") for ref in descriptor["requires"]):
            raise SoftRuntimeError("plugin.manifest_invalid", "unsupported dependency type in soft host")
        fiber = Fiber(deepcopy(descriptor), apply, deepcopy({} if config is None else config))
        self._validate(descriptor["config_schema"], fiber.settings)
        key = (fiber.id, descriptor["version"])
        if self._versions.get(key, descriptor["content_digest"]) != descriptor["content_digest"]:
            raise SoftRuntimeError("version_conflict", "published version identifies different content")
        exported = {ref["name"] for ref in descriptor["provides"]}
        for owner in self._fibers.values():
            if owner.id != fiber.id and owner.state != "DISPOSED":
                if any(r["name"] in exported for r in owner.descriptor["provides"]):
                    raise SoftRuntimeError("plugin.service_conflict", "another plugin owns this export",
                                           plugin_id=owner.id)
        return fiber

    async def _cleanup(self, fiber):
        if fiber.cleanup_failed:
            raise SoftRuntimeError("plugin.cleanup_incomplete", "previous cleanup is unconfirmed")
        effects = list(reversed(fiber.effects))
        fiber.effects.clear()
        deadline = asyncio.get_running_loop().time() + fiber.descriptor["resources"]["shutdown_ms"] / 1000
        for index, dispose in enumerate(effects):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                fiber.cleanup_failed.extend(range(index, len(effects)))
                break
            try:
                # wait_for can wait indefinitely if a plugin suppresses cancellation.
                # Quarantine an unconfirmed owner even if it later finishes.
                work = asyncio.create_task(_invoke(dispose))
                work.add_done_callback(lambda task: None if task.cancelled() else task.exception())
                done, _ = await asyncio.wait([work], timeout=remaining)
                if not done:
                    work.cancel()
                    raise TimeoutError
                work.result()
            except (Exception, asyncio.CancelledError):
                fiber.cleanup_failed.append(index)
        if fiber.cleanup_failed:
            fiber.error = "plugin.cleanup_incomplete"
            self._transition(fiber, "FAILED", code=fiber.error, unreleased=list(fiber.cleanup_failed))
            raise SoftRuntimeError(fiber.error, "disposer completion unconfirmed", unreleased=fiber.cleanup_failed)

    async def _activate_pending(self):
        # Installing one provider can make an entire pending dependency chain ready.
        while True:
            progressed = False
            for fiber in list(self._fibers.values()):
                if fiber.state != "PENDING" or fiber.draining:
                    continue
                try:
                    self._closure(fiber, allow_draining=False, loading=True)
                except SoftRuntimeError:
                    continue
                self._transition(fiber, "LOADING")
                try:
                    disposer = await _invoke(fiber.apply, Context(self, fiber))
                    if disposer is not None:
                        Context(self, fiber).effect(disposer)
                    expected = fiber.declared("provides", "tool") | fiber.declared("provides", "service")
                    if set(expected) != set(fiber.exports):
                        raise SoftRuntimeError("plugin.manifest_invalid", "apply did not register declared exports")
                except (Exception, asyncio.CancelledError) as exc:
                    fiber.error = getattr(exc, "code", "plugin.apply_failed")
                    self._transition(fiber, "FAILED", code=fiber.error)
                    await self._cleanup(fiber)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                else:
                    self._transition(fiber, "ACTIVE")
                progressed = True
            if not progressed:
                break
        self._availability()

    async def _install(self, fiber):
        self._emit(fiber, "plugin.lifecycle", **{"from": None, "to": "PENDING"})
        self._fibers[fiber.id] = fiber
        self._versions[fiber.id, fiber.descriptor["version"]] = fiber.descriptor["content_digest"]
        await self._activate_pending()
        if fiber.state == "FAILED":
            raise SoftRuntimeError(fiber.error, "plugin activation failed", plugin_id=fiber.id)
        return self._row(fiber)

    async def install(self, descriptor, apply, config=None):
        async with self._manage:
            fiber = self._candidate(descriptor, apply, config)
            old = self._fibers.get(fiber.id)
            if old and old.state != "DISPOSED":
                raise SoftRuntimeError("version_conflict", "ID already installed", plugin_id=fiber.id)
            return await self._install(fiber)

    def _close_admission(self, fiber):
        if not fiber.draining:
            self._emit(fiber, "plugin.drain", reservations=len(fiber.references), inflight=fiber.active)
            fiber.draining = True
            self._availability()

    async def drain(self, plugin_id):
        async with self._manage:
            fiber = self._find(plugin_id)
            self._close_admission(fiber)
            return self._row(fiber)

    async def _remove(self, fiber):
        if fiber.state == "DISPOSED":
            return self._row(fiber)
        self._close_admission(fiber)
        if fiber.references or fiber.active:
            raise SoftRuntimeError("plugin.draining", "frozen or executing work remains",
                                   reservations=len(fiber.references), inflight=fiber.active)
        if fiber.cleanup_failed:
            raise SoftRuntimeError("plugin.cleanup_incomplete", "release must be reconciled")
        self._transition(fiber, "UNLOADING")
        await self._cleanup(fiber)
        self._transition(fiber, "DISPOSED")
        self._availability()
        return self._row(fiber)

    async def remove(self, plugin_id):
        async with self._manage:
            return await self._remove(self._find(plugin_id))

    async def update(self, descriptor, apply, config=None):
        async with self._manage:
            fiber = self._candidate(descriptor, apply, config)
            old = self._find(fiber.id)
            if not old.descriptor["hot_reload"]:
                raise SoftRuntimeError("plugin.permission_denied", "code reload disabled")
            await self._remove(old)
            return await self._install(fiber)

    async def configure(self, plugin_id, config):
        async with self._manage:
            self._audited()
            fiber = self._find(plugin_id)
            if fiber.draining or fiber.state not in ("PENDING", "ACTIVE"):
                raise SoftRuntimeError("plugin.draining", "instance does not accept configuration")
            self._validate(fiber.descriptor["config_schema"], config)
            for schema in fiber.config_checks:
                self._validate(schema, config)
            self._emit(fiber, "plugin.config", config_digest=_digest(config))
            fiber.settings = deepcopy(config)
            return self._row(fiber)

    async def set_health(self, plugin_id, healthy):
        async with self._manage:
            fiber = self._find(plugin_id)
            if type(healthy) is not bool:
                raise ValueError("health must be boolean")
            if fiber.healthy != healthy:
                self._emit(fiber, "plugin.health", healthy=healthy)
                fiber.healthy = healthy
            await self._activate_pending()
            return self._row(fiber)

    def _snapshot(self, plugin_id, name, allow_draining):
        fiber = self._find(plugin_id)
        closure = self._closure(fiber, allow_draining=allow_draining)
        if name not in fiber.exports:
            raise SoftRuntimeError("tool.binding_unavailable", "requested export missing", name=name)
        settings = {key: deepcopy(owner.settings) for key, owner in closure.items()}
        dependencies = {key: [owner.descriptor["version"], owner.descriptor["content_digest"], _digest(settings[key])]
                        for key, owner in closure.items()}
        pin = {"plugin_id": plugin_id, "name": name,
               "version": fiber.descriptor["version"], "content_digest": fiber.descriptor["content_digest"],
               "binding": _digest([plugin_id, name, dependencies])}
        return Reservation(pin, closure, settings)

    def preview(self, plugin_id, name):
        self._audited()
        return self._snapshot(plugin_id, name, False).pin

    async def freeze(self, plugin_id, name, reservation_id, *, expected_pin=None, restore=False):
        self._audited()
        if not isinstance(reservation_id, str) or not reservation_id:
            raise SoftRuntimeError("tool.binding_unavailable", "reservation identity required")
        existing = self._reservations.get(reservation_id)
        if existing is not None:
            pin = existing.pin
            if (pin["plugin_id"] != plugin_id or pin["name"] != name
                    or (expected_pin is not None and expected_pin != pin)):
                raise SoftRuntimeError("version_conflict", "reservation belongs to different work")
            return deepcopy(pin)
        if restore and not expected_pin:
            raise SoftRuntimeError("tool.binding_unavailable", "restoring requires the original durable pin")
        reservation = self._snapshot(plugin_id, name, allow_draining=restore)
        pin = reservation.pin
        pin["reservation_id"] = reservation_id
        if expected_pin is not None and pin != expected_pin:
            raise SoftRuntimeError("tool.binding_unavailable", "cannot restore original binding")
        self._reservations[reservation_id] = reservation
        for owner in reservation.closure.values():
            owner.references.add(reservation_id)
        return deepcopy(pin)

    async def _execute(self, reservation, callback, arguments):
        token = self._scope.set(reservation)
        reservation.running = True
        try:
            for owner in reservation.closure.values():
                owner.active += 1
            return await _invoke(callback, arguments)
        finally:
            for owner in reservation.closure.values():
                owner.active -= 1
            reservation.running = False
            self._scope.reset(token)

    async def call(self, pin, arguments):
        self._audited()
        reservation = self._reservations.get(pin["reservation_id"])
        if reservation is None or reservation.pin != pin:
            raise SoftRuntimeError("tool.binding_unavailable", "no matching reservation")
        arg_digest = _digest(arguments)
        if reservation.task is None:
            fiber = self._find(pin["plugin_id"])
            closure = self._closure(fiber, allow_draining=True)
            if closure != reservation.closure:
                raise SoftRuntimeError("tool.binding_unavailable", "original implementation unavailable")
            reservation.argument_digest = arg_digest
            reservation.task = asyncio.create_task(self._execute(
                reservation, fiber.exports[pin["name"]], deepcopy(arguments)))
            # Disconnecting an HTTP client cannot cancel, duplicate or orphan the operation.
            reservation.task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        elif reservation.argument_digest != arg_digest:
            raise SoftRuntimeError("version_conflict", "operation arguments changed on replay")
        return deepcopy(await asyncio.shield(reservation.task))

    async def release(self, reservation_id):
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            return
        if reservation.task is not None and not reservation.task.done():
            raise SoftRuntimeError("plugin.draining", "operation still executing")
        for owner in reservation.closure.values():
            owner.references.discard(reservation_id)
        del self._reservations[reservation_id]

    async def emit(self, name, payload):
        self._audited()
        # Snapshot the subscribers. Re-check admission immediately before each delivery.
        deliveries = [(fiber, handler) for fiber in self._fibers.values()
                      if fiber.state == "ACTIVE" for event, handler in fiber.listeners if event == name]
        for fiber, handler in deliveries:
            closure = self._closure(fiber, allow_draining=False)
            settings = {key: deepcopy(owner.settings) for key, owner in closure.items()}
            await self._execute(Reservation({}, closure, settings), handler, deepcopy(payload))
