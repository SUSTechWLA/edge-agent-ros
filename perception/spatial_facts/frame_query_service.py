"""Project a read-only statistics backend onto the frozen DSH observation service."""
import asyncio
from http.client import HTTPConnection, HTTPException
import json

from edge_contracts.spatial_facts import SpatialFactError, require


def query(request, batch, settings):
    peer = HTTPConnection("frame-query", 8142, timeout=4)
    try:
        peer.request("POST", "/query", json.dumps({**request, "observations": batch}, allow_nan=False).encode(),
                     {"Content-Type": "application/json"})
        response = peer.getresponse()
        raw = response.read(65537)
        require(len(raw) <= 65536, "perception.receipt_too_large")
        record = json.loads(raw)
        if response.status != 200:
            raise SpatialFactError(record.get("error", {}).get("code", "query.unavailable"))
        require(all(record[key] == settings[key] for key in ("backend_digest", "profile_digest")),
                "perception.deployment_mismatch")
        result = record["result"]
        require(result["program"] == request["program"] and result["actor_ref"] == batch["actor_ref"] == request["actor_ref"],
                "query.binding_mismatch")
        sources = {s["source_id"]: s for s in batch["samples"] if s["kind"] == "artifact"}
        for kind in ("rgb", "depth", "calibration"):
            sample = sources.get(result[kind + "_source_id"])
            require(sample and sample["artifact_ref"] == result[kind + "_ref"]
                    and str(sample["stamp_ns"]) == result[kind + "_stamp_ns"], "query.binding_mismatch")
        return {"analysis": result, "observations": batch, "backend_digest": record["backend_digest"],
                "profile_digest": record["profile_digest"]}
    except (OSError, HTTPException, KeyError, TypeError, UnicodeError, ValueError) as err:
        if isinstance(err, SpatialFactError):
            raise
        raise SpatialFactError("query.unavailable") from err
    finally:
        peer.close()


def apply(ctx):
    config = ctx.config()
    observations = ctx.require("sensing.observations")

    async def invoke(request):
        try:
            for attempt in range(6):
                batch = await observations({"actor_ref": request["actor_ref"]})
                try:
                    value = await asyncio.to_thread(query, request, batch, config["settings"])
                    return {"ok": True, "value": value}
                except SpatialFactError as err:
                    if err.code != "grounding.unsynchronized_input" or attempt == 5:
                        raise
                    await asyncio.sleep(.1)
        except SpatialFactError as err:
            return {"ok": False, "code": err.code}

    ctx.provide(config["profile"]["service"], invoke)
