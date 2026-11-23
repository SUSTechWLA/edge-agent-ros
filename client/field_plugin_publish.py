"""Deployment-owned admission of a qualified read-only field plugin into a running DSH site."""
from contextlib import closing, contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]
from edge_contracts.descriptor import GIT, NAME, SHA, canonical, compute_content_digest, validate_descriptor
from perception.spatial_facts import authored_screen as adapter
from site_pack import verify
from soft_host import settings_for
from soft_runtime import _digest as soft_digest
from v2_registry import V2Registry, admissible, fingerprint, immutable_version

TOOL_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
DEPENDENCY = "perception.frame_statistics"
TEMPLATE = "perception.measure_grid"
ENTRYPOINT = "perception.spatial_facts.authored_screen:apply"
READ_POLICY = {"classes": ["soft"], "effects": ["read"], "site_approval": False}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_bytes())


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def write(path, raw, *, immutable=False):
    require(not path.is_symlink(), "unexpected symlink: " + path.name)
    if path.exists():
        if path.read_bytes() == raw:
            return
        require(not immutable, "field release file conflict: " + path.name)
    previous = path.stat() if path.exists() else None
    mode = stat.S_IMODE(previous.st_mode) if previous else 0o644
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".field-tmp-", delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        # A publisher with CAP_CHOWN and no CAP_FOWNER can set the mode only while it owns the temp file.
        tmp.chmod(mode)
        if previous is not None and hasattr(os, "chown"):
            current = tmp.stat()
            if (previous.st_uid, previous.st_gid) != (current.st_uid, current.st_gid):
                os.chown(tmp, previous.st_uid, previous.st_gid)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def serial(site):
    # SQLite releases the process lock on crash. No release state depends on this DB.
    with closing(sqlite3.connect(site / ".field-publisher.sqlite", timeout=45)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        try:
            yield
        finally:
            lock.rollback()


def commit_for_adapter():
    commit = os.getenv("EDGE_FIELD_ADAPTER_COMMIT")
    if not commit:
        rel = Path(adapter.__file__).resolve().relative_to(ROOT).as_posix()
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        sealed = subprocess.check_output(["git", "show", commit + ":" + rel], cwd=ROOT)
        require(sealed.replace(b"\r\n", b"\n") == Path(adapter.__file__).read_bytes().replace(b"\r\n", b"\n"),
                "adapter differs from the recorded commit")
    require(GIT.fullmatch(commit), "invalid site adapter commit")
    return commit


def authorised_policy(path):
    obj = read(path)
    require(set(obj) == {"schema_version", "bindings"} and type(obj["schema_version"]) is int
            and obj["schema_version"] == 1 and isinstance(obj["bindings"], dict)
            and all(NAME.fullmatch(k) and isinstance(v, str) and SHA.fullmatch(v)
                    for k, v in obj["bindings"].items()), "invalid deployment binding policy")
    return obj


def build(stage, original, lock, items, candidate, program):
    name = candidate["name"]
    pid = "field." + name
    schema_ref = pid + "/call@1"
    template = items[TEMPLATE]
    require(template["class"] == "soft" and template["runtime"] == "inproc" and template["kind"] == "tool"
            and template["effects"] == "read" and not template["mounts"] and template["hot_reload"]
            and not template["network"]["ingress"] and not template["network"]["egress"],
            "field template is not a mount-free read tool")
    for key in ("releases", "config", "trust"):
        shutil.copytree(original / key, stage / key)
    record = deepcopy(template)
    actor = read(original / "config" / (TEMPLATE + ".json"))["actor_ref"]
    settings = {"actor_ref": actor}
    legacy = {"id": name, "version": "1.0.0", "description": candidate["description"], "effect": "read",
              "parameters": {k: v["type"] for k, v in adapter.CALL_SCHEMA["properties"].items()},
              "utterances": [], "_parameter_schema": adapter.CALL_SCHEMA}
    profile = {"legacy": legacy, "program": program}
    source = Path(adapter.__file__).read_bytes()
    digest = sha(source)
    code_path = "assets/" + digest[7:] + ".py"
    schema_path = "assets/" + pid + "-call.json"
    files = {"trust/" + code_path: source, "trust/" + schema_path: canonical(adapter.CALL_SCHEMA) + b"\n",
             "config/" + pid + ".json": canonical(settings) + b"\n"}
    for relative, raw in files.items():
        write(stage / relative, raw, immutable=True)
    dep, = [r for r in items[DEPENDENCY]["provides"] if r["name"] == DEPENDENCY and r["type"] == "service"]
    cfg_schema = {"type":"object", "properties":{"profile":{"type":"object","const":profile},
                  "settings":{"type":"object","const":settings}}, "required":["profile","settings"],
                  "additionalProperties":False}
    outcomes = {k: {"type":"object"} for k in ("review", "analysis", "observations")}
    record.update(id=pid, version="1.0.0", entrypoint=ENTRYPOINT, parameters={"schema_ref":schema_ref},
                  provides=[{"type":"tool","name":name,"schema_ref":schema_ref}], requires=[dep],
                  config_schema=cfg_schema, outcomes={"type":"object","properties":outcomes,"required":list(outcomes)})
    record["provenance"].update(author="field plugin publisher / local model program", code_digest=digest,
                                source_commit=commit_for_adapter())
    implementation = {"id":pid, "exports":[name], "artifact":code_path, "kind":"code", "entrypoint":ENTRYPOINT,
                      "class":"soft", "source_commit":record["provenance"]["source_commit"],
                      "image_digest":record["provenance"]["image_digest"], "network":record["network"]}
    catalog = read(stage / "trust/catalog.json")
    require(catalog["schemas"].get(schema_ref, schema_path) == schema_path, "field schema conflict")
    catalog["schemas"][schema_ref] = schema_path
    providers = catalog["implementations"].setdefault(digest, [])
    if isinstance(providers, dict):
        providers = catalog["implementations"][digest] = [providers]
    existing = [p for p in providers if p["id"] == pid]
    require(not existing or existing == [implementation], "field implementation conflict")
    if not existing:
        providers.append(implementation)
    write(stage / "trust/catalog.json", canonical(catalog)+b"\n")
    record["content_digest"] = compute_content_digest(record, stage / "trust")
    validate_descriptor(record, stage / "trust")
    rel = "releases/" + pid + ".plugin.json"
    files[rel] = canonical(record) + b"\n"
    write(stage / rel, files[rel], immutable=True)
    spec = {"version":record["version"], "content_digest":record["content_digest"], "class":"soft","effects":"read",
            "config_sha256":fingerprint(settings),"site_approval":False}
    require(lock["plugins"].get(pid, spec) == spec, "plugin name already holds a different release")
    # Resolve a one-member pack with its descriptor pinned, using the deployed pack format.
    pack = {"schema_version":1, "id":pid, "policy":READ_POLICY,
            "plugins":[{"id":pid,"version":record["version"],"content_digest":record["content_digest"],"config":settings}]}
    new_lock = deepcopy(lock)
    require(new_lock["packs"].get(pid, fingerprint(pack)) == fingerprint(pack), "pack name conflict")
    new_lock["plugins"][pid] = spec
    new_lock["packs"][pid] = fingerprint(pack)
    write(stage / "pack.lock.json", canonical(new_lock)+b"\n")
    lock_id = fingerprint(new_lock)
    _, wanted = verify(stage, lock_id)
    return record, files, lock_id, wanted, pack


def expected_binding(site, items, pid, name):
    owners = {ref["name"]: key for key, item in items.items() for ref in item["provides"]}
    closure = {}
    def visit(key):
        if key in closure:
            return
        item = items[key]
        _, cfg = settings_for(item, site)
        closure[key] = [item["version"],item["content_digest"],soft_digest(cfg)]
        for ref in item["requires"]:
            visit(owners[ref["name"]])
    visit(pid)
    return soft_digest([pid, name, closure])


def live_record(agent_url, pid, name, binding):
    url = urlsplit(agent_url)
    require(url.scheme == "http" and url.hostname and not (url.username or url.password or url.query or url.fragment)
            and url.path.rstrip("/") in ("", "/agent"), "invalid deployment Agent URL")
    endpoint = "http://" + url.netloc + "/agent/capabilities"
    deadline = time.monotonic() + 32
    while True:
        try:
            with urlopen(endpoint, timeout=3) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            require(len(raw) <= 2 * 1024 * 1024, "capability response exceeds limit")
            rows = [r for r in json.loads(raw)["records"] if r.get("id") == name]
            if len(rows) == 1:
                row = rows[0]
                if (row.get("provider") == pid and row.get("binding") == binding and row.get("class") == "soft"
                        and row.get("source") == "v2_soft" and row.get("effects") == "read"
                        and row.get("version") == "1.0.0" and row.get("availability") == "available"
                        and not row.get("draining")):
                    return row
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise ValueError("new plugin has no available matching live binding")
        time.sleep(0.3)


def native_release(reg, pid, digest):
    pairs = reg.db.execute("""SELECT event_id, json_extract(event,'$.trace_id') FROM module_bus_events
        WHERE json_extract(event,'$.code')='release.applied' AND json_extract(event,'$.data.action')='publish'
        AND json_extract(event,'$.data.id')=?
        AND json_extract(event,'$.data.content_digest')=? ORDER BY seq DESC LIMIT 1""", (pid,digest)).fetchone()
    require(pairs, "missing native plugin admission receipt")
    row = reg.db.execute("""SELECT event_id,json_extract(event,'$.data.lock') FROM module_bus_events
        WHERE json_extract(event,'$.code')='release.pack_applied' AND json_extract(event,'$.trace_id')=?
        ORDER BY seq DESC LIMIT 1""", (pairs[1],)).fetchone()
    require(row, "missing native pack admission receipt")
    return pairs[0], row[0], row[1]


def publish(site: Path, registry: Path, policy: Path, candidate: dict, agent_url: str, evidence_dir: Path) -> dict:
    """Install once. A retry after an interruption verifies the identical release before completing authorization."""
    require(type(candidate) is dict and set(candidate) == {"name", "description", "program"},
            "candidate must contain just name, description and program")
    name, description = candidate["name"], candidate["description"]
    require(isinstance(name, str) and 2 <= len(name) <= 40 and TOOL_NAME.fullmatch(name)
            and name not in ("status", "inspect"), "invalid field tool name")
    require(isinstance(description, str) and description.strip() == description and 1 <= len(description) <= 300
            and all(c.isprintable() for c in description), "invalid field tool description")
    program = adapter.validate_program(candidate["program"])
    proof = adapter.qualify(program)
    site, policy = Path(site).resolve(strict=True), Path(policy)
    if policy.is_dir():
        policy = policy / "approved.json"
    pid = "field." + name
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    with serial(site), V2Registry(registry) as reg, tempfile.TemporaryDirectory() as work:
        policy_before = authorised_policy(policy)
        lock_hash = fingerprint(read(site / "pack.lock.json"))
        lock, items = verify(site, lock_hash)
        require(lock["policy"] == READ_POLICY and all(not v["site_approval"] for v in lock["plugins"].values()),
                "site must permit automatic soft/read admission")
        require(not any(ref["name"] == name for k,v in items.items() if k != pid for ref in v["provides"]),
                "new tool conflicts with an existing export")
        entries = reg.entries()
        require(not any(row["draining"] or row["disposed"] for row in entries), "existing pack is undergoing a handoff")
        live = {r["descriptor"]["id"]:r["descriptor"]["content_digest"] for r in entries}
        installed = reg.db.execute("SELECT lock_digest,allowed FROM v2_pack_policy WHERE singleton=1").fetchone()
        require(installed and json.loads(installed[1]) == live, "registry and installed pack disagree")
        old = deepcopy(lock)
        if pid in old["plugins"]:
            del old["plugins"][pid]
            old["packs"].pop(pid, None)
        require({k:v["content_digest"] for k,v in items.items() if k!=pid} == {k:v for k,v in live.items() if k!=pid},
                "release would change existing plugins")
        stage = Path(work)
        item, files, next_hash, wanted, pack = build(stage, site, lock, items, candidate, program)
        digest = item["content_digest"]
        require(installed[0] in (lock_hash, fingerprint(old), next_hash), "site differs from the deployed lock")
        require(not admissible(item, entries, reg.claimed_exports()), "field plugin has missing providers")
        immutable_version(reg.db, item)
        binding = expected_binding(stage, wanted, pid, name)
        require(policy_before["bindings"].get(name, binding) == binding, "existing read authorization conflicts")
        require(pid not in live or live[pid] == digest, "existing plugin has a different digest")
        for relative, raw in files.items():
            write(site / relative, raw, immutable=True)
        write(site / "trust/catalog.json", (stage / "trust/catalog.json").read_bytes())
        if pid not in live:
            result = reg.install_pack(list(wanted.values()), next_hash)
            require(result["admitted"] == [pid], "unexpected change to installed plugins")
        write(site / "pack.lock.json", (stage / "pack.lock.json").read_bytes())
        verify(site, next_hash)
        native, event, admission = native_release(reg, pid, digest)
        row = live_record(agent_url, pid, name, binding)
        require(authorised_policy(policy) == policy_before, "deployment read policy changed during publication")
        latest = deepcopy(policy_before)
        latest["bindings"][name] = binding
        write(policy, canonical(latest)+b"\n")
        receipt = {"plugin_id":pid,"tool_id":name,"version":item["version"],"content_digest":digest,
                   "program_digest":proof["program_sha256"],"code_digest":item["provenance"]["code_digest"],
                   "binding":binding,"plugin_event_id":native,"event_id":event,"lock_digest":admission,
                   "active_lock":next_hash,"validation":proof,"catalog":row,"pack":pack}
        write(evidence_dir / "published-plugin.json", canonical(receipt)+b"\n")
        return receipt

