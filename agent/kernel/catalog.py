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


def reference_utterance(pattern, parameters, effect):
    """Compile a read-only template with two uniquely delimited opaque reference IDs."""
    if (effect != "read" or not isinstance(pattern, str) or len(pattern) > 256
            or len(parameters) != 2 or any(kind != "string" for kind in parameters.values())):
        return None
    parts = re.split(r"\{([a-z][a-z0-9_]{0,58}_ref)\}", pattern)
    if len(parts) != 5 or set(parts[1::2]) != set(parameters):
        return None
    prefix, first, delimiter, second, suffix = parts
    # At least one non-ID character separates the slots, so a reference can
    # never absorb their delimiter. All template text is treated literally.
    if (any("{" in s or "}" in s for s in parts[::2])
            or not re.search(r"[^A-Za-z0-9_./:@-]", delimiter)):
        return None
    token = r"[A-Za-z0-9_][A-Za-z0-9_./:@-]{0,127}"
    return re.compile(re.escape(prefix) + f"(?P<{first}>{token})" + re.escape(delimiter)
                      + f"(?P<{second}>{token})" + re.escape(suffix))


def valid_utterance(pattern, parameters, effect=None):
    if len(parameters) == 2:
        return reference_utterance(pattern, parameters, effect) is not None
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
            or not isinstance(patterns, list) or not patterns or not isinstance(parameters, dict) or len(parameters) != 1
            or not all(valid_utterance(pattern, parameters) for pattern in patterns)):
        return False
    marker = "{" + next(iter(parameters)) + "}"
    return all(all(pattern.split(marker)) for pattern in patterns)


def is_series(cap):
    return not cap.get("endpoint") and cap.get("derived", {}).get("schema_version") == 2


def registry(*, legacy_only=False, duplicate_tools=None):
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
            valid = (re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", key) and compatible
                     and isinstance(item["version"],str) and item["version"] and isinstance(item["description"],str)
                     and url.scheme in ("http","https") and url.hostname and not url.username and not url.password
                     and item["effect"] in ("read","idempotent")
                     and isinstance(parameters,dict)
                     and isinstance(utterances,list) and all(valid_utterance(p,parameters,item["effect"]) for p in utterances)
                     and valid_verbatim_manifest(item)
                     and isinstance(outcomes,dict) and all(isinstance(k,str) and isinstance(v,str) and v
                                                         for k,v in outcomes.items())
                     and isinstance(terms,dict) and all(k in outcomes and isinstance(words,list)
                          and all(isinstance(w,str) and w for w in words) for k,words in terms.items())
                     and (key == "inspect" or all(v in ("string","number","boolean") for v in parameters.values())))
            if valid and key in loaded and duplicate_tools is not None:
                duplicate_tools.add(key)
            if valid and key not in loaded:
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


def _legacy_record(name, item, source):
    service = source == "v1_service"
    meta = normalize_legacy_service(item) if service else normalize_legacy_tool(item)
    return {"id":name, "version":meta["version"], "class":meta["class"], "source":source,
            "provider":item["id"], "kind":meta["kind"], "binding":meta["binding"],
            "effects":item.get("effect"), "parameters":item.get("parameters"),
            "outcomes":item.get("outcomes") if service else item.get("outcomes", {}),
            "health":meta["health"], "lifecycle":"draining" if item.get("draining") else meta["lifecycle"],
            "availability":item.get("status", "registered") if service else "registered"}


def _track_records(view, source, admitted):
    plugins = {row["id"]:row for row in view["plugins"]}
    tools = {cap["id"]:cap for cap in view["tools"]}
    services = view.get("services", {})
    for name, owner in view["claims"].items():
        plugin = plugins.get(owner, {})
        export = next((ref for ref in plugin.get("provides", []) if ref["name"] == name), {})
        kind = export.get("type", plugin.get("kind"))
        tool = tools.get(name, {})
        service = services.get(name, {})
        pin = tool.get("_soft") or tool.get("_physical") or service.get("pin", {})
        yield {"id":name, "provider":owner, "source":source, "kind":kind,
               "version":pin.get("version", plugin.get("version")), "binding":pin.get("binding"),
               "class":plugin.get("class", "soft" if source == "v2_soft" else "physical"),
               "effects":tool.get("effect"), "parameters":tool.get("parameters"),
               "outcomes":tool.get("outcomes", {}) if tool else None,
               "health":plugin.get("health", "unknown"), "lifecycle":plugin.get("lifecycle", "claimed"),
               "draining":plugin.get("draining", False),
               "availability":"available" if service or (tool and admitted.get(name) == tool) else "unavailable"}


def capability_records(legacy, directory, views, admitted, duplicate_tools):
    """Select by exported name. Discovery never grants invocation or probes a provider."""
    candidates = {}
    priority = {"derived":0, "builtin":1, "v1_tool":1, "v1_service":2, "v2_soft":3, "physical":3}

    def add(record):
        candidates.setdefault(record["id"], []).append(record)

    for name, cap in legacy.items():
        if cap.get("endpoint"):
            source = "v1_tool"
        elif "derived" in cap:
            source = "derived"
        else:
            source = "builtin"
        add(_legacy_record(name, cap, source))
    for row in directory["plugins"]:
        for name in row.get("provides", []):
            add(_legacy_record(name, row, "v1_service"))
    for source, view in zip(("v2_soft", "physical"), views):
        for record in _track_records(view, source, admitted):
            add(record)

    records, conflicts = [], []
    for name, rows in sorted(candidates.items()):
        rank = max(priority[row["source"]] for row in rows)
        winners = [row for row in rows if priority[row["source"]] == rank]
        if len(winners) > 1 or (winners[0]["source"] == "v1_tool" and name in duplicate_tools):
            conflicts.append({"id":name, "code":"catalog.conflict", "providers":[
                {"source":row["source"], "id":row["provider"]} for row in winners]})
        else:
            records.append(winners[0])
    return {"records":records, "conflicts":conflicts}


def capability_view(service_snapshot):
    duplicate_tools = set()
    legacy = registry(legacy_only=True, duplicate_tools=duplicate_tools)
    try:
        views = host.views()
        admitted = host.catalog(legacy, views)
    except host.SoftError as error:
        raise failure(Unavailable, str(error), error.code, error.origin) from error
    result = {"capabilities":catalog(admitted)}
    try:
        directory = service_snapshot(observing=True).public()
    except OSError:
        # Service discovery was never a dependency of the legacy tool listing.
        return {**result, "records":[], "conflicts":[], "records_error":{"code":"catalog.source_unavailable",
                "source":"v1_service"}}
    return {**result, **capability_records(legacy, directory, views, admitted, duplicate_tools)}


def plugin_view(directory):
    """Return the existing schema=2 public view, including unclaimed legacy records."""
    soft, physical = host.views()
    claims = {**soft["claims"], **physical["claims"]}
    legacy = [normalize_legacy_service(row) for row in directory["plugins"]
              if row["id"] not in claims.values()]
    legacy += [normalize_legacy_tool(cap) for cap in registry(legacy_only=True).values()
               if cap["id"] not in claims and cap["id"] not in ("inspect", "status")]
    return {"schema_version":2, "plugins":legacy + soft["plugins"] + physical["plugins"]}
