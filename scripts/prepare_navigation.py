"""Prepare an immutable read-only navigation plugin site; never contacts a device."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]
from prepare_soft_pilots import GIT, compute_content_digest, local_image, obj, save, sha, verify_image
from site_pack import build_lock
from navigation.bench import catalog, start_pose


SERVICES = {
    "sensing.replay": ("sensing.observations", "perception.spatial_facts.replay"),
    "sensing.published": ("sensing.observations", "perception.spatial_facts.published"),
    "localization.weighted": ("localization.position", "perception.spatial_facts.weighted"),
    "navigation.maps": ("navigation.catalog", "navigation.catalog_service"),
    "navigation.planner": ("navigation.route_planner", "navigation.route_service"),
}
TOOLS = {
    "read_spatial_observations": ("读取主体的空间观测", ["sensing.observations"], "读取 {actor_ref} 的空间观测"),
    "nav_locate": ("查询主体的定位及其来源", ["sensing.observations", "localization.position"], "查询 {actor_ref} 的定位"),
    "nav_resolve": ("解析当前地图中的目的地", ["navigation.catalog"], "为 {actor_ref} 解析地点 {destination_ref}"),
    "nav_plan": ("在当前地图上只读试算路线", ["sensing.observations", "localization.position", "navigation.route_planner"],
                 "为 {actor_ref} 试算到 {destination_ref} 的路线"),
}


def bench_settings():
    site = catalog()
    snapshots, policies = {}, {}
    for actor in site["actors"]:
        pose = start_pose(actor)
        names = {"position.a": "bench.a", "position.b": "bench.b"}
        samples = [{**{k: pose[k] for k in ("map_ref", "frame_id", "clock_id", "localization_epoch", "stamp_ns")},
                    "source_id": name, "kind": "position", "calibration_ref": calibration,
                    "position_m": pose["position_m"], "variance_m2": [0.04] * len(pose["position_m"])}
                   for name, calibration in names.items()]
        snapshots[actor] = {"clock_id": pose["clock_id"], "as_of_ns": pose["stamp_ns"], "samples": samples}
        policies[actor] = {**{k: pose[k] for k in ("map_ref", "frame_id", "clock_id", "localization_epoch")},
                           "max_age_ns": 100000000, "max_skew_ns": 20000000, "sources": names}
    return {"sensing.replay": {"actors": snapshots}, "localization.weighted": {"actors": policies},
            "navigation.maps": {"catalog": site}}


def prepare(output, manifest, commit, *, live=False, settings=None):
    if not GIT.fullmatch(commit):
        raise ValueError("expected a complete source commit")
    image = sha(manifest)
    verify_image(manifest, image)
    profile = "navigation-live" if live else "navigation-bench"
    settings = bench_settings() if settings is None and not live else settings
    if not settings or not {"localization.weighted", "navigation.maps"} <= settings.keys():
        raise ValueError("provide site-owned maps and localization policies")
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory) / "release"
        trust, releases = base / "trust", base / "releases"
        assets = trust / "assets"
        assets.mkdir(parents=True)
        releases.mkdir()
        index = {"schemas": {}, "implementations": {}}
        descriptors = []
        entries = [(pid, name, module, "service", ["navigation.catalog"] if name == "navigation.route_planner" else [])
                   for pid, (name, module) in SERVICES.items()
                   if pid != ("sensing.replay" if live else "sensing.published")]
        entries += [("navigation." + name, name, "navigation.tools", "tool", deps)
                    for name, (_, deps, _) in TOOLS.items()]
        for pid, name, module, kind, deps in entries:
            spec = {}
            if kind == "tool":
                description, _, utterance = TOOLS[name]
                args = {"actor_ref": "string"}
                if name in ("nav_plan", "nav_resolve"):
                    args["destination_ref"] = "string"
                spec["legacy"] = {"id": name, "version": "1.0.0", "description": description,
                                  "effect": "read", "parameters": args, "utterances": [utterance]}
                params = obj({k: {"type": "string", "minLength": 1, "maxLength": 128} for k in args})
            else:
                params = {"type": "object"} if name in ("localization.position", "navigation.route_planner") else (
                    obj({}) if name == "navigation.catalog" else obj({"actor_ref": {"type": "string"}}))
            schema_ref = name + "/call@1"
            schema_file = "assets/" + name + ".json"
            save(trust / schema_file, params)
            index["schemas"][schema_ref] = schema_file
            source = (ROOT / (module.replace(".", "/") + ".py")).read_bytes()
            code_digest = sha(source)
            file = "assets/" + code_digest[7:] + ".py"
            (trust / file).write_bytes(source)
            network = {"ingress": [], "egress": ["spatial-facts"] if pid == "sensing.published" else []}
            entrypoint = module + ":apply"
            index["implementations"].setdefault(code_digest, []).append({
                "id": pid, "exports": [name], "artifact": file, "kind": "code", "entrypoint": entrypoint,
                "class": "soft", "source_commit": commit, "image_digest": image, "network": network})
            fields = {"nav_plan": {"route": "object", "localization": "object"},
                      "nav_resolve": {"map_ref": "string"}, "nav_locate": {"position_m": "array", "sources": "array"},
                      "read_spatial_observations": {"samples": "array", "basis": "string"}}.get(name, {})
            descriptors.append({
                "schema_version": 2, "id": pid, "version": "1.0.0", "kind": kind, "effects": "read",
                "class": "soft", "runtime": "inproc", "entrypoint": entrypoint, "content_digest": None,
                "provenance": {"author": "edge-agent-ros", "source_commit": commit, "image_digest": image,
                               "code_digest": code_digest}, "mounts": [], "network": network,
                "provides": [{"type": kind, "name": name, "schema_ref": schema_ref}],
                "requires": [{"type": "service", "name": dep, "schema_ref": dep + "/call@1"} for dep in deps],
                "parameters": {"schema_ref": schema_ref},
                "outcomes": obj({k: {"type": v} for k, v in fields.items()}, closed=False),
                "config_schema": obj({"profile": {"type": "object", "const": spec}, "settings": {"type": "object"}}),
                "hot_reload": True, "drain_required": True, "device_lease": None, "stop_protocol": None,
                "verifier": None, "operation_ledger": None, "health": {"probe": "ready", "ttl_ms": 6000, "timeout_ms": 1000},
                "resources": {"cpu": 0.5, "memory_mib": 128, "max_inflight": 2, "shutdown_ms": 5000},
                "policy": {"install": ["release-admin"], "enable": ["site-operator"], "site_approval": False}})
        save(trust / "catalog.json", index)
        for descriptor in descriptors:
            descriptor["content_digest"] = compute_content_digest(descriptor, trust)
            save(releases / (descriptor["id"] + ".plugin.json"), descriptor)
        overlay = base / "overlay.json"
        save(overlay, {"settings": settings})
        return build_lock(ROOT / "capabilities", ROOT / "profiles" / (profile + ".json"), base, overlay, output)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--site", type=Path, required=True)
    cli.add_argument("--source-commit", required=True)
    cli.add_argument("--image", required=True, help="local platform image containing this source revision")
    cli.add_argument("--live-settings", type=Path, help="site map catalog and localization policies for published facts")
    args = cli.parse_args()
    result = prepare(args.site, local_image(args.image), args.source_commit, live=bool(args.live_settings),
                     settings=json.loads(args.live_settings.read_bytes()) if args.live_settings else None)
    print(json.dumps({"lock": result, "site": str(args.site)}))
