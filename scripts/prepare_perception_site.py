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
               "对当前相机画面按区域测量和比较数值。program 写成英文表达式：通道 | grid(行,列) | 统计量。"
               "通道可用 depth(光轴深度米)、brightness(亮度)、red、green、blue。"
               "统计量可用 mean、median、min、max、coverage(有效百分比)、quantile(q)。"
               "grid 可省略。可以在 grid 前加 crop(x0,y0,x1,y1)，坐标和 q 使用 0 到 1 的数字。"
               "行从上到下，列从左到右。", [], ["program"])}


def prepare(original, previous_lock, output, source_commit, backend_digest, profile_file, actor, *, objects=False, queries=False):
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
    if set(old).intersection(services) or set(old).intersection(tools):
        raise ValueError("requested capability already exists in the original site")
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        base = Path(tmp) / "input"
        shutil.copytree(original, base)
        trust = base / "trust"
        index = json.loads((trust / "catalog.json").read_bytes())
        pending = []
        for pid in [*services, *tools]:
            if pid in services:
                path, result_key, args = services[pid]
                module = service_module
                name, kind = pid, "service"
                spec = {"service": name, "path": path, "result_key": result_key}
                deps = ["sensing.observations"]
                fields = {"ok": {"type": "boolean"}}
                network = {"ingress": [], "egress": [peer]}
                settings[pid] = {"backend_digest": backend_digest, "profile_digest": config_digest}
                item = deepcopy(old["sensing.published"])
            else:
                name, service, description, utterance, args = tools[pid]
                kind, module, deps = "tool", "agent.read_service_tool", [service]
                spec = {"service": service, "legacy": {"id": name, "version": "1.0.0", "description": description,
                        "effect": "read", "parameters": {key: "string" for key in args},
                        "utterances": [utterance] if isinstance(utterance, str) else utterance}}
                fields = {services[service][1]: {"type": "object"}, "observations": {"type": "object"}}
                network = {"ingress": [], "egress": []}
                settings[pid] = {} if "actor_ref" in args else {"actor_ref": actor}
                item = deepcopy(old["navigation.read_spatial_observations"])
            schema = name + "/call@1"
            params = obj({key: {"type": "string", "minLength": 1, "maxLength": 128} for key in args})
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
            item.update(id=pid, entrypoint=entry, network=network,
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
    for key in ("original", "out", "profile"):
        cli.add_argument("--"+key, type=Path, required=True)
    for key in ("previous-lock", "source-commit", "backend-digest", "actor"):
        cli.add_argument("--"+key, required=True)
    args = cli.parse_args()
    print(json.dumps(prepare(args.original, args.previous_lock, args.out, args.source_commit,
                             args.backend_digest, args.profile, args.actor, objects=args.objects, queries=args.queries)))
