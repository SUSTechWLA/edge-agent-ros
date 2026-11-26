"""Typed projection of the frame statistics service for equal rectangular regions."""
from navigation.tools import wire_times


def apply(ctx):
    config = ctx.config()
    service = ctx.require(config["profile"]["service"])

    async def invoke(wire):
        args = wire["arguments"]
        rows, cols = args["row_count"], args["column_count"]
        if rows * cols > 32:
            return {"operation_id": wire["operation_id"], "state": "BLOCKED",
                    "error": {"code": "query.too_many_regions", "message": "The budget is 32 regions per frame."}}
        program = f"{args['channel']} | grid({rows},{cols}) | {args['statistic']}"
        reply = await service({"actor_ref": config["settings"]["actor_ref"], "program": program})
        if not reply["ok"]:
            return {"operation_id": wire["operation_id"], "state": "BLOCKED",
                    "error": {"code": reply["code"], "message": reply["code"]}}
        return {"operation_id": wire["operation_id"], "state": "SUCCEEDED", "evidence": wire_times(reply["value"])}

    ctx.tools.register(config["profile"]["legacy"]["id"], invoke)
