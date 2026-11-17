"""Pure read-only tools derived from one odometry sample. Declarations contain data only."""
import hashlib
import json
import logging
import math
import operator
from pathlib import Path
import re
import unicodedata


_FIELDS = {
    # The status provider exposes signed forward linear.x as "speed".
    "speed": ("m/s", {"m/s": 1, "km/h": 3.6, "cm/s": 100}),
    "yaw": ("rad", {"rad": 1, "deg": 180 / math.pi}),
    "x": ("m", {"m": 1, "cm": 100, "mm": 1000}),
    "y": ("m", {"m": 1, "cm": 100, "mm": 1000}),
}
_COMPARISONS = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge}
UNITS_BY_FIELD = {field: tuple(units) for field, (_, units) in _FIELDS.items()}
COMPARISON_OPS = tuple(_COMPARISONS)
_REQUIRED = {"schema_version", "id", "description", "field", "unit"}
_ID = re.compile(r"derived\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
MAX_BYTES = 8192


def _number(value, name):
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite JSON number")
    try:
        numeric = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} is out of range") from error
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be finite")
    return numeric


def check_id(key):
    if not isinstance(key, str) or len(key) > 64 or not _ID.fullmatch(key):
        raise ValueError("id must be derived.<snake_name>, up to 64 characters")
    return key


def validate(definition):
    """Validate the complete v1 contract and return a new canonical definition."""
    if not isinstance(definition, dict) or set(definition) not in (_REQUIRED, _REQUIRED | {"comparison"}):
        raise ValueError("expected schema_version, id, description, field, unit and optional comparison")
    version = definition["schema_version"]
    if type(version) is not int or version != 1:
        raise ValueError("unsupported schema_version")
    key = check_id(definition["id"])
    description = definition["description"]
    if not isinstance(description, str):
        raise ValueError("description must be text")
    description = unicodedata.normalize("NFC", description.strip())
    if not 1 <= len(description) <= 256 or not description.isprintable():
        raise ValueError("description must contain 1 to 256 printable characters")
    field, unit = definition["field"], definition["unit"]
    if not isinstance(field, str) or field not in _FIELDS:
        raise ValueError("unknown odometry field")
    if not isinstance(unit, str) or unit not in _FIELDS[field][1]:
        raise ValueError(f"unsupported unit for {field}")

    result = {"schema_version": 1, "id": key, "description": description, "field": field, "unit": unit}
    if "comparison" in definition:
        compare = definition["comparison"]
        if not isinstance(compare, dict) or set(compare) != {"op", "threshold"}:
            raise ValueError("comparison must contain op and threshold")
        op = compare["op"]
        if not isinstance(op, str) or op not in _COMPARISONS:
            raise ValueError("comparison.op must be lt, le, gt or ge")
        result["comparison"] = {"op": op, "threshold": _number(compare["threshold"], "comparison.threshold")}
    return result


def capability(definition):
    """Pin the full declaration, including unit and predicate, in the tool version."""
    definition = validate(definition)
    data = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {"id": definition["id"], "version": hashlib.sha256(data).hexdigest(),
            "description": definition["description"], "parameters": {}, "effect": "read", "derived": definition}


def evaluate(definition, sample):
    """Apply a declaration to a caller-checked fresh sample in its original clock/frame."""
    definition = validate(definition)
    if not isinstance(sample, dict):
        raise ValueError("expected a measured odometry sample")
    for key in ("source", "frame"):
        if not isinstance(sample.get(key), str) or not sample[key].strip():
            raise ValueError(f"missing odometry {key}")
    stamp = sample.get("stamp_ns")
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("stamp_ns must be a positive integer")
    field, unit = definition["field"], definition["unit"]
    raw_unit, factors = _FIELDS[field]
    raw = _number(sample.get(field), field)
    value = _number(raw * factors[unit], "converted value")
    evidence = {"source": sample["source"], "frame": sample["frame"], "stamp_ns": stamp,
                "field": field, "raw_value": raw, "raw_unit": raw_unit, "value": value, "unit": unit}
    if "comparison" in definition:
        compare = definition["comparison"]
        evidence["comparison"] = compare
        evidence["matches"] = _COMPARISONS[compare["op"]](value, compare["threshold"])
    return evidence


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate declaration key")
        result[key] = value
    return result


def parse_definition(raw):
    """Decode the bounded UTF-8 wire format used by publishers and the loader."""
    if len(raw) > MAX_BYTES:
        raise ValueError("declaration exceeds size limit")
    try:
        definition = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys)
    except RecursionError as error:
        raise ValueError("declaration is too deeply nested") from error
    return validate(definition)


def load_directory(path):
    """Return an independent snapshot; a malformed or partly published file stays invisible."""
    tools = {}
    for file in sorted(Path(path).glob("derived.*.json")):
        try:
            if file.is_symlink() or not file.is_file():
                continue
            with file.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            tool = capability(parse_definition(raw))
            if file.name != tool["id"] + ".json":
                raise ValueError("filename must match the declared id")
            tools[tool["id"]] = tool
        except (OSError, ValueError) as error:
            logging.getLogger(__name__).warning("Ignoring derived tool %r (%s)", file.name, error)
    return tools
