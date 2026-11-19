"""SDD-045. Offline release identity and typed contracts. Never import candidate code."""
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath
import re
import stat


MAX_JSON = 128 * 1024
NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,126}@[0-9A-Za-z_.-]{1,32}")
SHA = re.compile(r"sha256:[0-9a-f]{64}")
GIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
NUM = r"(?:0|[1-9][0-9]*)"
PRE = rf"(?:{NUM}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER = re.compile(rf"{NUM}\.{NUM}\.{NUM}(?:-{PRE}(?:\.{PRE})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
FIELDS = set("""schema_version id version content_digest provenance kind class runtime provides requires
parameters outcomes config_schema effects hot_reload drain_required device_lease stop_protocol verifier
operation_ledger entrypoint mounts network resources health policy""".split())
PHYSICAL = ("device_lease", "stop_protocol", "verifier", "operation_ledger")


class DescriptorError(ValueError):
    def __init__(self, code, field):
        self.code, self.field = code, field
        super().__init__(f"{code}: {field}")


def _check(condition, field, code="plugin.manifest_invalid"):
    if not condition:
        raise DescriptorError(code, field)


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _keys(obj, required, field, optional=()):
    _check(isinstance(obj, dict) and set(required) <= obj.keys() <= set(required) | set(optional), field)


def _strings(values, field, pattern=NAME):
    _check(isinstance(values, list) and all(_matches(pattern, v) for v in values)
           and len(values) == len(set(values)), field)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _check(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _read(path):
    try:
        _check(stat.S_ISREG(path.lstat().st_mode), "expected regular JSON file")
        with path.open("rb") as stream:
            raw = stream.read(MAX_JSON + 1)
        _check(len(raw) <= MAX_JSON, "JSON byte limit")
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        canonical(obj)  # Reject non-finite numbers, unsupported integers and broken Unicode.
        return obj
    except DescriptorError:
        raise
    except (OSError, ValueError, RecursionError, OverflowError) as exc:
        raise DescriptorError("plugin.manifest_invalid", "invalid JSON or inaccessible file") from exc


def canonical(value):
    """RFC 8785 for the I-JSON domain. Preserve UTF-16 key order and ECMAScript number spelling."""
    if isinstance(value, dict):
        names = sorted(value, key=lambda k: k.encode("utf-16-be"))
        return b"{" + b",".join(canonical(k) + b":" + canonical(value[k]) for k in names) + b"}"
    if isinstance(value, list):
        return b"[" + b",".join(canonical(v) for v in value) + b"]"
    if type(value) is int:
        _check(abs(value) <= (1 << 53) - 1, "integer outside interoperable range")
    if type(value) is float:
        _check(math.isfinite(value), "non-finite number")
        if value == 0:
            return b"0"
        digits = Decimal(repr(value))
        if 1e-6 <= abs(value) < 1e21:
            number = format(digits, "f")
            if "." in number:
                number = number.rstrip("0").rstrip(".")
        else:
            coefficient, exponent = format(digits.normalize(), "e").split("e")
            number = coefficient + "e" + format(int(exponent), "+d")
        return number.encode("ascii")
    _check(value is None or type(value) in (int, bool, str), "expected a JSON value")
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _asset(root, relative):
    _check(isinstance(relative, str) and relative and not PureWindowsPath(relative).drive
           and "\\" not in relative and not relative.startswith("/") and
           all(part not in ("", ".", "..") for part in relative.split("/")), "catalog asset path")
    root = Path(root).resolve(strict=True)
    candidate = root / relative
    _check(candidate.resolve(strict=True) == candidate and stat.S_ISREG(candidate.lstat().st_mode),
           "catalog asset must be a regular file under trust root")
    return candidate


SCHEMA_TYPES = {
    "object": (dict,), "array": (list,), "string": (str,), "number": (float, int),
    "integer": (int,), "boolean": (bool,), "null": (type(None),),
}
KEYWORDS = {
    "object": ("properties", "required", "additionalProperties"),
    "array": ("items", "minItems", "maxItems"), "string": ("minLength", "maxLength"),
    "number": ("minimum", "maximum"), "integer": ("minimum", "maximum"),
}


def _validate_schema(schema, field="schema"):
    _check(isinstance(schema, dict) and isinstance(schema.get("type"), str)
           and schema["type"] in SCHEMA_TYPES, field + ".type")
    kind = schema["type"]
    _keys(schema, {"type"}, field, ("title", "description", "unit", "enum", "const", *KEYWORDS.get(kind, ())))
    for key in ("title", "description", "unit"):
        _check(key not in schema or isinstance(schema[key], str), field + "." + key)
    if "enum" in schema:
        values = schema["enum"]
        _check(isinstance(values, list) and values and len({canonical(v) for v in values}) == len(values), field + ".enum")
    if kind == "object":
        props = schema.get("properties", {})
        required = schema.get("required", [])
        _check(isinstance(props, dict) and type(schema.get("additionalProperties", True)) is bool, field)
        _check(isinstance(required, list) and all(isinstance(k, str) and k in props for k in required)
               and len(set(required)) == len(required), field + ".required")
        for key, child in props.items():
            _validate_schema(child, field + ".properties." + key)
    if kind == "array" and "items" in schema:
        _validate_schema(schema["items"], field + ".items")
    for lower, upper in (("minimum", "maximum"), ("minLength", "maxLength"), ("minItems", "maxItems")):
        for key in (lower, upper):
            if key in schema:
                val = schema[key]
                ok = type(val) in (int, float) and math.isfinite(val)
                if key not in ("minimum", "maximum"):
                    ok = type(val) is int and val >= 0
                _check(ok, field + "." + key)
        if lower in schema and upper in schema:
            _check(schema[lower] <= schema[upper], field + "." + upper)
    return schema


def validate_instance(schema, value):
    """Check the supported schema vocabulary strictly, including nested values and boolean types."""
    _validate_schema(schema)

    def visit(rule, actual, where):
        def require(ok):
            _check(ok, where, "plugin.config_invalid")

        kind = rule["type"]
        require(type(actual) in SCHEMA_TYPES[kind])
        if "enum" in rule:
            require(any(canonical(actual) == canonical(v) for v in rule["enum"]))
        if "const" in rule:
            require(canonical(actual) == canonical(rule["const"]))
        if kind == "object":
            props = rule.get("properties", {})
            require(set(rule.get("required", [])) <= actual.keys())
            require(rule.get("additionalProperties", True) or actual.keys() <= props.keys())
            for key in actual.keys() & props.keys():
                visit(props[key], actual[key], where + "." + key)
        if kind == "array" and "items" in rule:
            for index, item in enumerate(actual):
                visit(rule["items"], item, f"{where}[{index}]")
        if kind in ("string", "array"):
            suffix = "Length" if kind == "string" else "Items"
            require(rule.get("min" + suffix, 0) <= len(actual) <= rule.get("max" + suffix, math.inf))
        if kind in ("integer", "number"):
            require(math.isfinite(actual) and rule.get("minimum", -math.inf) <= actual <= rule.get("maximum", math.inf))

    visit(schema, value, "value")


def _catalog(root):
    catalog = _read(_asset(root, "catalog.json"))
    _keys(catalog, {"implementations", "schemas"}, "site trust catalog")
    _check(all(isinstance(v, dict) for v in catalog.values()), "site trust catalog")
    return catalog


def _schema_ref(ref, root, catalog):
    _check(_matches(REF, ref) and ref in catalog["schemas"], "schema_ref")
    return _validate_schema(_read(_asset(root, catalog["schemas"][ref])), ref)


def resolve_schema(schema, trust_root):
    if isinstance(schema, dict) and set(schema) == {"schema_ref"}:
        return _schema_ref(schema["schema_ref"], trust_root, _catalog(trust_root))
    return _validate_schema(schema)


def _permissions(item):
    network, mounts = item["network"], item["mounts"]
    _keys(network, {"ingress", "egress"}, "network")
    for direction in network:
        _strings(network[direction], "network." + direction)
    _check(isinstance(mounts, list), "mounts")
    for mount in mounts:
        _keys(mount, {"source", "target", "read_only"}, "mounts")
        _check(all(isinstance(mount[k], str) and mount[k] and "\x00" not in mount[k]
                   for k in ("source", "target")) and type(mount["read_only"]) is bool, "mounts")
        if item["class"] == "soft":
            # P1 supports a mount-free soft process ABI; host mounts cannot be authenticated by a manifest.
            raise DescriptorError("plugin.class_violation", "soft.mounts")
    resources = item["resources"]
    _keys(resources, {"memory_mib", "cpu", "max_inflight", "shutdown_ms"}, "resources")
    for k, value in resources.items():
        _check(type(value) in ((float, int) if k == "cpu" else (int,)) and value > 0 and math.isfinite(value),
               "resources." + k)
    health = item["health"]
    _keys(health, {"probe", "timeout_ms", "ttl_ms"}, "health")
    _check(_matches(NAME, health["probe"]) and type(health["timeout_ms"]) is int
           and type(health["ttl_ms"]) is int and 0 < health["timeout_ms"] <= health["ttl_ms"], "health")
    policy = item["policy"]
    _keys(policy, {"install", "enable", "site_approval"}, "policy")
    for k in ("install", "enable"):
        _strings(policy[k], "policy." + k)
        _check(bool(policy[k]), "policy." + k)
    _check(type(policy["site_approval"]) is bool, "policy.site_approval")


def _descriptor(item):
    _keys(item, FIELDS - set(PHYSICAL), "descriptor fields", PHYSICAL)
    _check(type(item["schema_version"]) is int and item["schema_version"] == 2, "schema_version")
    _check(_matches(NAME, item["id"]), "id")
    _check(_matches(SEMVER, item["version"]), "version")
    for k, choices in (("kind", ("tool","service","consumer","view")), ("effects", ("read","idempotent","actuation")),
                       ("class", ("soft","physical")), ("runtime", ("inproc","process"))):
        _check(item[k] in choices, k)
    _check(type(item["hot_reload"]) is bool and item["drain_required"] is True, "hot_reload/drain_required")
    if item["class"] == "physical":
        _check(item["runtime"] == "process", "runtime", "plugin.class_violation")
        _check(not item["hot_reload"], "hot_reload", "plugin.physical_hot_reload_forbidden")
        for field in PHYSICAL:
            _check(_matches(NAME, item.get(field)), field, "plugin.physical_contract_missing")
        _check(item["verifier"] != item["id"], "verifier", "plugin.verifier_invalid")
    else:
        _check(item["effects"] != "actuation", "effects", "plugin.class_violation")
        _check(all(k in item for k in PHYSICAL), "physical contract fields")
        _check(all(item[k] is None for k in ("device_lease", "stop_protocol", "operation_ledger")),
               "soft device contracts", "plugin.class_violation")
        _check(item["verifier"] is None or (_matches(NAME, item["verifier"]) and item["verifier"] != item["id"]),
               "verifier", "plugin.verifier_invalid")
    refs = set()
    exported = set()
    for field in ("provides", "requires"):
        rows = item[field]
        _check(isinstance(rows, list), field)
        names = set()
        for row in rows:
            _keys(row, {"name", "type", "schema_ref"}, field)
            _check(_matches(NAME, row["name"]) and row["name"] not in names and
                   row["type"] in ("service", "tool", "view", "event") and _matches(REF, row["schema_ref"]), field)
            names.add(row["name"])
            refs.add(row["schema_ref"])
        if field == "provides":
            exported = names
        else:
            _check(not names & exported, "requires", "plugin.dependency_cycle")
    _check(item["verifier"] not in exported, "verifier", "plugin.verifier_invalid")
    for key in ("parameters", "outcomes", "config_schema"):
        schema = item[key]
        if isinstance(schema, dict) and set(schema) == {"schema_ref"}:
            _check(_matches(REF, schema["schema_ref"]), key)
            refs.add(schema["schema_ref"])
        else:
            _validate_schema(schema, key)
    p = item["provenance"]
    _keys(p, {"author","source_commit","image_digest"}, "provenance",
          ("code_digest",) if item["runtime"] == "inproc" else ())
    _check(isinstance(p["author"], str) and 0 < len(p["author"].strip()) <= 160, "provenance.author")
    _check(_matches(GIT,p["source_commit"]) and _matches(SHA,p["image_digest"]), "provenance")
    entry = item["entrypoint"]
    if item["runtime"] == "inproc":
        _check(_matches(SHA, p.get("code_digest")), "provenance.code_digest")
        _check(_matches(re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:apply", re.ASCII), entry), "entrypoint")
    else:
        _check(isinstance(entry, list) and entry and all(isinstance(s,str) and s and "\x00" not in s for s in entry),
               "entrypoint")
    _permissions(item)
    return refs


def _record(item, catalog):
    p = item["provenance"]
    impl = p.get("code_digest", p["image_digest"])
    records = catalog["implementations"].get(impl, [])
    if isinstance(records, dict):
        records = [records]
    _check(isinstance(records, list), "site implementations")
    matches = [r for r in records if isinstance(r, dict) and r.get("id") == item["id"]]
    _check(len(matches) == 1, "site identity", "plugin.permission_denied")
    record = matches[0]
    expected = {"source_commit": p["source_commit"], "image_digest": p["image_digest"],
                "entrypoint": item["entrypoint"], "class": item["class"],
                "kind": "code" if item["runtime"] == "inproc" else "image"}
    _check(all(record.get(k) == v for k,v in expected.items()), "site identity", "plugin.permission_denied")
    _check(record.get("mounts", []) == item["mounts"] and
           record.get("network", {"ingress":[], "egress":[]}) == item["network"], "site permissions", "plugin.permission_denied")
    return impl, record


def compute_content_digest(item, trust_root):
    """Build and loader share this check. Image artifacts are platform manifest bytes; code artifacts are package bytes."""
    try:
        refs = _descriptor(item)
        catalog = _catalog(trust_root)
        impl, record = _record(item, catalog)
        digest = hashlib.sha256()
        with _asset(trust_root, record.get("artifact")).open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                digest.update(block)
        _check("sha256:" + digest.hexdigest() == impl, "implementation", "plugin.digest_mismatch")
        schemas = {ref: _sha(canonical(_schema_ref(ref, trust_root, catalog))) for ref in refs}
        raw = {k: v for k,v in item.items() if k != "content_digest"}
        return _sha(canonical({"descriptor":raw, "implementation":impl, "schemas":schemas}))
    except DescriptorError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, OverflowError) as error:
        raise DescriptorError("plugin.manifest_invalid", "invalid descriptor or missing site assets") from error


def validate_descriptor(item, trust_root):
    actual = compute_content_digest(item, trust_root)
    _check(_matches(SHA, item["content_digest"]), "content_digest")
    _check(item["content_digest"] == actual, "content_digest", "plugin.digest_mismatch")
    return item


def load_descriptor(path, trust_root):
    return validate_descriptor(_read(Path(path)), trust_root)


def trusted_code(item, trust_root):
    """Return immutable reviewed source bytes to the isolated soft host; never reopen by name for execution."""
    validate_descriptor(item, trust_root)
    _check(item["class"] == "soft" and item["runtime"] == "inproc", "runtime", "plugin.class_violation")
    try:
        digest, record = _record(item, _catalog(trust_root))
        limit = 1024 * 1024
        with _asset(trust_root, record.get("artifact")).open("rb") as stream:
            source = stream.read(limit + 1)
        _check(len(source) <= limit, "inproc code byte limit")
        _check(_sha(source) == digest, "executable code", "plugin.digest_mismatch")
        return source
    except OSError as exc:
        raise DescriptorError("plugin.manifest_invalid", "unavailable code artifact") from exc


def _legacy(row, kind, binding, software=False):
    return {"id":row["id"], "version":row.get("version"), "kind":kind, "class":"soft" if software else "physical",
            "content_digest":None, "binding":binding, "legacy":True, "health":"unknown",
            "availability":row.get("status", "unknown"), "lifecycle":"legacy"}


def normalize_legacy_tool(cap):
    """Use the already resolved v1 registry snapshot. Never reinterpret an old plan or grant installation rights."""
    from planning import binding
    software = cap.get("derived", {}).get("schema_version") == 2 and "endpoint" not in cap
    return _legacy(cap, "tool", binding(cap), software)


def normalize_legacy_service(public_row):
    return _legacy(public_row, "service", public_row.get("binding"))
