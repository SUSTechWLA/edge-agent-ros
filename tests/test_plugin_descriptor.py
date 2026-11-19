"""SDD-045 AC1/3/5. Test site controls the catalog and bytes separately from the publisher."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import plugin_descriptor as desc


def descriptor_fixture(trust_root, *, plugin_id="series.test", version="1.0.0", runtime="inproc", track="soft"):
    root = Path(trust_root)
    root.mkdir(exist_ok=True, parents=True)
    manifest = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
    image = "sha256:" + hashlib.sha256(manifest).hexdigest()
    if runtime == "inproc":
        artifact = b"# Reviewed fixture code\ndef apply(ctx):\n    return None\n"
        entry = "fixture:apply"
    else:
        artifact = manifest
        entry = ["python3", "-m", "fixture"]
    impl = "sha256:" + hashlib.sha256(artifact).hexdigest()
    artifact_name = impl[7:] + ".bin"
    (root / artifact_name).write_bytes(artifact)
    schema = {"type":"object", "properties":{}, "additionalProperties":False}
    schema_name = "fixture/input@1"
    (root / "input.json").write_text(json.dumps(schema), encoding="utf-8")
    provenance = {"author":"unit-fixture", "image_digest":image, "source_commit":"a" * 40}
    if runtime == "inproc":
        provenance["code_digest"] = impl
    item = {
        "schema_version":2, "id":plugin_id, "version":version, "content_digest":None,
        "provenance":provenance, "kind":"tool", "class":track, "runtime":runtime,
        "effects":"read", "hot_reload":track == "soft", "drain_required":True,
        "parameters":{"schema_ref":schema_name}, "outcomes":schema, "config_schema":schema,
        "provides":[{"name":plugin_id, "type":"tool", "schema_ref":schema_name}], "requires":[],
        "device_lease":None, "stop_protocol":None, "verifier":None, "operation_ledger":None,
        "entrypoint":entry, "mounts":[], "network":{"ingress":[], "egress":[]},
        "resources":{"cpu":0.5, "memory_mib":128, "max_inflight":4, "shutdown_ms":1000},
        "health":{"probe":"ready", "timeout_ms":500, "ttl_ms":2000},
        "policy":{"install":["release-admin"], "enable":["site-operator"], "site_approval":False},
    }
    if track == "physical":
        item.update(device_lease="chassis.lease", stop_protocol="chassis.stopped", verifier="independent.verify",
                    operation_ledger="gateway.operations", effects="actuation")
    catalog_path = root / "catalog.json"
    catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else {"implementations":{}, "schemas":{}}
    catalog["schemas"][schema_name] = "input.json"
    records = catalog["implementations"].setdefault(impl, [])
    records[:] = [r for r in records if r["id"] != plugin_id]
    records.append({"id":plugin_id, "class":track, "artifact":artifact_name, "entrypoint":entry,
                    "kind":"code" if runtime == "inproc" else "image", "image_digest":image,
                    "source_commit":provenance["source_commit"]})
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    item["content_digest"] = desc.compute_content_digest(item, root)
    return item


FIXTURES = ROOT / "tests" / "fixtures" / "plugin-tracks"


def legacy_samples(root, code_root):
    """Exercise original public reader, manager, tool identities and deterministic provider bytes."""
    import author_tool
    import derived_tools
    import planning
    import plugin_manager
    import runtime_plugins
    from evidence.provider import EvidenceProvider
    from evidence.query import dump
    from sessions_plugin.provider import SessionProvider, encoded
    from uuid import UUID

    inputs = json.loads((FIXTURES / "v1-input.json").read_bytes())
    wire = lambda obj: json.dumps(obj, ensure_ascii=False, allow_nan=False)
    out = {}
    services = root / "runtime"
    derived = root / "derived"
    services.mkdir()
    derived.mkdir()
    for row in inputs["services"]:
        (services / (row["id"] + ".json")).write_text(json.dumps(row), encoding="utf-8")
    for tool in (code_root / "evidence" / "tools").glob("*.json"):
        shutil.copyfile(tool, root / tool.name)
    (root / "verbatim.json").write_text(json.dumps(inputs["tool"]), encoding="utf-8")
    for name in ("series", "odometry"):
        row = inputs[name]
        (derived / (row["id"] + ".json")).write_text(json.dumps(row), encoding="utf-8")
    with patch.dict(os.environ, {"EDGE_PLUGIN_DIR":str(root), "EDGE_EXTRA_PLUGIN_DIR":""}):
        caps = planning.registry()
        snap = runtime_plugins.snapshot()
        out["capabilities"] = wire({"capabilities":planning.catalog(caps)})
        out["tool-bindings"] = wire({name:planning.binding(cap) for name,cap in caps.items()})
        out["series-receipt"] = wire(derived_tools.evaluate(inputs["series"], inputs["arguments"]))
        out["odometry-receipt"] = wire(derived_tools.evaluate(inputs["odometry"], {
            "speed":1.25, "source":"odometry", "frame":"base_link", "stamp_ns":1024}))
        out["service-catalog"] = wire(snap.public())
        frozen = snap.pin("report.read")
        out["service-pin"] = wire(frozen)
        path = services / "session.default.json"
        normalized = plugin_manager.descriptor(path)
        out["published"] = author_tool.json_bytes(normalized).decode()
        out["manager-revision"] = plugin_manager.revision(normalized)
        author_tool.write_atomic(path, normalized)
        out["explicit-false-pin"] = wire(runtime_plugins.snapshot().pin("report.read"))
        author_tool.write_atomic(path, dict(normalized, draining=True))
        drained = runtime_plugins.snapshot()
        out["draining-revision"] = plugin_manager.revision(plugin_manager.descriptor(path))
        out["draining-pin"] = wire(drained.pin("report.read", frozen["binding"]))
        out["draining-catalog"] = wire(drained.public())
        try:
            drained.pin("report.read")
        except runtime_plugins.PluginUnavailable as exc:
            out["new-work-refusal"] = exc.code
        path.unlink()
        out["dependency-outage"] = wire(runtime_plugins.snapshot().public())
        out["absent-directory"] = wire(runtime_plugins.ServiceSnapshot(root / "missing", observing=True).public())
    bad_session = SessionProvider(None, root / "session.sqlite")
    try:
        status, reply = bad_session.invoke({"schema_version":1, "request_id":"fixed"})
        out["session-rejection"] = str(status) + " " + encoded(reply).decode()
    finally:
        bad_session.close()
    evidence = EvidenceProvider(None, root / "evidence.sqlite")
    try:
        # Only fix the query ID; journal event IDs remain unique even when issued in the same request.
        from types import SimpleNamespace
        clock = SimpleNamespace(uuid4=lambda: UUID(int=777))
        with patch("evidence.provider.uuid", clock):
            body = {"operation_id":"golden/op", "version":"1", "arguments":{}}
            for name in ("evidence-call", "evidence-replay"):
                status, reply = evidence.invoke("evidence_schema", body)
                out[name] = str(status) + " " + dump(reply)
    finally:
        evidence.close()
    return out


class LegacyCompatibility(unittest.TestCase):
    def test_pinned_pre_migration_wire_bytes(self):
        """The shipped oracle was extracted from git archive 8520e43; this test cannot rewrite it."""
        expected = json.loads((FIXTURES / "v1-expected.json").read_bytes())
        with tempfile.TemporaryDirectory() as tmp:
            actual = legacy_samples(Path(tmp), ROOT)
        self.assertEqual(set(actual), set(expected))
        for name, raw in actual.items():
            with self.subTest(sample=name):
                self.assertEqual(raw.encode(), expected[name].encode())

    def test_metadata_adapter_does_not_mutate_v1_or_trust_a_provider_name(self):
        import derived_tools
        import planning
        inputs = json.loads((FIXTURES / "v1-input.json").read_bytes())
        for field, expected in (("series", "soft"), ("odometry", "physical")):
            cap = derived_tools.capability(inputs[field])
            old = copy.deepcopy(cap)
            view = desc.normalize_legacy_tool(cap)
            self.assertEqual(view["binding"], planning.binding(cap))
            self.assertEqual(view["class"], expected)
            self.assertIsNone(view["content_digest"])
            self.assertEqual(cap, old)
        fake = {**inputs["tool"], "id":"read_session"}
        self.assertEqual(desc.normalize_legacy_tool(fake)["class"], "physical")
        service = {**inputs["services"][0], "binding":"legacy", "status":"ready"}
        self.assertEqual(desc.normalize_legacy_service(service)["binding"], "legacy")
