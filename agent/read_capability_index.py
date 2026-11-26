"""Project an ordered site contract onto the *current* authorized DSH tool catalog."""
import hashlib
import json
import os
from pathlib import Path

from edge_contracts.descriptor import NAME, SHA
from edge_contracts.errors import Refused, failure
from agent.kernel.read_authority import unique_fields


def _require(condition):
    if not condition:
        raise ValueError("invalid capability index")


def _name(value):
    return isinstance(value, str) and bool(NAME.fullmatch(value))


def catalog(caps):
    path = os.environ.get("EDGE_READ_CAPABILITY_INDEX")
    if path is None:
        return []
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(131073)
        _require(len(raw) <= 131072)
        doc = json.loads(raw, object_pairs_hook=unique_fields)
        _require(isinstance(doc, dict) and set(doc) == {"schema_version", "contracts"}
                 and type(doc["schema_version"]) is int and doc["schema_version"] == 1)
        contracts = doc["contracts"]
        _require(isinstance(contracts, dict) and 1 <= len(contracts) <= 32)
        index_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        return [project(key, contract, caps, index_digest) for key, contract in contracts.items()]
    except (ValueError, TypeError, KeyError, OSError, RecursionError) as exc:
        raise failure(Refused, "站点只读能力索引无效", "planner.capability_index_invalid", "deployment") from exc


def project(key, contract, caps, index_digest):
    _require(_name(key) and key != "ordinary" and isinstance(contract, dict)
             and set(contract) == {"description", "implementations"})
    description = contract["description"]
    rows = contract["implementations"]
    _require(isinstance(description, str) and 0 < len(description.strip()) <= 2048
             and isinstance(rows, list) and 1 <= len(rows) <= 32)
    evaluated, installed = [], []
    seen = set()
    for row in rows:
        _require(isinstance(row, dict) and {"provider", "tool", "binding", "origin"} <= row.keys()
                 <= {"provider", "tool", "binding", "origin", "graph_language"}
                 and all(_name(row[k]) for k in ("provider", "tool", "origin"))
                 and isinstance(row["binding"], str) and SHA.fullmatch(row["binding"])
                 and row["tool"] not in seen)
        if "graph_language" in row:
            _require(isinstance(row["graph_language"], dict))
        seen.add(row["tool"])
        cap = caps.get(row["tool"])
        pin = (cap or {}).get("_soft", {})
        admitted = (pin.get("name") == row["tool"] and pin.get("plugin_id") == row["provider"]
                    and pin.get("binding") == row["binding"])
        evaluated.append({k:row[k] for k in ("tool", "provider", "origin", "binding")}
                         | {"eligible": admitted})
        if admitted:
            installed.append(row)
    return {"id":key, "description":description, "installed_tools":[r["tool"] for r in installed[:1]],
            "preferred":installed[0] if installed else None, "candidates":evaluated, "index_digest":index_digest}


def task_input_schema(schema, implementation):
    if not implementation or "graph_language" not in implementation:
        return schema
    if ("graph" not in schema.get("properties", {}) or "graph" not in schema.get("required", [])
            or schema["properties"]["graph"].get("type") != "object"):
        raise failure(Refused, "组合工具未声明图输入契约", "planner.capability_index_invalid", "deployment")
    return {**schema, "properties":{k:v for k,v in schema["properties"].items() if k != "graph"},
            "required":[k for k in schema["required"] if k != "graph"]}
