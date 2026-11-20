"""Read an operator-published snapshot. No filesystem, ROS, device or clock access."""
from copy import deepcopy
import re

from edge_contracts.spatial_facts import SpatialFactError, check_batch, identifier, require


def replay_ns(value):
    # Site settings use interoperable JSON. Decimal strings preserve Unix
    # nanoseconds above 2^53; small integer fields in older recordings still work.
    if isinstance(value, str):
        require(re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", value), "sensing.invalid_timestamp")
        return int(value)
    return value


def capture(settings, actor_ref):
    """Return the actor's observations at the recorded instant, including opaque artifacts."""
    require(identifier(actor_ref), "sensing.invalid_actor")
    require(isinstance(settings, dict) and isinstance(settings.get("actors"), dict),
            "sensing.invalid_settings")
    if actor_ref not in settings["actors"]:
        raise SpatialFactError("sensing.actor_unavailable")
    snapshot = settings["actors"][actor_ref]
    require(isinstance(snapshot, dict) and set(snapshot) == {"clock_id", "as_of_ns", "samples"}
            and isinstance(snapshot["samples"], list), "sensing.invalid_snapshot")
    batch = {"schema_version": 1, "basis": "replay", "actor_ref": actor_ref, **deepcopy(snapshot)}
    batch["as_of_ns"] = replay_ns(batch["as_of_ns"])
    for sample in batch["samples"]:
        require(isinstance(sample, dict), "sensing.invalid_sample")
        sample.setdefault("actor_ref", actor_ref)
        sample["stamp_ns"] = replay_ns(sample.get("stamp_ns"))
    check_batch(batch)
    return batch


def apply(ctx):
    def read(request):
        require(isinstance(request, dict) and set(request) == {"actor_ref"}, "sensing.invalid_request")
        return capture(ctx.config()["settings"], request["actor_ref"])

    ctx.provide("sensing.observations", read)
