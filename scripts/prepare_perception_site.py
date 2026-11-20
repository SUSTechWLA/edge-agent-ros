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


def prepare(original, previous_lock, output, source_commit, backend_digest, profile_file, actor):
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
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        base = Path(tmp) / "input"
        shutil.copytree(original, base)
        trust = base / "trust"
        index = json.loads((trust / "catalog.json").read_bytes())
        pending = []
        for pid in [*SERVICES, *TOOLS]:
            if pid in SERVICES:
                path, result_key, args = SERVICES[pid]
                name, kind, module = pid, "service", "perception.spatial_facts.compute_service"
                spec = {"service": name, "path": path, "result_key": result_key}
                deps = ["sensing.observations"]
                fields = {"ok": {"type": "boolean"}}
                network = {"ingress": [], "egress": ["stationary-perception"]}
                settings[pid] = {"backend_digest": backend_digest, "profile_digest": config_digest}
                item = deepcopy(old["sensing.published"])
            else:
                name, service, description, utterance, args = TOOLS[pid]
                kind, module, deps = "tool", "agent.read_service_tool", [service]
                spec = {"service": service, "legacy": {"id": name, "version": "1.0.0", "description": description,
                        "effect": "read", "parameters": {key: "string" for key in args}, "utterances": [utterance]}}
                fields = {SERVICES[service][1]: {"type": "object"}, "observations": {"type": "object"}}
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
        for stage, packs in (("ready", ["perception-static"]), ("candidate", ["perception-static", "perception-markup"])):
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
    for key in ("original", "out", "profile"):
        cli.add_argument("--"+key, type=Path, required=True)
    for key in ("previous-lock", "source-commit", "backend-digest", "actor"):
        cli.add_argument("--"+key, required=True)
    args = cli.parse_args()
    print(json.dumps(prepare(args.original, args.previous_lock, args.out, args.source_commit,
                             args.backend_digest, args.profile, args.actor)))
