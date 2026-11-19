"""Provision a new site trust directory for the SDD-045 first-party pilots. No registry or network writes."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from derived_tools import capability, parse_definition
from plugin_descriptor import GIT, SHA, compute_content_digest, load_descriptor


DEFAULT_SERIES = {"schema_version":2, "id":"derived.series_mean", "description":"求输入数列的平均值",
                  "transforms":[], "reduce":"mean"}
MANIFEST_LIMIT = 4 * 1024 * 1024


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def verify_image(raw, expected):
    if not SHA.fullmatch(expected) or len(raw) > MANIFEST_LIMIT or sha(raw) != expected:
        raise ValueError("image manifest bytes do not match the expected SHA-256")
    image = json.loads(raw)
    if (image.get("schemaVersion") != 2 or not isinstance(image.get("layers"), list)
            or not SHA.fullmatch(image.get("config", {}).get("digest", ""))):
        raise ValueError("expected a platform OCI or Docker image manifest (config + layers)")
    return raw


def local_image(reference):
    # Docker's unqualified Id/Descriptor can identify the multi-platform index.
    if not reference or reference.startswith("-"):
        raise ValueError("invalid local image reference")
    inspect = json.loads(subprocess.check_output(["docker", "image", "inspect", reference]))[0]
    platform = "/".join(s for s in (inspect["Os"], inspect["Architecture"], inspect.get("Variant")) if s)
    selected = json.loads(subprocess.check_output(["docker", "image", "inspect", "--platform", platform, reference]))[0]
    descriptor = selected.get("Descriptor") or {}
    expected = descriptor.get("digest", "")
    if not SHA.fullmatch(expected) or "manifest" not in descriptor.get("mediaType",""):
        raise ValueError("no locally attested platform manifest; provide --image-manifest and --image-digest")
    raw = None
    with tempfile.TemporaryFile() as errors:
        cmd = ["docker", "image", "save", "--platform", platform, reference]
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors) as proc:
            try:
                with tarfile.open(fileobj=proc.stdout, mode="r|*") as tar:
                    for entry in tar:
                        if entry.name != "blobs/sha256/" + expected[7:]:
                            continue
                        if not entry.isfile() or entry.size > MANIFEST_LIMIT:
                            raise ValueError("invalid platform manifest blob")
                        with closing(tar.extractfile(entry)) as file:
                            raw = verify_image(file.read(), expected)
                        break
            finally:
                proc.stdout.close()
                if proc.poll() is None and raw is not None:
                    proc.terminate()
            if proc.wait() and raw is None:
                errors.seek(0)
                raise ValueError("local docker image save failed: " + errors.read(1000).decode(errors="replace"))
    if raw is None:
        raise ValueError("cannot identify one local platform image manifest; provide --image-manifest and --image-digest")
    return raw


def obj(properties, required=None, closed=True):
    return {"type":"object", "properties":properties, "required":list(properties) if required is None else required,
            "additionalProperties":not closed}


def evidence_contract(name, software_series):
    """Native outcomes describe the returned evidence inside a successful tool receipt."""
    properties = {
        "session.read":{"document":"object","request_id":"string"},
        "read_session":{"query_id":"string","scope":"object","coverage":"object","markdown":"string","refs":"array"},
        "evidence_schema":{"query_id":"string","schema_version":"integer","table":"string","columns":"array"},
        "query_evidence":{"query_id":"string","scope":"object","coverage":"object","columns":"array","rows":"array"},
    }
    fields = ({"source":"string","input_sha256":"string","input_count":"integer","output_count":"integer",
               "value":"number","definition_version":"string"} if software_series else properties[name])
    return obj({key:{"type":kind} for key,kind in fields.items()}, closed=False)


def save(path, content):
    path.write_bytes((json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


def prepare_site(site, manifest, commit, definition):
    if not GIT.fullmatch(commit):
        raise ValueError("source commit must be a full Git object ID")
    if definition.get("schema_version") != 2:
        raise ValueError("only validated caller-supplied series may be installed as soft tools")
    derived = capability(definition)
    image = sha(manifest)
    verify_image(manifest, image)
    site = Path(site)
    if site.exists():
        raise ValueError("select a new site directory; an existing trust root must remain immutable")
    site.parent.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(dir=site.parent, prefix=".soft-pilots-") as staging:
        stage = Path(staging) / "site"
        trust = stage / "trust"
        assets = trust / "assets"
        releases = stage / "releases"
        assets.mkdir(parents=True)
        releases.mkdir()
        catalog = {"schemas":{}, "implementations":{}}
        descriptors = []
        code = (ROOT / "agent" / "soft_series_plugin.py").read_bytes()
        impl = sha(code)
        code_file = "assets/" + impl[7:] + ".py"
        image_file = "assets/" + image[7:] + ".json"
        (trust / code_file).write_bytes(code)
        (trust / image_file).write_bytes(manifest)
        session = {"schema_version":1, "id":"session.default", "version":"1",
                   "endpoint":"http://session:8126/session", "provides":["session.read"], "requires":[], "draining":False}
        pilots = [(session["id"],"session.read","service", session, "soft-session")]
        for path in sorted((ROOT / "evidence" / "tools").glob("*.json")):
            tool = json.loads(path.read_bytes())
            pilots.append(("evidence." + tool["id"], tool["id"], "tool", tool, "soft-evidence"))
        pilots.append((derived["id"],derived["id"],"tool",derived,None))
        for plugin_id, name, kind, legacy, peer in pilots:
            profile = {"legacy":legacy}
            if peer == "soft-session":
                profile.update(endpoint="http://soft-session:8126/session", probe="http://soft-session:8126/health",
                               health_expected={"plugin_id":"session.default", "plugin_version":"1", "service":"session.read"})
                params = obj({"schema_version":{"type":"integer","const":1},
                              **{k:{"type":"string"} for k in ("request_id","plugin_id","plugin_version","binding","task_id","trace_id")}},
                             ["schema_version","request_id","plugin_id","plugin_version","binding"])
            else:
                params = obj({key:{"type":t} for key,t in legacy["parameters"].items()})
                if peer:
                    profile.update(endpoint="http://soft-evidence:8125/invoke?tool=" + name,
                                   probe="http://soft-evidence:8125/schema", health_expected={"schema_version":1, "table":"events"})
            contract_ref = name + "/call@1"
            schema_path = "assets/" + name + ".schema.json"
            save(trust / schema_path, params)
            catalog["schemas"][contract_ref] = schema_path
            network = {"ingress":[], "egress":[peer] if peer else []}
            provenance = {"author":"edge-agent-ros", "source_commit":commit, "image_digest":image}
            if peer:
                executable = "sessions_plugin" if peer == "soft-session" else "evidence"
                entrypoint = ["python3", "/opt/edge/" + executable + "/provider.py"]
            else:
                entrypoint = "soft_series_plugin:apply"
                provenance["code_digest"] = impl
            schema = obj({"profile":{"type":"object","const":profile}, "settings":{"type":"object"}})
            item = {"schema_version":2,"id":plugin_id,"version":"1.0.0","kind":kind,"effects":"read",
                    "class":"soft","runtime":"process" if peer else "inproc","entrypoint":entrypoint,
                    "provenance":provenance,"content_digest":None,"mounts":[],"network":network,
                    "provides":[{"name":name,"type":kind,"schema_ref":contract_ref}],"requires":[],
                    "parameters":{"schema_ref":contract_ref},"outcomes":evidence_contract(name, not peer),"config_schema":schema,
                    "hot_reload":True,"drain_required":True,"device_lease":None,"stop_protocol":None,
                    "verifier":None,"operation_ledger":None,
                    "health":{"probe":"ready","ttl_ms":6000,"timeout_ms":1000},
                    "resources":{"cpu":0.5,"memory_mib":128,"max_inflight":2,"shutdown_ms":5000},
                    "policy":{"install":["release-admin"],"enable":["site-operator"],"site_approval":False}}
            records = catalog["implementations"].setdefault(image if peer else impl, [])
            records.append({"id":plugin_id,"exports":[name],"artifact": image_file if peer else code_file,
                            "kind":"image" if peer else "code","entrypoint":entrypoint,"class":"soft",
                            "source_commit":commit,"image_digest":image,"network":network})
            descriptors.append(item)
        save(trust / "catalog.json", catalog)
        for item in descriptors:
            item["content_digest"] = compute_content_digest(item,trust)
            target = releases / (item["id"] + ".plugin.json")
            save(target, item)
            load_descriptor(target,trust)
        stage.rename(site)
    return [item["id"] for item in descriptors]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="locally available Docker image; does not pull")
    source.add_argument("--image-manifest", type=Path, help="offline OCI platform manifest bytes")
    parser.add_argument("--image-digest", help="required with --image-manifest")
    parser.add_argument("--derived", type=Path, help="reviewed v2 caller-series definition")
    parser.add_argument("--profile", type=Path, help="resolve a board profile and write its immutable lock")
    parser.add_argument("--override", type=Path, help="optional narrowing overlay used with --profile")
    parser.add_argument("--physical-site",type=Path,help="separate site-approved camera deployment referenced by the profile")
    args = parser.parse_args()
    if bool(args.image_manifest) != bool(args.image_digest):
        parser.error("--image-manifest and --image-digest are used together")
    if args.image_manifest:
        with args.image_manifest.open("rb") as stream:
            manifest = verify_image(stream.read(MANIFEST_LIMIT + 1), args.image_digest)
    else:
        manifest = local_image(args.image)
    if args.derived:
        with args.derived.open("rb") as stream:
            definition = parse_definition(stream.read(8193))
    else:
        definition = DEFAULT_SERIES
    if (args.override or args.physical_site) and not args.profile:
        parser.error("--override and --physical-site require --profile")
    result={"site":str(args.site.resolve()),"image_digest":sha(manifest)}
    if args.profile:
        from site_pack import build_lock, verify
        with tempfile.TemporaryDirectory(prefix="edge-pack-input-") as tmp:
            base=Path(tmp)/"trusted"
            prepare_site(base,manifest,args.source_commit,definition)
            result["lock"]=build_lock(ROOT/"capabilities",args.profile,base,args.override,args.site,args.physical_site)
        result["plugins"]=list(verify(args.site,result["lock"])[1])
    else:
        result["plugins"]=prepare_site(args.site, manifest, args.source_commit, definition)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
