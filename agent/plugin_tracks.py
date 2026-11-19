"""Route an already admitted task to its track. Only the deployment controller accepts device work."""
import os
if __package__:
    from . import soft_client
else:
    import soft_client


def physical_enabled():
    return bool(os.environ.get("EDGE_PHYSICAL_SOCKET"))


def physical_request(action, **fields):
    path = os.getenv("EDGE_PHYSICAL_SOCKET")
    if not path:
        raise soft_client.SoftError("tool.binding_unavailable", "original deployed physical controller unavailable")
    try:
        return soft_client.request_at(path, action, **fields)
    except soft_client.SoftError as exc:
        exc.origin = "physical.controller"
        raise


def call_for(pin, action, **fields):
    if pin.get("class") == "physical":
        return physical_request(action, pin=pin, **fields)
    return soft_client.request(action, pin=pin, **fields)


def views():
    soft = soft_client.request("catalog") if soft_client.enabled() else {"tools":[], "plugins":[], "claims":{}}
    physical = physical_request("catalog") if physical_enabled() else {"tools":[], "plugins":[], "claims":{}}
    return soft, physical


def catalog(legacy, track_views=None):
    soft, physical = views() if track_views is None else track_views
    tools = soft_client.catalog(legacy, soft)
    # Both owners must agree on admission. A duplicate name is withheld even
    # when one track is draining and no longer advertises a callable tool.
    for name in physical["claims"]:
        if name not in ("inspect", "status"):
            tools.pop(name, None)
    for cap in physical["tools"]:
        name = cap["id"]
        if name not in legacy and name not in soft["claims"] and name not in ("inspect","status"):
            tools[name] = cap
    return tools


def freeze(task, candidate):
    provider, steps = candidate
    if any("soft_pin" in step for step in steps):
        if not soft_client.enabled():
            raise soft_client.SoftError("tool.binding_unavailable","original soft runtime unavailable")
        provider, steps = soft_client.request("freeze_plan",task=task,provider=provider,steps=steps)
    if any("physical_pin" in step for step in steps):
        provider, steps = physical_request("freeze_plan",task=task,provider=provider,steps=steps)
    return provider, steps
