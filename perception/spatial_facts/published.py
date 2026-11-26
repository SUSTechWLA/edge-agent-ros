"""Consume the site's read-only facts gateway, reachable on the isolated software network."""
from http.client import HTTPConnection, HTTPException
import json

from edge_contracts.spatial_facts import SpatialFactError, check_batch, identifier, require


def apply(ctx):
    def read(request):
        require(isinstance(request, dict) and set(request) == {"actor_ref"} and identifier(request["actor_ref"]),
                "sensing.invalid_request")
        # The reviewed deployment permits only this peer and a fixed read endpoint.
        connection = HTTPConnection("spatial-facts", 8137, timeout=1)
        try:
            connection.request("POST", "/observations", body=json.dumps(request),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            payload = response.read(65537)
            require(response.status == 200 and len(payload) <= 65536, "sensing.source_unavailable")
            batch = json.loads(payload)
        except (OSError, HTTPException, json.JSONDecodeError, UnicodeError) as error:
            raise SpatialFactError("sensing.source_unavailable") from error
        finally:
            connection.close()
        check_batch(batch)
        require(batch["actor_ref"] == request["actor_ref"] and batch["basis"] == "published", "sensing.actor_mismatch")
        return batch

    async def invoke(request):
        import asyncio
        return await asyncio.to_thread(read, request)

    ctx.provide("sensing.observations", invoke)
