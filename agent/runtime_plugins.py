"""SDD-006. Discover installed services and verify a candidate receipt without actuating."""
from concurrent.futures import Future
import hashlib
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time
from urllib.parse import urlsplit

if not __package__:
    import _contract_path  # Support direct script entry points.
from edge_contracts.service_directory import (MAX_CONFIG_BYTES, NAME, VERSION, PluginUnavailable,
    ServiceSnapshot, _decode, _descriptor, _names, _read, _schema)
from module_journal import ModuleJournal, canonical
from tracing import encoded, exchange, sanitized


MAX_REPLY_BYTES = 65536
VERIFY_TIMEOUT = 2
_NETWORK_SLOTS = threading.BoundedSemaphore(4)


class PolicyError(ValueError):
    code = "verification.policy_invalid"
    origin = "runtime.plugins"


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
