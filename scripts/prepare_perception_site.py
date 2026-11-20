"""Extend a verified live DSH site with software services and optional read-tool projections."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]
from prepare_soft_pilots import GIT, SHA, compute_content_digest, obj, save, sha
from site_pack import build_lock, verify
from v2_registry import fingerprint


SERVICES = {
    "perception.fused_scene": ("/fuse", "fusion", ["actor_ref"]),
    "perception.target_location": ("/ground", "grounding", ["actor_ref", "target"]),
}
TOOLS = {
    "perception.read_stationary": ("perceive_stationary", "perception.fused_scene", "获取主体的静止融合感知",
                                   "获取 {actor_ref} 的静止融合感知", ["actor_ref"]),
    "perception.mark_target": ("mark_target", "perception.target_location", "从现场 RGB-D 在二维和三维标注目标",
                                "在二维和三维标注{target}", ["target"]),
}


OBJECT_SERVICES = {"perception.object_location": ("/locate", "grounding", ["actor_ref", "target"])}
OBJECT_TOOLS = {"perception.see_object": ("see_object", "perception.object_location",
                 "从彩色图寻找指定物体并测量其可见表面到相机的距离",
                 ["请帮我找到{target}，量一下它离相机有多远", "帮我看看{target}在哪里"], ["target"])}


QUERY_SERVICES = {"perception.frame_statistics": ("/query", "analysis", ["actor_ref", "program"])}
QUERY_TOOLS = {"perception.query_frame": ("query_frame", "perception.frame_statistics",
               "Compute measurements on one live RGB-D image. Each call samples a NEW frame. Compare regions "
               "of the SAME frame in ONE call using grid(rows,cols). rows is the number of bands top to bottom; "
               "cols is the number left to right. Using grid alone partitions the entire image equally. "
               "program grammar: channel | optional crop(x0,y0,x1,y1) | optional grid(rows,cols) | statistic. "
               "Omit unused stages entirely. Channels: depth (optical depth in metres), brightness, red, green, blue. "
               "Statistics: mean, median, min, max, coverage (valid %), quantile(q). crop and q accept decimal "
               "literals between 0 and 1. No fractions or arithmetic. Output includes all region values, units, "
               "and indices for minima and maxima.", [], ["program"])}
GRID_FIELDS = {"channel":{"type":"string", "enum":["depth", "brightness", "red", "green", "blue"]},
               "row_count":{"type":"integer", "minimum":1, "maximum":8},
               "column_count":{"type":"integer", "minimum":1, "maximum":8},
               "statistic":{"type":"string", "enum":["median", "mean", "min", "max", "coverage"]}}
QUERY_TOOLS["perception.measure_grid"] = (
    "measure_grid", "perception.frame_statistics",
    "Measure equal rectangular regions of ONE live RGB-D image. Returns all region values and the closest/"
    "farthest indices for depth, lowest/highest indices for other channels. row_count divides top-to-bottom; "
    "column_count divides left-to-right. Use 1 for an undivided axis. depth is optical-axis metres, brightness "
    "and RGB use 0..255, coverage reports % valid pixels. Handles at most 32 regions per image.",
    [], list(GRID_FIELDS))


def prepare(original, previous_lock, output, source_commit, backend_digest, profile_file, actor, *,
            objects=False, queries=False, operator_policy=None):
    lock, old = verify(original, previous_lock)
    if not GIT.fullmatch(source_commit) or not SHA.fullmatch(backend_digest):
        raise ValueError("invalid source or backend identity")
    if json.loads(profile_file.read_bytes())["actor_ref"] != actor:
        raise ValueError("actor differs from site configuration")
    if lock["policy"]["effects"] != ["read"] or any(x["class"] != "soft" for x in old.values()):
        raise ValueError("expected an exclusively read-only software site")
    output.mkdir(parents=True, exist_ok=False)
    config_digest = sha(profile_file.read_bytes())
    host = old["sensing.published"]["provenance"]["image_digest"]
    settings = {key: json.loads((original / "config" / (key + ".json")).read_bytes()) for key in old}
    services = OBJECT_SERVICES if objects else SERVICES
    tools = OBJECT_TOOLS if objects else TOOLS
    stages = (("objects", ["perception-objects"]),) if objects else (
        ("ready", ["perception-static"]), ("candidate", ["perception-static", "perception-markup"]))
    peer = "object-perception" if objects else "stationary-perception"
    service_module = "perception.spatial_facts.object_service" if objects else "perception.spatial_facts.compute_service"
    if queries:
        if objects:
            raise ValueError("select a single release stage")
        services, tools = QUERY_SERVICES, QUERY_TOOLS
        stages = (("queries", ["perception-queries"]),)
        peer, service_module = "frame-query", "perception.spatial_facts.frame_query_service"
    if operator_policy is not None:
        if objects or queries or "perception.frame_statistics" not in old:
            raise ValueError("operator projection needs the installed single-frame statistics service")
        policy = json.loads(operator_policy.read_bytes())
        if policy.get("runner") != "analyze_regions" or not policy.get("contracts"):
            raise ValueError("missing numerical operator policy")
        services = {"computation.numeric": ("/evaluate", "derived", ["operator_digest", "values"])}
        tools = {"perception.analyze_regions": ("analyze_regions", "computation.numeric",
                 "Evaluate one installed numerical program on the measured regions of a single live image.",
                 [], ["operator_digest", *GRID_FIELDS])}
        stages = (("operators", ["perception-operators"]),)
        peer, service_module = "operator-evaluator", "agent.numeric_service"
        config_digest = sha(operator_policy.read_bytes())
    if set(old).intersection(services) or set(old).intersection(tools):
        raise ValueError("requested capability already exists in the original site")
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        base = Path(tmp) / "input"
        shutil.copytree(original, base)
        trust = base / "trust"
        index = json.loads((trust / "catalog.json").read_bytes())
        pending = []
        for pid in [*services, *tools]:
            version = "1.1.0" if queries else "1.0.0"
            if pid in services:
                path, result_key, args = services[pid]
                module = service_module
                name, kind = pid, "service"
                spec = {"service": name, "path": path, "result_key": result_key}
                deps = ["sensing.observations"]
                if pid == "computation.numeric":
                    deps = []
                fields = {"ok": {"type": "boolean"}}
                network = {"ingress": [], "egress": [peer]}
                settings[pid] = {"backend_digest": backend_digest, "profile_digest": config_digest}
                item = deepcopy(old["sensing.published"])
            else:
                name, service, description, utterance, args = tools[pid]
                kind, module, deps = "tool", "agent.read_service_tool", [service]
                spec = {"service": service, "legacy": {"id": name, "version": version, "description": description,
                        "effect": "read", "parameters": {key: "string" for key in args},
                        "utterances": [utterance] if isinstance(utterance, str) else utterance}}
                fields = {services[service][1]: {"type": "object"}, "observations": {"type": "object"}}
                network = {"ingress": [], "egress": []}
                settings[pid] = {} if "actor_ref" in args else {"actor_ref": actor}
                item = deepcopy(old["navigation.read_spatial_observations"])
            schema = name + "/call@1"
            params = obj({key: {"type": "string", "minLength": 1, "maxLength": 128} for key in args})
            if pid == "perception.measure_grid":
                module = "perception.spatial_facts.grid_tool"
                params = obj(GRID_FIELDS)
                spec["legacy"]["parameters"] = {k: v["type"] for k, v in GRID_FIELDS.items()}
                # Legacy v2 hosts return this immutable projection verbatim.
                spec["legacy"]["_parameter_schema"] = params
            if pid == "computation.numeric":
                params = obj({"operator_digest":{"type":"string", "minLength":71, "maxLength":71},
                              "values":{"type":"array", "minItems":1, "maxItems":32, "items":{"type":"number"}}})
            if pid == "perception.analyze_regions":
                deps = ["perception.frame_statistics", "computation.numeric"]
                module = "perception.spatial_facts.region_operator_tool"
                args_schema = {"operator_digest":{"type":"string", "minLength":71, "maxLength":71}, **GRID_FIELDS}
                params = obj(args_schema)
                spec["legacy"]["parameters"] = {k:v["type"] for k,v in args_schema.items()}
                spec["legacy"]["_parameter_schema"] = params
            filename = "assets/" + name + ".json"
            save(trust / filename, params)
            index["schemas"][schema] = filename
            source = (ROOT / (module.replace(".", "/") + ".py")).read_bytes()
            cx = sha(source)
            file = "assets/" + cx[7:] + ".py"
            (trust / file).write_bytes(source)
            entry = module + ":apply"
            index["implementations"].setdefault(cx, []).append({"id": pid, "exports": [name], "artifact": file,
                "kind": "code", "entrypoint": entry, "class": "soft", "source_commit": source_commit,
                "image_digest": host, "network": network})
            item.update(id=pid, version=version, entrypoint=entry, network=network,
                        provides=[{"type": kind, "name": name, "schema_ref": schema}],
                        requires=[{"type": "service", "name": dep, "schema_ref": dep + "/call@1"} for dep in deps],
                        parameters={"schema_ref": schema}, outcomes=obj(fields, closed=False),
                        config_schema=obj({"profile": {"type": "object", "const": spec},
                                           "settings": {"type": "object"}}))
            item["provenance"].update(source_commit=source_commit, code_digest=cx)
            pending.append(item)
        save(trust / "catalog.json", index)
        for item in pending:
            item["content_digest"] = compute_content_digest(item, trust)
            save(base / "releases" / (item["id"] + ".plugin.json"), item)
        hashes = {}
        for stage, packs in stages:
            profile = {"schema_version": 1, "id": "perception-" + stage, "policy": lock["policy"],
                       "packs": sorted([*lock["packs"], *packs]), "settings": dict(settings)}
            if stage == "ready":
                profile["settings"].pop("perception.mark_target")
            specfile = base / "resolve.json"
            save(specfile, profile)
            hashes[stage] = build_lock(ROOT / "capabilities", specfile, base, None, output / stage)
            resolved, _ = verify(output / stage, hashes[stage])
            if any(resolved["plugins"][key] != value for key, value in lock["plugins"].items()):
                raise ValueError("a pre-existing site binding would change")
            if any(fingerprint(json.loads((ROOT/"capabilities"/key/"pack.json").read_bytes())) != value
                   for key, value in lock["packs"].items()):
                raise ValueError("original pack definition changed")
        return hashes


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument("--objects", action="store_true", help="add RGB-D object location")
    mode.add_argument("--queries", action="store_true", help="add bounded RGB-D frame queries")
    mode.add_argument("--operator-policy", type=Path, help="add the pure operator evaluator and its RGB-D input projection")
    for key in ("original", "out", "profile"):
        cli.add_argument("--"+key, type=Path, required=True)
    for key in ("previous-lock", "source-commit", "backend-digest", "actor"):
        cli.add_argument("--"+key, required=True)
    args = cli.parse_args()
    print(json.dumps(prepare(args.original, args.previous_lock, args.out, args.source_commit,
                             args.backend_digest, args.profile, args.actor, objects=args.objects, queries=args.queries,
                             operator_policy=args.operator_policy)))
