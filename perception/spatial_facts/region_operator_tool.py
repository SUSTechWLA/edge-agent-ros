"""Compose a single measured image with an independently qualified pure software operator."""
from navigation.tools import wire_times


def apply(ctx):
    config = ctx.config()
    statistics = ctx.require("perception.frame_statistics")
    evaluator = ctx.require("computation.numeric")

    async def invoke(wire):
        args = wire["arguments"]
        def blocked(code):
            return {"operation_id": wire["operation_id"], "state": "BLOCKED", "error": {"code": code, "message": code}}
        rows, cols = args["row_count"], args["column_count"]
        if rows * cols > 32:
            return blocked("query.too_many_regions")
        program = f"{args['channel']} | grid({rows},{cols}) | {args['statistic']}"
        result = await statistics({"actor_ref": config["settings"]["actor_ref"], "program": program})
        if not result["ok"]:
            return blocked(result["code"])
        facts = result["value"]
        analysis = facts["analysis"]
        values = [r["value"] for r in analysis["regions"]]
        if any(v is None for v in values) or len(values) != rows * cols:
            return blocked("operator.incomplete_measurements")
        computed = await evaluator({"operator_digest":args["operator_digest"], "values":values})
        if not computed["ok"]:
            return blocked(computed["code"])
        r = computed["value"]
        return {"operation_id":wire["operation_id"], "state":"SUCCEEDED",
                "evidence":wire_times({**facts, "derived":{**r["derived"], "unit":analysis["unit"]},
                                       "operator_runtime_digest":r["runtime_digest"], "operator_policy_digest":r["policy_digest"]})}

    ctx.tools.register(config["profile"]["legacy"]["id"], invoke)
