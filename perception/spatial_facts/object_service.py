"""A frozen DSH observation dependency and a single approved object-location peer."""
import asyncio
from http.client import HTTPConnection, HTTPException
import json

from edge_contracts.spatial_facts import SpatialFactError, require


def locate(request, batch, settings):
    connection = HTTPConnection("object-perception", 8141, timeout=8)
    try:
        body = json.dumps({**request, "observations": batch}, allow_nan=False).encode()
        connection.request("POST", "/locate", body, {"Content-Type":"application/json"})
        response = connection.getresponse()
        raw = response.read(384 * 1024 + 1)
        require(len(raw) <= 384 * 1024, "perception.receipt_too_large")
        record = json.loads(raw)
        if response.status != 200:
            raise SpatialFactError(record.get("error", {}).get("code", "perception.object_unavailable"))
        require(all(record[key] == settings[key] for key in ("backend_digest", "profile_digest")),
                "perception.deployment_mismatch")
        g = record["result"]
        require(g["query"] == request["target"] and g["actor_ref"] == batch["actor_ref"] == request["actor_ref"],
                "perception.object_binding_mismatch")
        sources = {s["source_id"]: s for s in batch["samples"] if s["kind"] == "artifact"}
        for kind in ("rgb", "depth", "calibration"):
            source = sources.get(g[kind + "_source_id"])
            require(source and source["artifact_ref"] == g[kind + "_ref"]
                    and str(source["stamp_ns"]) == str(g[kind + "_stamp_ns"]), "perception.object_binding_mismatch")
        return {"grounding": g, "observations": batch,
                "backend_digest": record["backend_digest"], "profile_digest": record["profile_digest"]}
    except (OSError, HTTPException, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise SpatialFactError("perception.object_unavailable") from error
    finally:
        connection.close()


def apply(ctx):
    config = ctx.config()
    observations = ctx.require("sensing.observations")

    async def invoke(request):
        try:
            for attempt in range(6):
                batch = await observations({"actor_ref": request["actor_ref"]})
                try:
                    return {"ok": True, "value": await asyncio.to_thread(locate, request, batch, config["settings"])}
                except SpatialFactError as error:
                    if error.code != "grounding.unsynchronized_input" or attempt == 5:
                        raise
                    await asyncio.sleep(.1)
        except SpatialFactError as error:
            return {"ok": False, "code": error.code}

    ctx.provide(config["profile"]["service"], invoke)
