"""Replaceable route computation over the frozen site catalog and a caller's localization."""
import asyncio

from navigation.planner import plan_route


def apply(ctx):
    catalog = ctx.require("navigation.catalog")

    async def plan(request):
        maps = await catalog({})
        return await asyncio.to_thread(plan_route, maps, request["actor_ref"], request["destination_ref"],
                                       request["localization"])

    ctx.provide("navigation.route_planner", plan)
