"""First-party tool adapters. Services own observations, localization policy and maps."""
from edge_contracts.spatial_facts import SpatialFactError
from navigation.planner import NavigationError, resolve_destination


def wire_times(value):
    """JSON numbers cannot represent a Unix nanosecond timestamp exactly in every consumer."""
    if isinstance(value, list):
        return [wire_times(item) for item in value]
    if isinstance(value, dict):
        return {key: str(item) if key.endswith("_ns") and type(item) is int else wire_times(item)
                for key, item in value.items()}
    return value


def apply(ctx):
    profile = ctx.config()["profile"]
    name = profile["legacy"]["id"]
    observations = ctx.require("sensing.observations") if name != "nav_resolve" else None
    position = ctx.require("localization.position") if name in ("nav_locate", "nav_plan") else None
    maps = ctx.require("navigation.catalog") if name == "nav_resolve" else None
    planner = ctx.require("navigation.route_planner") if name == "nav_plan" else None

    async def invoke(wire):
        args = wire["arguments"]
        actor = args["actor_ref"]
        op = wire["operation_id"]
        try:
            if name == "nav_resolve":
                evidence = resolve_destination(await maps({}), actor, args["destination_ref"])
            else:
                batch = await observations({"actor_ref": actor})
                if name == "read_spatial_observations":
                    evidence = batch
                else:
                    pose = await position(batch)
                    if name == "nav_locate":
                        evidence = pose
                    else:
                        route = await planner({**args, "localization": pose})
                        evidence = {"route": route, "localization": pose}
        except (SpatialFactError, NavigationError) as error:
            return {"operation_id": op, "state": "BLOCKED", "error": {"code": error.code, "message": str(error)}}
        return {"operation_id": op, "state": "SUCCEEDED", "evidence": wire_times(evidence)}

    ctx.tools.register(name, invoke)
