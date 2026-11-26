"""DSH service consuming a pinned observation snapshot and a site-bound software backend."""
import asyncio
from http.client import HTTPConnection, HTTPException
import json

from edge_contracts.spatial_facts import SpatialFactError, require


def exchange(profile, settings, request, batch):
    connection = HTTPConnection("stationary-perception", 8140, timeout=4)
    payload = {"actor_ref": request["actor_ref"], "observations": batch}
    if "target" in request:
        payload["target"] = request["target"]
    try:
        connection.request("POST", profile["path"], json.dumps(payload), {"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(384 * 1024 + 1)
        require(len(raw) <= 384 * 1024, "perception.receipt_too_large")
        result = json.loads(raw)
        if response.status != 200:
            raise SpatialFactError(result.get("error", {}).get("code", "perception.provider_unavailable"))
        require(all(result[key] == settings[key] for key in ("backend_digest", "profile_digest")),
                "perception.deployment_mismatch")
        require(result["result"]["actor_ref"] == request["actor_ref"], "sensing.actor_mismatch")
        return {profile["result_key"]: result["result"], "observations": batch,
                "backend_digest": result["backend_digest"], "profile_digest": result["profile_digest"]}
    except (OSError, HTTPException, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpatialFactError("perception.provider_unavailable") from exc
    finally:
        connection.close()


def apply(ctx):
    config = ctx.config()
    profile, settings = config["profile"], config["settings"]
    observations = ctx.require("sensing.observations")

    async def invoke(request):
        try:
            for attempt in range(6):
                batch = await observations({"actor_ref": request["actor_ref"]})
                try:
                    result = await asyncio.to_thread(exchange, profile, settings, request, batch)
                    return {"ok": True, "value": result}
                except SpatialFactError as exc:
                    if exc.code != "grounding.unsynchronized_input" or attempt == 5:
                        raise
                    await asyncio.sleep(.10)
        except SpatialFactError as exc:
            return {"ok": False, "code": exc.code}

    ctx.provide(profile["service"], invoke)
