"""Inverse-variance fusion for independent Cartesian positions already in a common frame."""
from itertools import combinations
import math

from edge_contracts.spatial_facts import (
    MAX_SOURCES, MAX_STAMP_NS, check_batch, digest_ref, identifier, ns, require,
)


def localize(batch, policy):
    """Fuse all required sources for one actor on the batch clock at its as-of instant."""
    check_batch(batch)
    require(isinstance(policy, dict) and digest_ref(policy.get("map_ref"))
            and all(identifier(policy.get(k)) for k in ("localization_epoch", "frame_id", "clock_id"))
            and ns(policy.get("max_age_ns")) and ns(policy.get("max_skew_ns"))
            and isinstance(policy.get("sources"), dict) and 0 < len(policy["sources"]) <= MAX_SOURCES
            and all(identifier(k) and identifier(v) for k, v in policy["sources"].items()),
            "localization.invalid_policy")
    require(batch["clock_id"] == policy["clock_id"], "localization.clock_mismatch")
    by_source = {s["source_id"]: s for s in batch["samples"] if s["kind"] == "position"}
    require(policy["sources"].keys() <= by_source.keys(), "localization.source_missing")
    selected = [by_source[name] for name in sorted(policy["sources"])]
    for sample in selected:
        require(sample["calibration_ref"] == policy["sources"][sample["source_id"]],
                "localization.calibration_mismatch")
        for field in ("clock_id", "map_ref", "frame_id", "localization_epoch"):
            require(sample[field] == policy[field], "localization." + {
                "clock_id": "clock_mismatch", "map_ref": "map_mismatch",
                "frame_id": "frame_mismatch", "localization_epoch": "epoch_mismatch",
            }[field])
        age = batch["as_of_ns"] - sample["stamp_ns"]
        require(age >= 0, "localization.future_sample")
        require(age <= policy["max_age_ns"], "localization.stale")
    stamps = [s["stamp_ns"] for s in selected]
    oldest, newest = min(stamps), max(stamps)
    require(newest - oldest <= policy["max_skew_ns"], "localization.time_skew")
    axes = len(selected[0]["position_m"])
    require(all(len(s["position_m"]) == axes for s in selected), "localization.dimension_mismatch")
    # Reject inconsistent estimates before averaging. Each axis uses the sum of
    # the two independent variances and a five-standard-deviation threshold.
    for a, b in combinations(selected, 2):
        for i in range(axes):
            residual = a["position_m"][i] - b["position_m"][i]
            require(residual ** 2 <= 25 * (a["variance_m2"][i] + b["variance_m2"][i]),
                    "localization.inconsistent_sources")
    position, variance = [], []
    for i in range(axes):
        weights = [1.0 / s["variance_m2"][i] for s in selected]
        total = math.fsum(weights)
        position.append(math.fsum(w * s["position_m"][i] for w, s in zip(weights, selected)) / total)
        variance.append(1.0 / total)
    return {"schema_version": 1, "basis": batch["basis"], "actor_ref": batch["actor_ref"],
            "clock_id": policy["clock_id"], "as_of_ns": batch["as_of_ns"], "stamp_ns": oldest,
            "valid_until_ns": min(MAX_STAMP_NS, oldest + policy["max_age_ns"]),
            "map_ref": policy["map_ref"], "frame_id": policy["frame_id"],
            "localization_epoch": policy["localization_epoch"], "position_m": position,
            "variance_m2": variance, "algorithm": "localization.weighted@1.0.0",
            "sources": [{k: s[k] for k in ("source_id", "calibration_ref", "stamp_ns")} for s in selected]}


def apply(ctx):
    def read(batch):
        check_batch(batch)
        settings = ctx.config()["settings"]
        require(isinstance(settings, dict) and isinstance(settings.get("actors"), dict),
                "localization.invalid_policy")
        policy = settings["actors"].get(batch["actor_ref"])
        require(policy is not None, "localization.actor_unavailable")
        return localize(batch, policy)

    ctx.provide("localization.position", read)
