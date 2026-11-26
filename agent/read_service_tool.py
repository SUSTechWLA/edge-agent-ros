"""A first-party, declarative Tool projection for a frozen read-only DSH Service."""
from navigation.tools import wire_times


def apply(ctx):
    config = ctx.config()
    profile = config["profile"]
    service = ctx.require(profile["service"])

    async def invoke(wire):
        request = {**config["settings"], **wire["arguments"]}
        reply = await service(request)
        if not reply["ok"]:
            return {"operation_id": wire["operation_id"], "state": "BLOCKED",
                    "error": {"code": reply["code"], "message": reply["code"]}}
        return {"operation_id": wire["operation_id"], "state": "SUCCEEDED",
                "evidence": wire_times(reply["value"])}
    ctx.tools.register(profile["legacy"]["id"], invoke)
