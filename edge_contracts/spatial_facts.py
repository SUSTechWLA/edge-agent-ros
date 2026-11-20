"""Published spatial observations v1. Coordinates use metres and diagonal variances use m²."""
import math
import re

from edge_contracts.descriptor import SHA


MAX_SOURCES = 32
MAX_STAMP_NS = (1 << 63) - 1
IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./:@-]{0,127}")
MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}")


class SpatialFactError(ValueError):
    """Stable reason for an invalid or unavailable published observation."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise SpatialFactError(code)


def identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def digest_ref(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def ns(value):
    return type(value) is int and 0 <= value <= MAX_STAMP_NS


def finite_vector(value, *, variance=False):
    return (isinstance(value, list) and len(value) in (2, 3)
            and all(type(n) in (float, int)
                    and (1e-12 <= n <= 1e12 if variance else -1e8 <= n <= 1e8)
                    and math.isfinite(n) for n in value))


def check_sample(sample, actor_ref):
    require(isinstance(sample, dict) and sample.get("actor_ref") == actor_ref,
            "sensing.actor_mismatch")
    require(all(identifier(sample.get(k)) for k in
                ("source_id", "calibration_ref", "clock_id", "frame_id"))
            and ns(sample.get("stamp_ns")), "sensing.invalid_provenance")
    common = {"actor_ref", "source_id", "calibration_ref", "clock_id", "frame_id", "stamp_ns", "kind"}
    if sample.get("kind") == "position":
        require(set(sample) == common | {"map_ref", "localization_epoch", "position_m", "variance_m2"}
                and digest_ref(sample["map_ref"]) and identifier(sample["localization_epoch"])
                and finite_vector(sample["position_m"])
                and finite_vector(sample["variance_m2"], variance=True)
                and len(sample["position_m"]) == len(sample["variance_m2"]), "sensing.invalid_position")
    elif sample.get("kind") == "artifact":
        require(set(sample) == common | {"artifact_ref", "media_type"} and digest_ref(sample["artifact_ref"])
                and isinstance(sample["media_type"], str) and MEDIA_TYPE.fullmatch(sample["media_type"]),
                "sensing.invalid_artifact")
    else:
        raise SpatialFactError("sensing.unsupported_sample_kind")


def check_batch(batch):
    require(isinstance(batch, dict) and type(batch.get("schema_version")) is int
            and batch["schema_version"] == 1 and identifier(batch.get("actor_ref"))
            and identifier(batch.get("clock_id")) and ns(batch.get("as_of_ns"))
            and batch.get("basis") in ("replay", "published") and isinstance(batch.get("samples"), list)
            and 0 < len(batch["samples"]) <= MAX_SOURCES, "sensing.invalid_batch")
    seen = set()
    for sample in batch["samples"]:
        check_sample(sample, batch["actor_ref"])
        key = (sample["kind"], sample["source_id"])
        require(key not in seen, "sensing.duplicate_source")
        seen.add(key)
