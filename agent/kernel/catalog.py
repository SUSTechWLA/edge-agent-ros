"""Read-only task capability registry and public plugin directory projections."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from edge_contracts.descriptor import binding, normalize_legacy_service, normalize_legacy_tool
from edge_contracts.errors import Unavailable, failure
from . import host

if __package__ == "kernel":
    from derived_tools import load_directory
else:
    from ..derived_tools import load_directory


BASE = {
    "inspect": {"id":"inspect", "description":"依次到明确给出的平面坐标停稳并记录位置", "version":"1", "parameters":{"points":"xy_list"}, "effect":"idempotent"},
    "status": {"id":"status", "description":"读取本机当前里程计位置、朝向和速度", "version":"1", "parameters":{}, "effect":"read"},
}


def valid_utterance(pattern, parameters):
    if len(parameters)!=1 or next(iter(parameters.values()))!="string" or not isinstance(pattern,str):
        return False
    marker="{"+next(iter(parameters))+"}"
    return (pattern.count(marker)==1 and pattern.count("{")==pattern.count("}")==1
            and len(marker)<len(pattern)<=256)


def valid_verbatim_manifest(cap):
    flag = cap.get("verbatim_argument", False)
    if flag is False:
        return True
    patterns = cap.get("utterances")
    parameters = cap.get("parameters")
    if (flag is not True or cap.get("effect") != "read" or cap.get("id") in BASE
            or not isinstance(patterns, list) or not patterns or not isinstance(parameters, dict)
            or not all(valid_utterance(pattern, parameters) for pattern in patterns)):
        return False
    marker = "{" + next(iter(parameters)) + "}"
    return all(all(pattern.split(marker)) for pattern in patterns)


def is_series(cap):
    return not cap.get("endpoint") and cap.get("derived", {}).get("schema_version") == 2


def registry(*, legacy_only=False):
    caps = dict(BASE)
    loaded = set()
    dirs=[Path(os.getenv("EDGE_PLUGIN_DIR", "/etc/edge/plugins"))]
    extra=os.getenv("EDGE_EXTRA_PLUGIN_DIR")
    if extra:
        dirs.append(Path(extra))
    for path in (p for directory in dirs for p in sorted(directory.glob("*.json"))):
        try:
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                raw = stream.read(131073)
            if len(raw) > 131072:
                raise ValueError("manifest exceeds 128 KiB")
            item = json.loads(raw.decode("utf-8-sig"))
            key = item["id"]
            parameters = item["parameters"]
            outcomes = item.get("outcomes",{})
            terms = item.get("outcome_terms",{})
            utterances=item.get("utterances",[])
            if not isinstance(item.get("endpoint"), str):
                print(f"Invalid capability manifest {path.name}: endpoint must be a string", flush=True)
                continue
            url = urlsplit(item["endpoint"])
            compatible = (key not in BASE or
                          (parameters == BASE[key]["parameters"] and item["effect"] == BASE[key]["effect"]))
            valid = (re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", key) and key not in loaded and compatible
                     and isinstance(item["version"],str) and item["version"] and isinstance(item["description"],str)
                     and url.scheme in ("http","https") and url.hostname and not url.username and not url.password
                     and item["effect"] in ("read","idempotent")
                     and isinstance(parameters,dict)
                     and isinstance(utterances,list) and all(valid_utterance(p,parameters) for p in utterances)
                     and valid_verbatim_manifest(item)
                     and isinstance(outcomes,dict) and all(isinstance(k,str) and isinstance(v,str) and v
                                                         for k,v in outcomes.items())
                     and isinstance(terms,dict) and all(k in outcomes and isinstance(words,list)
                          and all(isinstance(w,str) and w for w in words) for k,words in terms.items())
                     and (key == "inspect" or all(v in ("string","number","boolean") for v in parameters.values())))
            if valid:
                loaded.add(key)
                caps[key] = item
            else:
                print(f"Invalid capability manifest {path.name}", flush=True)
        except (ValueError, TypeError, KeyError, OSError, RecursionError) as exc:
            print(f"Invalid capability manifest {path.name}: {type(exc).__name__}", flush=True)
    # Preserve the existing precedence of deployed HTTP tools. Derived definitions
    # have their own directory and cannot shadow an existing provider.
    for key, cap in load_directory(dirs[0] / "derived").items():
        caps.setdefault(key, cap)
    if not legacy_only and host.enabled():
        try:
            return host.catalog(caps)
        except host.SoftError as error:
            raise failure(Unavailable, str(error), error.code, error.origin) from error
    return caps


def catalog(caps):
    entries = []
    for item in caps.values():
        entry = {k:v for k,v in item.items() if k in ("id","version","description","parameters","effect","outcomes")}
        if "derived" in item and not item.get("endpoint"):
            entry["derived"] = {k:v for k,v in item["derived"].items()
                                if k in ("field","unit","comparison","transforms","reduce")}
        entries.append(entry)
    return entries


def plugin_view(directory):
    """Return the existing schema=2 public view, including unclaimed legacy records."""
    soft, physical = host.views()
    claims = {**soft["claims"], **physical["claims"]}
    legacy = [normalize_legacy_service(row) for row in directory["plugins"]
              if row["id"] not in claims.values()]
    legacy += [normalize_legacy_tool(cap) for cap in registry(legacy_only=True).values()
               if cap["id"] not in claims and cap["id"] not in ("inspect", "status")]
    return {"schema_version":2, "plugins":legacy + soft["plugins"] + physical["plugins"]}
