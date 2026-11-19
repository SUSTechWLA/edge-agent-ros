"""Read-only v1 service discovery and binding resolution."""
from collections import defaultdict
from graphlib import CycleError, TopologicalSorter
import hashlib
import json
import math
import re
import stat
from urllib.parse import urlsplit

from .trace_content import encoded


NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
MAX_CONFIG_BYTES = 131072


class PluginUnavailable(Exception):
    origin = "runtime.plugins"

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _number(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _decode(raw):
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                      parse_constant=_number, parse_float=_number)


def _read(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("expected a regular configuration file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("configuration exceeds size limit")
    return _decode(raw)


def _names(values):
    if not isinstance(values, list) or not all(isinstance(v, str) and NAME.fullmatch(v) for v in values):
        raise ValueError("expected a list of service names")
    if len(values) != len(set(values)):
        raise ValueError("duplicate service name")
    return sorted(values)


def _schema(obj, keys, optional=()):
    if (not isinstance(obj, dict) or set(obj).difference(optional) != keys or type(obj["schema_version"]) is not int
            or obj["schema_version"] != 1):
        raise ValueError("invalid v1 configuration fields")


def _descriptor(path):
    item = _read(path)
    _schema(item, {"schema_version","id","version","endpoint","provides","requires"}, optional=("draining",))
    if "draining" in item and type(item["draining"]) is not bool:
        raise ValueError("draining must be boolean")
    if not (isinstance(item["id"], str) and NAME.fullmatch(item["id"]) and path.stem == item["id"]):
        raise ValueError("invalid id or mismatched filename")
    if not isinstance(item["version"], str) or not VERSION.fullmatch(item["version"]):
        raise ValueError("invalid plugin version")
    url = item["endpoint"]
    if not isinstance(url, str) or not url.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
        raise ValueError("invalid endpoint")
    target = urlsplit(url)
    if (target.scheme not in ("http", "https") or not target.hostname or target.port == 0
            or target.username is not None or target.password is not None or any(c in url for c in "?#\\")):
        raise ValueError("expected an HTTP endpoint without credentials, query or fragment")
    item["provides"] = _names(item["provides"])
    item["requires"] = _names(item["requires"])
    if not item["provides"]:
        raise ValueError("expected at least one provided service")
    return item


class ServiceSnapshot:
    """Dependency bindings are computed once from these descriptors. No HTTP or process management."""

    def __init__(self, directory, observing=False):
        self.entries = {}
        self.invalid = []
        self.directory_present = True
        if observing:
            # glob may silently turn a permission error into an empty directory.
            try:
                paths = sorted(p for p in directory.iterdir() if p.suffix == ".json")
            except FileNotFoundError:
                self.directory_present = False
                paths = []
        else:
            paths = sorted(directory.glob("*.json"))
        for path in paths:
            if path.name.startswith("."):
                continue
            try:
                item = _descriptor(path)
            except (OSError, ValueError, RecursionError) as error:
                if observing and isinstance(error, OSError):
                    raise
                # Do not disclose parts of a malformed or credential-bearing descriptor in the catalog.
                self.invalid.append({"id":path.stem, "status":"invalid", "code":"plugin.manifest_invalid",
                                     "reason":"invalid runtime descriptor: " + type(error).__name__})
                continue
            self.entries[item["id"]] = item
        self._index()

    def _index(self):
        self.owners = defaultdict(list)
        self.problems = {}
        self.bindings = {}
        self.draining = {}
        for key,item in self.entries.items():
            for service in item["provides"]:
                self.owners[service].append(key)
        self._resolve()

    def with_change(self, name, descriptor):
        """Resolve a prospective update against the same observed catalog, without changing files."""
        changed = object.__new__(type(self))
        changed.entries = {key:item for key,item in self.entries.items() if key != name}
        if descriptor is not None:
            changed.entries[name] = descriptor
        changed.invalid = [row for row in self.invalid if row["id"] != name]
        changed.directory_present = self.directory_present
        changed._index()
        return changed

    def _resolve(self):
        graph = TopologicalSorter()
        for key, item in self.entries.items():
            parents = []
            required = set(item["requires"])
            for service in item["provides"] + item["requires"]:
                owners = self.owners[service]
                if len(owners) != 1:
                    code = "plugin.service_conflict" if owners else "plugin.service_missing"
                    self.problems[key] = (code, f"{key}: {service} has {len(owners)} providers")
                elif service in required:
                    parents.append(owners[0])
            graph.add(key, *parents)
        try:
            graph.prepare()
        except CycleError:
            pass  # get_ready() still yields the independent, acyclic portion.
        while graph.is_active():
            ready = graph.get_ready()
            for key in ready:
                if key in self.problems:
                    continue
                dependencies = {}
                item = self.entries[key]
                drain_source = key if item.get("draining") else None
                for service in item["requires"]:
                    owner = self.owners[service][0]
                    if owner in self.problems:
                        self.problems[key] = ("plugin.dependency_unavailable", f"{key} needs blocked {owner} ({service})")
                        break
                    dependencies[service] = self.bindings[owner]
                    drain_source = drain_source or self.draining.get(owner)
                else:
                    # Admission controls change independently of implementation identity.
                    identity = {field:value for field, value in item.items() if field != "draining"}
                    self.bindings[key] = hashlib.sha256(encoded([identity, dependencies])).hexdigest()
                    if drain_source:
                        self.draining[key] = drain_source
            graph.done(*ready)
        for key in self.entries.keys() - self.bindings.keys() - self.problems.keys():
            self.problems[key] = ("plugin.dependency_cycle", f"{key} belongs to or depends on a dependency cycle")

    def pin(self, service, expected_binding=None):
        owners = self.owners.get(service, [])
        if len(owners) != 1:
            code = "plugin.service_conflict" if owners else "plugin.service_missing"
            raise PluginUnavailable(code, f"{service} has {len(owners)} installed providers")
        key = owners[0]
        if key in self.problems:
            raise PluginUnavailable(*self.problems[key])
        if expected_binding is not None and expected_binding != self.bindings[key]:
            raise PluginUnavailable("verification.binding_unavailable", f"installed binding for {service} differs")
        if expected_binding is None and key in self.draining:
            raise PluginUnavailable("plugin.draining", f"{self.draining[key]} is draining; {service} is closed to new work")
        return {**self._address(service, key),
                "dependencies":{required:self._address(required, self.owners[required][0])
                                for required in self.entries[key]["requires"]}}

    def _address(self, service, key):
        item = self.entries[key]
        return {"service":service, "plugin_id":key, "plugin_version":item["version"], "endpoint":item["endpoint"],
                "binding":self.bindings[key]}

    def public(self):
        rows = list(self.invalid)
        for key, item in self.entries.items():
            if key in self.problems:
                code, reason = self.problems[key]
                rows.append({**item, "status":"blocked", "code":code, "reason":reason})
            elif key in self.draining:
                rows.append({**item, "status":"draining", "code":"plugin.draining", "binding":self.bindings[key],
                             "reason":f"{self.draining[key]} is draining"})
            else:
                rows.append({**item, "status":"ready", "binding":self.bindings[key]})
        return {"plugins": sorted(rows, key=lambda row:row["id"])}
