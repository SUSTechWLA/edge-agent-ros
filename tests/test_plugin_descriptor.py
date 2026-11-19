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
sys.path[:0] = [str(ROOT / "agent"), str(ROOT)]
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
    records.append({"id":plugin_id, "class":track, "artifact":artifact_name, "entrypoint":entry, "exports":[plugin_id],
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


class DescriptorContract(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.item = descriptor_fixture(self.root)

    def rejects(self, item, code="plugin.manifest_invalid"):
        with self.assertRaises(desc.DescriptorError) as raised:
            desc.validate_descriptor(item, self.root)
        self.assertEqual(raised.exception.code, code)

    def test_fixed_canonical_vector_and_json_domains(self):
        # RFC 8785 examples: binary64 rounding, fixed/scientific thresholds and UTF-16 sort.
        numbers = [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27, -0.0, 1e-6, 1e-7, 1e20, 1e21]
        self.assertEqual(desc.canonical(numbers),
                         b"[333333333.3333333,1e+30,4.5,0.002,1e-27,0,0.000001,1e-7,100000000000000000000,1e+21]")
        self.assertEqual(desc.canonical({"\ue000":0,"\U00010000":1}), '{"\U00010000":1,"\ue000":0}'.encode())
        for invalid in (float("inf"), float("nan"), 1 << 54, "\ud800"):
            with self.subTest(value=repr(invalid)), self.assertRaises(ValueError):
                desc.canonical(invalid)

    def test_every_top_level_field_required_and_cross_fields_fail_closed(self):
        for field in desc.FIELDS:
            with self.subTest(missing=field):
                self.rejects({k:v for k,v in self.item.items() if k != field})
        mutations = [
            ({"unknown":True},"plugin.manifest_invalid"), ({"schema_version":True},"plugin.manifest_invalid"),
            ({"id":"../tool"},"plugin.manifest_invalid"), ({"version":"1.0.0-01"},"plugin.manifest_invalid"),
            ({"kind":[]}, "plugin.manifest_invalid"), ({"runtime":"fork"}, "plugin.manifest_invalid"),
            ({"effects":"actuation"},"plugin.class_violation"), ({"drain_required":False},"plugin.manifest_invalid"),
            ({"device_lease":"camera"},"plugin.class_violation"), ({"verifier":self.item["id"]},"plugin.verifier_invalid"),
            ({"resources":{"cpu":True}}, "plugin.manifest_invalid"), ({"policy":{}}, "plugin.manifest_invalid"),
            ({"entrypoint":"../unsafe.py"}, "plugin.manifest_invalid"), ({"health":{}}, "plugin.manifest_invalid"),
            ({"provides":["service.name"]},"plugin.manifest_invalid"),
            ({"provides":[{**self.item["provides"][0],"name":"status"}]},"plugin.permission_denied"),
            ({"requires":self.item["provides"]},"plugin.dependency_cycle"),
            ({"parameters":{"type":"object","$ref":"http://example.test"}},"plugin.manifest_invalid"),
            ({"outcomes":{"schema_ref":"missing@1"}},"plugin.manifest_invalid"),
            ({"mounts":[{"source":"/dev/tty","target":"/data","read_only":True}]},"plugin.class_violation"),
            ({"network":{"egress":["chassis"],"ingress":[]}},"plugin.permission_denied"),
        ]
        for change, code in mutations:
            with self.subTest(change=change):
                self.rejects({**self.item, **change}, code)
        good = {**self.item, "version":"0.0.1-alpha.1+linux.01"}
        good["content_digest"] = desc.compute_content_digest(good, self.root)
        self.assertEqual(desc.validate_descriptor(good, self.root), good)

    def test_hash_authenticates_code_schemas_and_descriptor_independently(self):
        valid = desc.validate_descriptor(self.item, self.root)
        self.assertTrue(desc.trusted_code(valid,self.root).startswith(b"# Reviewed fixture code"))
        self.rejects({**valid, "version":"2.0.0"}, "plugin.digest_mismatch")
        self.rejects({**valid, "entrypoint":"different:apply"}, "plugin.permission_denied")
        catalog_path = self.root / "catalog.json"
        catalog = json.loads(catalog_path.read_bytes())
        record = next(iter(catalog["implementations"].values()))[0]
        schema = self.root / "input.json"
        prior = schema.read_bytes()
        schema.write_bytes(b'{"type":"string"}')
        self.rejects(valid, "plugin.digest_mismatch")
        schema.write_bytes(prior)
        artifact = self.root / record["artifact"]
        artifact.write_bytes(b"changed after validation")
        with self.assertRaisesRegex(ValueError,"plugin.digest_mismatch"):
            desc.trusted_code(valid,self.root)
        record["artifact"] = "../outside"
        catalog_path.write_text(json.dumps(catalog),encoding="utf-8")
        self.rejects(valid)

    def test_pinned_physical_contract_and_process_soft(self):
        process = descriptor_fixture(self.root, plugin_id="camera.probe", runtime="process", track="physical")
        desc.validate_descriptor(process, self.root)
        for key in desc.PHYSICAL:
            with self.subTest(missing=key):
                self.rejects({k:v for k,v in process.items() if k!=key}, "plugin.physical_contract_missing")
        self.rejects({**process, "hot_reload":True}, "plugin.physical_hot_reload_forbidden")
        self.rejects({**process, "verifier":"camera.probe"}, "plugin.verifier_invalid")
        self.rejects({**process, "runtime":"inproc"}, "plugin.class_violation")
        process_soft = descriptor_fixture(self.root, plugin_id="read.log", runtime="process")
        self.assertEqual(desc.validate_descriptor(process_soft,self.root),process_soft)
        with self.assertRaisesRegex(ValueError,"plugin.class_violation"):
            desc.trusted_code(process_soft,self.root)

    def test_file_parser_truncated_duplicate_and_bounds(self):
        path = self.root / "test.plugin.json"
        for raw in (b'{"id":"a","id":"b"}', b"{}" + b" " * desc.MAX_JSON, b'{"a":1', b'{"a":NaN}'):
            path.write_bytes(raw)
            with self.subTest(prefix=raw[:20]), self.assertRaises(desc.DescriptorError):
                desc.load_descriptor(path,self.root)
        path.write_bytes(json.dumps(dict(reversed(list(self.item.items())))).encode())
        self.assertEqual(desc.load_descriptor(path,self.root), self.item)

    def test_config_nested_types_bounds_enums_and_closed_objects(self):
        schema = {"type":"object", "required":["items"],"additionalProperties":False, "properties":{
            "items":{"type":"array","maxItems":1,"minItems":1,"items":{"type":"integer","minimum":1,"maximum":5}},
            "mode":{"type":"string","enum":["read","scan"], "maxLength":4}}}
        self.assertIsNone(desc.validate_instance(schema, {"items":[1],"mode":"scan"}))
        for bad in ({}, {"items":[]}, {"items":[8]}, {"items":[True]}, {"items":[1,2]},
                    {"items":[2],"extra":1}, {"items":[1],"mode":"move"}):
            with self.subTest(value=bad), self.assertRaisesRegex(ValueError,"plugin.config_invalid"):
                desc.validate_instance(schema,bad)
        with self.assertRaisesRegex(ValueError,"plugin.config_invalid"):
            desc.validate_instance({"type":"boolean","enum":[0]}, False)


class PilotContracts(unittest.TestCase):
    def test_offline_site_and_real_callers_evidence_schema(self):
        from types import SimpleNamespace
        from evidence.query import schema
        from scripts.prepare_soft_pilots import DEFAULT_SERIES, prepare_site
        from soft_series_plugin import apply

        manifest = json.dumps({"schemaVersion":2, "config":{"digest":"sha256:" + "a"*64},"layers":[]}).encode()
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "site"
            names = prepare_site(site,manifest,"b"*40,DEFAULT_SERIES)
            self.assertEqual(sorted(names), ["derived.series_mean", "evidence.evidence_schema", "evidence.query_evidence",
                                            "evidence.read_session", "session.default"])
            with self.assertRaisesRegex(ValueError,"existing trust root"):
                prepare_site(site,manifest,"b"*40,DEFAULT_SERIES)
            trust = site / "trust"

            def pilot(name):
                return desc.load_descriptor(site / "releases" / (name + ".plugin.json"),trust)

            tool = pilot(DEFAULT_SERIES["id"])
            profile = tool["config_schema"]["properties"]["profile"]["const"]
            handlers = {}
            ctx = SimpleNamespace(config=lambda:{"profile":profile,"settings":{}},
                                  tools=SimpleNamespace(register=lambda name,handler: handlers.update({name:handler})))
            apply(ctx)
            payload = {"operation_id":"fixed","version":profile["legacy"]["version"],"arguments":{"series":"[2,4,6]"}}
            reply = handlers[tool["id"]](payload)
            self.assertEqual(reply["operation_id"], "fixed")
            self.assertEqual(reply["evidence"]["value"], 4)
            desc.validate_instance(tool["outcomes"], reply["evidence"])
            self.assertEqual(desc.trusted_code(tool,trust), (ROOT/"agent"/"soft_series_plugin.py").read_bytes())
            evidence = pilot("evidence.evidence_schema")
            expected = evidence["config_schema"]["properties"]["profile"]["const"]["health_expected"]
            self.assertEqual({k:schema()[k] for k in expected},expected)
            desc.validate_instance(evidence["outcomes"], {**schema(),"query_id":"fixed"})


if __name__ == "__main__":
    unittest.main()
