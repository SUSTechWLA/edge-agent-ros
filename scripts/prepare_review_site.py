"""Build one independent read-only RGB-D review site using the v2 pack and binding ABI."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "scripts")]
from client.field_plugin_publish import expected_binding
from edge_contracts.descriptor import GIT, SHA, compute_content_digest
from perception.review055.definitions import GRAPH_LANGUAGE, SPECS
from prepare_soft_pilots import obj, save, sha, verify_image
from site_pack import build_lock, verify


VERSION = "1.0.0"
PACK = "perception-rgbd-review"
POLICY = {"classes":["soft"], "effects":["read"], "site_approval":False}
CONTRACT = "obstacle_review"
DESCRIPTION = ("静止相机图像的近距离障碍候选人工复核。对调用者指定的列数和米制距离阈值，返回各区域"
               "有效深度第10百分位、有效覆盖率、近距判定、未知区域以及由近到远的人类复核次序。"
               "支持固定的已录制RGB-D帧。此能力仅用于人类观察。")
# Explicit station identities, independent of model phrasing and plugin name patterns.
ORIGINS = {"review_builtin":"builtin", "review_workflow":"site_workflow", "review_composed":"agent_composed"}


def release(base, catalog, spec, commit, image, manifest, frame, workflow):
    plugin, name, kind = spec["id"], spec["name"], spec["kind"]
    profile = ({"legacy":{"id":name,"version":VERSION,"effect":"read","description":spec["description"],
                          "parameters":{k:v["type"] for k,v in spec["parameters"]["properties"].items()},
                          "utterances":[]}} if kind == "tool" else {"service":name})
    schema_ref, schema_file = name + "/call@1", "assets/" + name + ".schema.json"
    trust = base / "trust"
    save(trust / schema_file, spec["parameters"])
    catalog["schemas"][schema_ref] = schema_file
    network = {"ingress":[], "egress":[]}
    provenance = {"author":"edge-agent-ros", "source_commit":commit, "image_digest":image}
    if spec["runtime"] == "process":
        peer = urlsplit(spec["endpoint"]).hostname
        network["egress"] = [peer]
        role = "workflow" if kind == "tool" else "measure"
        entry = ["python3", "-m", "perception.review055.server", "--role", role]
        source = manifest
        typ, suffix = "image", ".json"
        health = {"frame_ref":frame}
        if role == "workflow":
            health["workflow_digest"] = workflow
        profile.update(endpoint=spec["endpoint"], probe=spec["probe"], health_expected=health)
    else:
        module = spec["module"]
        entry = module + ":apply"
        source = (ROOT / (module.replace(".", "/") + ".py")).read_bytes()
        typ, suffix = "code", ".py"
        provenance["code_digest"] = sha(source)
    digest = sha(source)
    artifact = "assets/" + digest[7:] + suffix
    (trust / artifact).write_bytes(source)
    catalog["implementations"].setdefault(digest, []).append({
        "id":plugin, "exports":[name], "entrypoint":entry, "artifact":artifact, "kind":typ, "class":"soft",
        "source_commit":commit, "image_digest":image, "network":network})
    settings = {"frame_ref":frame}
    return {"schema_version":2,"id":plugin,"version":VERSION,"kind":kind,"class":"soft","effects":"read",
            "runtime":spec["runtime"],"entrypoint":entry,"provenance":provenance,"mounts":[],"network":network,
            "provides":[{"type":kind,"name":name,"schema_ref":schema_ref}],
            "requires":[{"type":"service","name":dep,"schema_ref":dep + "/call@1"} for dep in spec["requires"]],
            "parameters":{"schema_ref":schema_ref},"outcomes":spec["outcomes"],
            "config_schema":obj({"profile":{"type":"object","const":profile},
                                 "settings":{"type":"object","const":settings}}),
            "content_digest":None,"hot_reload":True,"drain_required":True,"device_lease":None,
            "stop_protocol":None,"verifier":None,"operation_ledger":None,
            "health":{"probe":"ready","timeout_ms":1500,"ttl_ms":10000},
            "resources":{"cpu":1,"memory_mib":192,"max_inflight":2,"shutdown_ms":5000},
            "policy":{"install":["release-admin"],"enable":["site-operator"],"site_approval":False}}


def prepare(site, policy_dir, manifest, image, commit, frame, workflow):
    verify_image(manifest, image)
    if not GIT.fullmatch(commit) or not all(SHA.fullmatch(v) for v in (frame, workflow)):
        raise ValueError("invalid immutable frame, workflow or source identity")
    if site.exists() or policy_dir.exists() or site == policy_dir:
        raise ValueError("select new distinct site and policy directories")
    site.parent.mkdir(exist_ok=True, parents=True)
    policy_dir.parent.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(dir=site.parent) as tmp, tempfile.TemporaryDirectory(dir=policy_dir.parent) as cfg:
        tmp, cfg = Path(tmp), Path(cfg)
        base = tmp / "base"
        (base / "trust/assets").mkdir(parents=True)
        (base / "releases").mkdir()
        index = {"schemas":{}, "implementations":{}}
        items = [release(base, index, spec, commit, image, manifest, frame, workflow) for spec in SPECS]
        save(base / "trust/catalog.json", index)
        members = []
        for item in items:
            item["content_digest"] = compute_content_digest(item, base / "trust")
            save(base / "releases" / (item["id"] + ".plugin.json"), item)
            members.append({"id":item["id"], "version":VERSION, "content_digest":item["content_digest"],
                            "config":{"frame_ref":frame}})
        (tmp / PACK).mkdir()
        save(tmp / PACK / "pack.json", {"schema_version":1,"id":PACK,"plugins":members,"policy":POLICY})
        profile = tmp / "profile.json"
        save(profile, {"schema_version":1,"id":PACK,"packs":[PACK],"settings":{},"policy":POLICY})
        built = tmp / "site"
        lock = build_lock(tmp, profile, base, None, built)
        _, active = verify(built, lock)
        rows = {}
        for desc in active.values():
            if desc["kind"] != "tool":
                continue
            name = desc["provides"][0]["name"]
            row = {"provider":desc["id"], "tool":name, "origin":ORIGINS[name],
                   "binding":expected_binding(built, active, desc["id"], name)}
            if row["origin"] == "agent_composed":
                row["graph_language"] = GRAPH_LANGUAGE
            rows[name] = row
        output = cfg / "policy"
        output.mkdir()
        save(output / "approved.json", {"schema_version":1,"bindings":{k:r["binding"] for k,r in rows.items()}})
        for first in ORIGINS:
            ranked = [rows[first], *(rows[name] for name in ORIGINS if name != first)]
            policy = {"schema_version":1,"contracts":{CONTRACT:{"description":DESCRIPTION,"implementations":ranked}}}
            save(output / (first + ".json"), policy)
            if first == "review_builtin":
                save(output / "implementations.json", policy)
        built.rename(site)
        output.rename(policy_dir)
    return {"lock":lock, "frame_ref":frame, "workflow_digest":workflow, "soft_platform_digest":image,
            "tools":{name:row["binding"] for name,row in rows.items()}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("site", "policy-dir", "image-manifest"):
        parser.add_argument("--" + key, type=Path, required=True)
    for key in ("source-commit", "image-digest", "frame-ref"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--workflow-digest", help="reviewed external module SHA-256; defaults to the bundled example")
    args = parser.parse_args()
    workflow = args.workflow_digest or sha((ROOT / "perception/review055/site_workflow.py").read_bytes())
    print(json.dumps(prepare(args.site, args.policy_dir, args.image_manifest.read_bytes(), args.image_digest,
                             args.source_commit, args.frame_ref, workflow), ensure_ascii=False))
