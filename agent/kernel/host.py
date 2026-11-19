"""Agent-facing operations on registered services and the active runtime tracks."""
if __package__ == "kernel":
    import plugin_tracks
    import runtime_plugins
    import soft_client
else:
    from .. import plugin_tracks, runtime_plugins, soft_client

from agent.tracing import exchange
from edge_contracts import invoke as protocol
from edge_contracts.errors import OutcomeUnknown, Refused, failure
from edge_contracts.soft_transport import SoftError


def enabled():
    return soft_client.enabled() or plugin_tracks.physical_enabled()


def catalog(legacy):
    return plugin_tracks.catalog(legacy)


def views():
    return plugin_tracks.views()


def resolve_service(name, expected_binding=None):
    return runtime_plugins.snapshot().pin(name, expected_binding=expected_binding)


def call_for(pin, action, **fields):
    return plugin_tracks.call_for(pin, action, **fields)


def freeze(task_id, candidate):
    return plugin_tracks.freeze(task_id, candidate)


def invoke(step, op, cap, trace, context, http_json):
    """Call the bound provider and return its candidate to the task owner."""
    body = protocol.request(op, step, context)

    def call():
        pin = step.get("physical_pin") or step.get("soft_pin")
        if pin:
            return call_for(pin, "call", wire=body)
        return http_json(cap["endpoint"], body)

    try:
        reply = exchange(trace, "adapter", step["tool"], body, call)
    except Refused as exc:
        if cap["effect"] != "read":
            raise failure(OutcomeUnknown, str(exc), getattr(exc, "code", "tool.receipt_invalid"),
                          getattr(exc, "origin", "provider"), getattr(exc, "details", None)) from exc
        raise
    return protocol.candidate(reply, op, step, cap)
