"""SDD-045 soft lifecycle tests. Each case gets an independent host and event loop."""
import asyncio
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from soft_runtime import SoftRuntime, SoftRuntimeError


def ref(name, kind="service"):
    return {"name": name, "type": kind, "schema_ref": "contract/" + name + "@1"}


def descriptor(name="math", version="1.0.0", *, provides=None, requires=()):
    """Admitted subset consumed by the runtime. Wire validation has separate tests."""
    return {"id": name, "version": version, "kind": "tool", "class": "soft", "effects": "read",
            "content_digest": "sha256:" + hashlib.sha256((name + version).encode()).hexdigest(),
            "hot_reload": True, "resources": {"shutdown_ms": 2000},
            "config_schema": {"type": "object"},
            "provides": [ref("calculate", "tool")] if provides is None else list(provides),
            "requires": list(requires)}


def calculate(ctx):
    ctx.tools.register("calculate", lambda args: args["number"] * ctx.config().get("scale", 1))


class SoftRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.runtime = SoftRuntime(on_event=self.events.append)
        self.old = descriptor()

    async def reject(self, code, operation):
        with self.assertRaises(SoftRuntimeError) as caught:
            await operation
        self.assertEqual(caught.exception.code, code)

    def row(self, name="math"):
        return next(item for item in self.runtime.catalog() if item["id"] == name)

    async def test_frozen_undispatched_plan_drains_before_explicit_dispose_and_apply(self):
        order = []

        def old(ctx):
            order.append("load-old")
            ctx.effect(lambda: order.append("dispose-old"))
            calculate(ctx)

        def new(ctx):
            order.append("load-new")
            ctx.tools.register("calculate", lambda args: args["numeric"] * 10)

        await self.runtime.install(self.old, old)
        preview = self.runtime.preview("math", "calculate")
        self.assertEqual(self.row()["reservations"], 0)
        pin = await self.runtime.freeze("math", "calculate", "task-1/step-1")
        self.assertEqual(pin, dict(preview, reservation_id="task-1/step-1"))
        v2 = descriptor(version="2.0.0")
        await self.reject("plugin.draining", self.runtime.update(v2, new))
        self.assertEqual(order, ["load-old"])
        await self.reject("plugin.draining", self.runtime.freeze("math", "calculate", "task-2/step-1"))
        self.assertEqual(await self.runtime.call(pin, {"number": 12}), 12)
        self.assertEqual(self.row()["reservations"], 1)  # candidate may still require verification
        await self.runtime.release(pin["reservation_id"])
        await self.runtime.update(v2, new)
        self.assertEqual(order, ["load-old", "dispose-old", "load-new"])
        await self.reject("tool.binding_unavailable", self.runtime.call(pin, {"number": 12}))
        future = await self.runtime.freeze("math", "calculate", "task-2/step-1")
        self.assertEqual(await self.runtime.call(future, {"numeric": 12}), 120)
        self.assertNotEqual(pin["binding"], future["binding"])
        states = [e["to"] for e in self.events if e["type"] == "plugin.lifecycle"]
        self.assertEqual(states, ["PENDING", "LOADING", "ACTIVE", "UNLOADING", "DISPOSED",
                                  "PENDING", "LOADING", "ACTIVE"])
        for event in self.events:
            self.assertIn(event["content_digest"], [self.old["content_digest"], v2["content_digest"]])
            self.assertIn(event["version"], ["1.0.0", "2.0.0"])
            self.assertTrue(event["generation"])

    async def test_dependency_loss_keeps_consumer_and_fails_closed_transitively(self):
        starts = []
        source = descriptor("sensor.facts", provides=[ref("numbers")])
        adapter = descriptor("adapter", provides=[ref("converted")], requires=[ref("numbers")])
        app = descriptor(requires=[ref("converted")])

        def consumer(ctx):
            starts.append("math")
            ctx.tools.register("calculate", ctx.require("converted"))

        await self.runtime.install(app, consumer)
        await self.runtime.install(adapter, lambda ctx: ctx.provide("converted", ctx.require("numbers")))
        self.assertEqual(self.row()["lifecycle"], "PENDING")
        await self.runtime.install(source, lambda ctx: ctx.provide("numbers", lambda args: 5))
        gen = self.row()["generation"]
        pin = await self.runtime.freeze("math", "calculate", "task/0")
        self.assertEqual(self.row("sensor.facts")["reservations"], 1)
        await self.runtime.set_health("sensor.facts", False)
        self.assertEqual(self.row()["lifecycle"], "ACTIVE")
        self.assertEqual(self.row()["availability"], "unavailable")
        await self.reject("plugin.dependency_unavailable", self.runtime.freeze("math", "calculate", "new"))
        await self.reject("plugin.dependency_unavailable", self.runtime.call(pin, {}))
        await self.runtime.set_health("sensor.facts", True)
        self.assertEqual(await self.runtime.call(pin, {}), 5)
        self.assertEqual(starts, ["math"])
        self.assertEqual(self.row()["generation"], gen)
        await self.reject("plugin.draining", self.runtime.remove("sensor.facts"))
        await self.runtime.release("task/0")
        await self.runtime.remove("sensor.facts")
        self.assertEqual(self.row()["availability"], "unavailable")
        # A mismatched typed contract cannot reactivate the consumer.
        source["provides"][0]["schema_ref"] = "other/value@1"
        source["version"], source["content_digest"] = "1.1.0", descriptor(version="1.1.0")["content_digest"]
        await self.runtime.install(source, lambda ctx: ctx.provide("numbers", lambda args: 6))
        self.assertEqual(self.row()["availability"], "unavailable")

    async def test_config_revision_is_atomic_and_frozen_per_plan(self):
        def plugin(ctx):
            ctx.config({"type": "object", "required": ["scale"], "properties": {
                "scale": {"type": "number", "minimum": 1}}})
            calculate(ctx)

        first = await self.runtime.install(self.old, plugin, {"scale": 2})
        old = await self.runtime.freeze("math", "calculate", "a")
        await self.reject("plugin.config_invalid", self.runtime.configure("math", {"scale": False}))
        latest = await self.runtime.configure("math", {"scale": 3})
        new = await self.runtime.freeze("math", "calculate", "b")
        self.assertEqual(first["generation"], latest["generation"])
        self.assertEqual(await self.runtime.call(old, {"number": 3}), 6)
        self.assertEqual(await self.runtime.call(new, {"number": 3}), 9)
        self.assertNotEqual(old["binding"], new["binding"])

    async def test_restart_restores_only_exact_transitive_identity_and_config(self):
        await self.runtime.install(self.old, calculate, {"scale": 2})
        pin = await self.runtime.freeze("math", "calculate", "persisted")
        restarted = SoftRuntime(on_event=self.events.append)
        await restarted.install(self.old, calculate, {"scale": 3})
        await self.reject("tool.binding_unavailable", restarted.freeze("math", "calculate", "persisted",
                                                                       expected_pin=pin, restore=True))
        await restarted.configure("math", {"scale": 2})
        await restarted.drain("math")
        await self.reject("plugin.draining", restarted.freeze("math", "calculate", "persisted", expected_pin=pin))
        self.assertEqual(await restarted.freeze("math", "calculate", "persisted", expected_pin=pin, restore=True), pin)
        self.assertEqual(await restarted.call(pin, {"number": 4}), 8)
        self.assertNotEqual(restarted.catalog()[0]["generation"], self.row()["generation"])
        # Altering a durable binding or the requested owner fails before dispatch.
        await self.reject("version_conflict", restarted.freeze("math", "other", "persisted"))
        await self.reject("tool.binding_unavailable", restarted.call(dict(pin, binding="forged"), {}))

    async def test_duplicate_call_or_lost_client_executes_once_and_holds_inflight(self):
        start, finish = asyncio.Event(), asyncio.Event()
        calls = []

        async def operation(args):
            calls.append(args)
            start.set()
            await finish.wait()
            return {"ok": args}

        await self.runtime.install(self.old, lambda ctx: ctx.tools.register("calculate", operation))
        pin = await self.runtime.freeze("math", "calculate", "op-try-1")
        first = asyncio.create_task(self.runtime.call(pin, {"number": 1}))
        await start.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        replay = asyncio.create_task(self.runtime.call(pin, {"number": 1}))
        await self.reject("version_conflict", self.runtime.call(pin, {"number": 7}))
        await self.reject("plugin.draining", self.runtime.release("op-try-1"))
        await self.reject("plugin.draining", self.runtime.remove("math"))
        self.assertEqual(self.row()["inflight"], 1)
        finish.set()
        self.assertEqual(await replay, {"ok": {"number": 1}})
        self.assertEqual(await self.runtime.call(pin, {"number": 1}), {"ok": {"number": 1}})
        self.assertEqual(calls, [{"number": 1}])

    async def test_partial_apply_rolls_back_and_preserves_failure_for_reconciliation(self):
        calls = []

        def broken(ctx):
            ctx.effect(lambda: calls.append("released"))
            calculate(ctx)
            raise RuntimeError("setup interrupted")

        await self.reject("plugin.apply_failed", self.runtime.install(self.old, broken))
        self.assertEqual(calls, ["released"])
        self.assertEqual(self.row()["lifecycle"], "FAILED")
        await self.reject("plugin.dependency_unavailable", self.runtime.freeze("math", "calculate", "op"))
        await self.runtime.remove("math")
        self.assertEqual(calls, ["released"])

    async def test_concurrent_dispose_serializes_reverse_async_cleanup(self):
        order = []
        started, release = asyncio.Event(), asyncio.Event()

        async def slow():
            order.append("C.start")
            started.set()
            await release.wait()
            order.append("C.done")

        def plugin(ctx):
            calculate(ctx)
            ctx.effect(lambda: order.append("A"))
            ctx.effect(lambda: order.append("B"))
            ctx.effect(slow)

        await self.runtime.install(self.old, plugin)
        first = asyncio.create_task(self.runtime.remove("math"))
        await started.wait()
        second = asyncio.create_task(self.runtime.remove("math"))
        await asyncio.sleep(0)
        self.assertEqual(order, ["C.start"])
        self.assertEqual(self.row()["lifecycle"], "UNLOADING")
        release.set()
        self.assertEqual(await first, await second)
        self.assertEqual(order, ["C.start", "C.done", "B", "A"])

    async def test_cleanup_exception_quarantines_old_instance_and_blocks_new_code(self):
        calls = []

        def fail():
            calls.append("failed")
            raise ValueError("handle not released")

        def plugin(ctx):
            calculate(ctx)
            ctx.effect(lambda: calls.append("independent"))
            ctx.effect(fail)

        await self.runtime.install(self.old, plugin)
        await self.reject("plugin.cleanup_incomplete", self.runtime.update(descriptor(version="2.0.0"), calculate))
        self.assertEqual(self.row()["lifecycle"], "FAILED")
        self.assertTrue(self.row()["cleanup_failed"])
        self.assertEqual(self.row()["version"], "1.0.0")
        await self.reject("plugin.cleanup_incomplete", self.runtime.remove("math"))
        self.assertEqual(calls, ["failed", "independent"])
        self.assertNotIn("DISPOSED", [event.get("to") for event in self.events])

    async def test_hung_cleanup_has_one_deadline_and_never_claims_release(self):
        def plugin(ctx):
            calculate(ctx)
            ctx.effect(asyncio.Event().wait)

        self.old["resources"]["shutdown_ms"] = 15
        await self.runtime.install(self.old, plugin)
        await self.reject("plugin.cleanup_incomplete", asyncio.wait_for(self.runtime.remove("math"), 1))
        self.assertTrue(self.row()["draining"])
        self.assertEqual(self.row()["last_error"], "plugin.cleanup_incomplete")

    async def test_listener_owned_by_fiber_is_drained_and_removed(self):
        called = []
        entered, finished = asyncio.Event(), asyncio.Event()
        listener = descriptor("listener", provides=[], requires=[ref("task.seen", "event")])

        async def listen(payload):
            entered.set()
            await finished.wait()
            called.append(payload)

        await self.runtime.install(listener, lambda ctx: ctx.on("task.seen", listen))
        delivery = asyncio.create_task(self.runtime.emit("task.seen", 1))
        await entered.wait()
        await self.reject("plugin.draining", self.runtime.remove("listener"))
        await self.reject("plugin.draining", self.runtime.emit("task.seen", 2))
        finished.set()
        await delivery
        await self.runtime.remove("listener")
        await self.runtime.emit("task.seen", 3)
        self.assertEqual(called, [1])

    async def test_permission_conflict_and_corrupt_candidate_preserve_live_version(self):
        await self.runtime.install(self.old, calculate)
        for changed in ({"class": "physical"}, {"effects": "actuation"}):
            await self.reject("plugin.class_violation", self.runtime.update(dict(self.old, **changed), calculate))
        await self.reject("plugin.permission_denied", self.runtime.update(self.old, "evil.module:apply"))
        await self.reject("plugin.service_conflict", self.runtime.install(descriptor("other"), calculate))
        await self.reject("version_conflict", self.runtime.update(dict(self.old, content_digest="other"), calculate))
        self.assertTrue(self.runtime.preview("math", "calculate"))
        self.assertEqual(self.row()["lifecycle"], "ACTIVE")
        self.assertFalse(self.row()["draining"])

    async def test_sink_failure_closes_dispatch_and_publish(self):
        def journal(event):
            if event["type"] == "plugin.drain":
                raise IOError("storage full")

        host = SoftRuntime(on_event=journal)
        await host.install(self.old, calculate)
        pin = await host.freeze("math", "calculate", "old")
        await self.reject("plugin.audit_unavailable", host.drain("math"))
        await self.reject("plugin.audit_unavailable", host.call(pin, {"number": 10}))
        await self.reject("plugin.audit_unavailable", host.freeze("math", "calculate", "new"))


if __name__ == "__main__":
    unittest.main()
