"""SDD-006. Discover installed services and verify a candidate receipt without actuating."""
from collections import defaultdict
from concurrent.futures import Future
from graphlib import CycleError, TopologicalSorter
import hashlib
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import threading
import time
from urllib.parse import urlsplit

from module_journal import ModuleJournal, canonical
from tracing import encoded, exchange, sanitized


NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
MAX_CONFIG_BYTES = 131072
MAX_REPLY_BYTES = 65536
VERIFY_TIMEOUT = 2
_NETWORK_SLOTS = threading.BoundedSemaphore(4)


class PluginUnavailable(Exception):
    origin = "runtime.plugins"

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


class PolicyError(ValueError):
    code = "verification.policy_invalid"
    origin = "runtime.plugins"


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
        self.owners = defaultdict(list)
        self.invalid = []
        self.problems = {}
        self.bindings = {}
        self.draining = {}
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
            for service in item["provides"]:
                self.owners[service].append(item["id"])
        self._resolve()

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


def snapshot(observing=False):
    return ServiceSnapshot(Path(os.getenv("EDGE_PLUGIN_DIR", "/etc/edge/plugins")) / "runtime", observing)


class RegistryObserver:
    """Serialize sampling with both durable state and evidence; absence of a mount is ambiguous."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.journal = None
        self.warning = None
        self.next_pulse = 0

    def _open(self):
        if self.journal is None:
            journal = ModuleJournal(self.path)
            try:
                journal.db.execute("""CREATE TABLE IF NOT EXISTS registry_projection(
                    plugin TEXT PRIMARY KEY, state TEXT NOT NULL, event_id TEXT NOT NULL)""")
            except BaseException:
                journal.close()
                raise
            self.journal = journal
        return self.journal

    def _health(self, journal, error):
        row = journal.db.execute("SELECT value FROM module_bus_meta WHERE key='registry_health'").fetchone()
        previous, parent = json.loads(row[0]) if row else (None, None)
        if error == previous:
            return
        code = "plugin.registry_observation_failed" if error else "plugin.registry_observation_restored"
        eid = journal.emit({"kind":code, "code":code, "module":"runtime.registry", "layer":"registry",
                            "trace_id":"registry-health."+journal.source_id, "parent_event_id":parent,
                            "level":"warn" if error else "info", "data":{"reason":error, "previous_reason":previous}})
        journal.db.execute("INSERT OR REPLACE INTO module_bus_meta VALUES('registry_health',?)",
                           (canonical([error, eid]),))

    def _record(self, journal, catalog, baseline):
        db = journal.db
        prior = {key:(json.loads(state), eid) for key,state,eid in
                 db.execute("SELECT plugin,state,event_id FROM registry_projection")}
        current = {}
        for row in catalog["plugins"]:
            key = row["id"]
            if not NAME.fullmatch(key):
                key = "invalid-file:" + hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
            current[key] = {field:row.get(field) for field in ("status", "version", "binding", "code")}
        for key in sorted(current.keys() | prior.keys()):
            before, parent = prior.get(key, (None, None))
            after = current.get(key)
            if after == before:
                continue
            eid = journal.emit({"module":"runtime.registry", "layer":"registry", "kind":"plugin.registry_observed",
                                "code":"plugin.registry_observed", "parent_event_id":parent,
                                "trace_id":f"registry.{journal.source_id}.{key}", "level":
                                "warn" if after and after["status"] in ("blocked","invalid") else "info",
                                "data":{"plugin_id":key, "before":before, "after":after, "baseline":baseline}})
            db.execute("INSERT OR REPLACE INTO registry_projection VALUES(?,?,?)", (key, canonical(after), eid))

    def _sample(self, background):
        journal = self._open()
        try:
            snap = snapshot(observing=True)
        except OSError as exc:
            with journal.db:
                self._health(journal, type(exc).__name__)
            raise
        catalog = snap.public()
        with journal.db:
            baseline = journal.db.execute("SELECT 1 FROM module_bus_meta WHERE key='registry_sampled'").fetchone() is None
            unavailable = not snap.directory_present and not baseline
            self._health(journal, "DirectoryMissing" if unavailable else None)
            if snap.directory_present:
                self._record(journal, catalog, baseline)
                if baseline:
                    journal.db.execute("INSERT INTO module_bus_meta VALUES('registry_sampled','1')")
            pulse = background and time.monotonic() >= self.next_pulse
            if pulse:
                journal.heartbeat(30)
        if pulse:
            self.next_pulse = time.monotonic() + 10
        if unavailable:
            raise FileNotFoundError("previously observed runtime directory unavailable")
        return catalog

    def observe(self, background=False):
        with self.lock:
            try:
                catalog = self._sample(background)
                self.warning = None
                return catalog
            except (OSError, sqlite3.Error, ValueError) as error:
                code = type(error).__name__
                if self.warning != code:
                    print(f"runtime.registry observation unavailable ({code})", flush=True)
                self.warning = code
                raise PluginUnavailable("plugin.registry_observation_unavailable",
                                        "Runtime registry evidence unavailable: " + code) from error

    def run(self, stop):
        while not stop.is_set():
            try:
                self.observe(background=True)
            except PluginUnavailable:
                pass  # The next sample retries. The task worker has a separate execution path.
            stop.wait(1)


def load_policy():
    path = Path(os.getenv("EDGE_CONTRACT_DIR", "/etc/edge/contracts")) / "verification-policy.json"
    try:
        try:
            path.lstat()
        except FileNotFoundError:
            return {"schema_version":1, "tools":{}, "outcomes":{}}
        policy = _read(path)
        _schema(policy, {"schema_version","tools","outcomes"})
        for group in ("tools", "outcomes"):
            mapping = policy[group]
            if not isinstance(mapping, dict) or not all(NAME.fullmatch(key) for key in mapping):
                raise ValueError("expected a map keyed by capability or outcome name")
            policy[group] = {key:_names(value) for key, value in mapping.items()}
        return policy
    except (OSError, ValueError, RecursionError) as error:
        raise PolicyError("invalid verification-policy.json; restore a valid policy") from error


def requirements(cap, policy):
    services = set(policy["tools"].get(cap["id"], []))
    for outcome in cap.get("outcomes", {}):
        if outcome == "image":
            services.add("verify.image")
        services.update(policy["outcomes"].get(outcome, []))
    return sorted(services)


def _post(endpoint, request):
    target = urlsplit(endpoint)
    cls = HTTPSConnection if target.scheme == "https" else HTTPConnection
    conn = cls(target.hostname, target.port, timeout=VERIFY_TIMEOUT)
    # A socket inactivity timeout alone doesn't cover slow DNS or trickling HTTP headers/body.
    # Capture the connected socket before http.client transfers it to the response reader.
    # Bounded workers limit the number of stalled OS resolutions left after a deadline.
    if not _NETWORK_SLOTS.acquire(blocking=False):
        raise PluginUnavailable("plugin.transport", "verification transport is busy")
    abandoned = threading.Event()
    sockets = []
    result = Future()

    def send():
        try:
            conn.connect()
            sockets.append(conn.sock)
            if not abandoned.is_set():
                result.set_result(_read_reply(conn, target.path, request))
        except Exception as exc:
            result.set_exception(exc)
        finally:
            try:
                conn.close()
            finally:
                _NETWORK_SLOTS.release()

    worker = threading.Thread(target=send, daemon=True, name="verifier-http")
    try:
        worker.start()
    except RuntimeError as exc:
        _NETWORK_SLOTS.release()
        raise PluginUnavailable("plugin.transport", "could not start verification transport") from exc
    try:
        return result.result(timeout=VERIFY_TIMEOUT)
    except TimeoutError as error:
        abandoned.set()
        if sockets:
            try:
                sockets[0].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        raise PluginUnavailable("plugin.transport", "verification transport timed out") from error
    except (OSError, HTTPException) as error:
        raise PluginUnavailable("plugin.transport", f"verification transport unavailable ({type(error).__name__})") from error
    except (ValueError, RecursionError) as error:
        raise PluginUnavailable("plugin.receipt_invalid", "invalid verifier JSON or receipt size") from error


def _read_reply(conn, path, request):
    conn.request("POST", path or "/", body=encoded(request), headers={"Content-Type":"application/json"})
    response = conn.getresponse()
    with response:
        if response.status != 200:
            raise PluginUnavailable("plugin.transport", f"verification endpoint returned HTTP {response.status}")
        declared = response.getheader("Content-Length")
        if declared is not None and not 0 <= int(declared) <= MAX_REPLY_BYTES:
            raise ValueError("invalid receipt size")
        raw = response.read(MAX_REPLY_BYTES + 1)
        if len(raw) > MAX_REPLY_BYTES or declared is not None and int(declared) != len(raw):
            raise ValueError("invalid receipt size")
        return _decode(raw)


def _check_evidence(value, depth=0):
    if depth > 24:
        raise ValueError("evidence nesting limit")
    if isinstance(value, list):
        for item in value:
            _check_evidence(item, depth + 1)
    elif isinstance(value, dict):
        for item in value.values():
            _check_evidence(item, depth + 1)


def verify(pin, request, trace=None):
    """Return a verified pass/fail reply. Unknown or unusable replies require reconciliation."""
    identity = {key: pin[key] for key in ("service", "plugin_id", "plugin_version")}
    body = {**request, **identity, "dependencies":pin["dependencies"]}
    identity["operation_id"] = body["operation_id"]

    def invoke():
        reply = _post(pin["endpoint"], body)
        if (not isinstance(reply, dict) or set(reply) != set(identity) | {"verdict","evidence"}
                or any(reply[key] != value for key, value in identity.items())
                or reply["verdict"] not in ("pass", "fail", "unknown")
                or not isinstance(reply["evidence"], dict) or not reply["evidence"]):
            raise PluginUnavailable("plugin.receipt_invalid", "verifier receipt has invalid identity, verdict or evidence")
        try:
            _check_evidence(reply["evidence"])
            reply["evidence"] = sanitized(reply["evidence"])
            # Reject unencodable unicode and excessive nesting before this result enters the ledger.
            encoded(reply["evidence"])
        except (RecursionError, ValueError) as error:
            raise PluginUnavailable("plugin.receipt_invalid", "invalid evidence content") from error
        if reply["verdict"] == "unknown":
            raise PluginUnavailable("plugin.verdict_unknown", "verifier could not establish the result", reply["evidence"])
        return reply

    return exchange(trace, "verification", pin["plugin_id"], body, invoke,
                    meta={"service":pin["service"], "version":pin["plugin_version"], "binding":pin["binding"]})
