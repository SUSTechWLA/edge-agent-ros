"""DSH software service for verified finite numerical programs on an isolated evaluator."""
import asyncio
from http.client import HTTPConnection, HTTPException
import json


def evaluate(request, settings):
    peer = HTTPConnection("operator-evaluator", 8143, timeout=3.)
    try:
        peer.request("POST", "/evaluate", json.dumps(request, allow_nan=False).encode(), {"Content-Type":"application/json"})
        response = peer.getresponse()
        raw = response.read(65537)
        if len(raw) > 65536:
            return {"ok":False, "code":"operator.receipt_too_large"}
        record = json.loads(raw)
        if response.status != 200:
            return {"ok":False, "code":record.get("error", "operator.rejected")}
        data = record["derived"]
        if (record["runtime_digest"] != settings["backend_digest"] or record["policy_digest"] != settings["profile_digest"]
                or data["operator_digest"] != request["operator_digest"] or data["input_values"] != request["values"]):
            return {"ok":False, "code":"operator.binding_mismatch"}
        return {"ok":True, "value":record}
    except (OSError, HTTPException, KeyError, TypeError, ValueError):
        return {"ok":False, "code":"operator.unavailable"}
    finally:
        peer.close()


def apply(ctx):
    config = ctx.config()

    async def invoke(request):
        return await asyncio.to_thread(evaluate, request, config["settings"])

    ctx.provide(config["profile"]["service"], invoke)
