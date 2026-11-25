"""Local site adapter for managed DSH v1 scene services and their isolated workers."""
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from edge_contracts.service_directory import ServiceSnapshot
from .transport import SERVICES, VERSION, SceneRuntimeError, call, exports_for_site, pin_service


ROOT = Path(__file__).resolve().parents[2]
# plugin_manager also supports direct CLI launch; its legacy imports live in agent/.
if str(ROOT / "agent") not in sys.path:
    sys.path.append(str(ROOT / "agent"))
import plugin_manager


def managed_change(state, descriptor, action, expected):
    """Use the repository's CAS publisher and append its normal durable receipt."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    name = plugin_manager.RuntimeRelease.check_id(descriptor["id"])
    if action not in ("publish", "remove"):
        raise ValueError("expected publish or remove")
    with tempfile.TemporaryDirectory(dir=state) as candidate:
        definition = Path(candidate) / (name + ".json")
        if action == "publish":
            definition.write_text(json.dumps(descriptor, allow_nan=False), encoding="utf-8")
        return plugin_manager.change(argparse.Namespace(root=state, kind="runtime", action=action,
                                                        expect=expected, id=name, definition=definition))


class OwnerLock:
    """One supervisor per replay site. Registry CAS also protects against external writers."""
    def __init__(self, path):
        self.handle = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.handle.seek(0, 2) == 0:
                    self.handle.write(b"1")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise SceneRuntimeError("scene.site_in_use") from exc

    def close(self):
        self.handle.close()


@dataclass
class Worker:
    process: subprocess.Popen
    descriptor: dict
    revision: str


class ReplaySite:
    def __init__(self, archive_dir, state_dir, *, grid_directory=None, grid_manifest_ref=None,
                 fusion_directory=None, fusion_manifest_ref=None):
        self.archive = Path(archive_dir).resolve() if archive_dir is not None else None
        self.state = Path(state_dir).resolve()
        self.directory = self.state/"runtime"
        self.grid_directory = Path(grid_directory).resolve() if grid_directory else None
        self.grid_manifest_ref = grid_manifest_ref
        self.fusion_directory = Path(fusion_directory).resolve() if fusion_directory else None
        self.fusion_manifest_ref = fusion_manifest_ref
        if bool(fusion_directory) != bool(fusion_manifest_ref):
            raise ValueError("fusion_directory and fusion_manifest_ref must be supplied together")
        if bool(grid_directory) != bool(grid_manifest_ref):
            raise ValueError("grid_directory and grid_manifest_ref must be supplied together")
        self.state.mkdir(parents=True, exist_ok=True)
        self.owner = OwnerLock(self.state/".scene-supervisor")
        self.workers = {}
        self.start_errors = {}
        self.changes = threading.RLock()
        self.closed = False
        self.configured = (("occupancy", "texture") if self.archive else ()) + ("query",)
        if grid_directory:
            self.configured += ("grid2d",)
        if fusion_directory:
            self.configured += ("fusion",)
        try:
            self.inactive = self._load_intent()
            for kind in self.configured:
                if kind in self.inactive:
                    self._discard_stale(kind)
                    continue
                try:
                    self.enable(kind)
                except SceneRuntimeError:
                    if kind == "query":
                        raise
                    # Each archive source and its release are independent. A faulty
                    # optional representation remains diagnosable and may be retried.
                    self._discard_stale(kind)
        except BaseException:
            self.close()
            raise

    def _load_intent(self):
        try:
            value = json.loads((self.state/"disabled-layers.json").read_bytes())
        except FileNotFoundError:
            return set()
        if (not isinstance(value, list) or not all(isinstance(k, str) and k in SERVICES for k in value)
                or len(set(value)) != len(value)):
            raise SceneRuntimeError("scene.invalid_installation_state")
        return set(value)

    def _set_enabled_intent(self, kind, enabled):
        desired = self.inactive - {kind} if enabled else self.inactive | {kind}
        if desired != self.inactive:
            tmp = self.state/"disabled-layers.tmp"
            tmp.write_text(json.dumps(sorted(desired)), encoding="utf-8")
            os.replace(tmp, self.state/"disabled-layers.json")
            self.inactive = desired

    def _discard_stale(self, kind):
        """Clear an earlier supervisor's unreachable pointer under the site owner lock."""
        service = SERVICES[kind]
        old, revision = plugin_manager.RuntimeRelease.installed(self.directory, service)
        if old is not None:
            if old["provides"] != [service]:
                raise SceneRuntimeError("plugin.service_conflict")
            managed_change(self.state, old, "remove", revision)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _start(self, kind):
        with tempfile.TemporaryDirectory(dir=self.state) as startup:
            ready = Path(startup)/"ready"
            data = self.grid_directory if kind == "grid2d" else self.archive
            if kind == "fusion":
                data = self.fusion_directory
            elif kind == "query":
                data = self.state
            command = [sys.executable, "-B", "-m", "perception.scene_layers.worker",
                       kind, str(data), str(self.state), str(ready)]
            if kind == "grid2d":
                command.extend(["--grid-manifest-ref", self.grid_manifest_ref])
            elif kind == "fusion":
                command.extend(["--fusion-manifest-ref", self.fusion_manifest_ref])
            # Write errors to a bounded-lifetime, per-service startup log. No
            # filesystem or network service accepts arbitrary provider commands.
            with (self.state/(kind+".log")).open("wb") as log:
                proc = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=log, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            try:
                deadline = time.monotonic()+12
                while not ready.is_file():
                    if proc.poll() is not None:
                        failure = ready.with_suffix(".error")
                        if failure.is_file():
                            reason = json.loads(failure.read_bytes())
                            raise SceneRuntimeError(reason["code"], reason["message"])
                        raise SceneRuntimeError("plugin.start_failed", f"{kind} exited, see {kind}.log")
                    if time.monotonic() >= deadline:
                        raise SceneRuntimeError("plugin.start_timeout", kind)
                    time.sleep(.02)
                return proc, ready.read_text(encoding="ascii")
            except BaseException:
                self._stop_unpublished(proc)
                raise

    @staticmethod
    def _stop_unpublished(proc):
        if not proc.stdin.closed:
            proc.stdin.close()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(3)

    def enable(self, kind):
        with self.changes:
            if self.closed or kind not in self.configured:
                raise SceneRuntimeError("scene.provider_not_configured")
            self._set_enabled_intent(kind, True)
            if kind in self.workers:
                existing = self.workers[kind]
                if existing.process.poll() is None and not existing.descriptor.get("draining"):
                    pin = self.pin(kind)
                    return {"state":"ready", "pid":existing.process.pid, "binding":pin["binding"]}
                self._remove_worker(kind)
            # A previous supervisor can have died between publication and disposal.
            # It closed its parent pipe on exit. Publish the new endpoint using CAS.
            service = SERVICES[kind]
            installed, previous = plugin_manager.RuntimeRelease.installed(self.directory, service)
            if installed is not None and installed["provides"] != [service]:
                raise SceneRuntimeError("plugin.service_conflict")
            try:
                proc, endpoint = self._start(kind)
            except SceneRuntimeError as exc:
                self.start_errors[kind] = {"code":exc.code, "reason":str(exc)}
                raise
            descriptor = {"schema_version":1, "id":service, "version":"1.1.0" if kind == "texture" else VERSION, "endpoint":endpoint,
                          "provides":[service], "requires":[]}
            try:
                receipt = managed_change(self.state, descriptor, "publish", previous)
            except BaseException:
                self._stop_unpublished(proc)
                raise
            self.workers[kind] = Worker(proc, descriptor, receipt["version"])
            self.start_errors.pop(kind, None)
            return {"state":"ready", "pid":proc.pid, "binding":self.pin(kind)["binding"], "publish":receipt}

    def disable(self, kind):
        with self.changes:
            if self.closed or kind not in self.configured:
                raise SceneRuntimeError("scene.provider_not_configured")
            plugin_manager.RuntimeRelease.check_graph(self.directory, SERVICES[kind], None)
            self._set_enabled_intent(kind, False)
            self.start_errors.pop(kind, None)
            return self._remove_worker(kind)

    def _remove_worker(self, kind):
        worker = self.workers.get(kind)
        if worker is None:
            return {"state":"absent", "service":SERVICES[kind]}
        drain = None
        if not worker.descriptor.get("draining"):
            marked = dict(worker.descriptor, draining=True)
            drain = managed_change(self.state, marked, "publish", worker.revision)
            worker.descriptor, worker.revision = marked, drain["version"]
        if not worker.process.stdin.closed:
            worker.process.stdin.close()
        try:
            exit_code = worker.process.wait(12)
        except subprocess.TimeoutExpired as exc:
            # A still-occupied provider stays visibly draining; removal can be retried.
            raise SceneRuntimeError("plugin.drain_timeout") from exc
        remove = managed_change(self.state, worker.descriptor, "remove", worker.revision)
        del self.workers[kind]
        return {"state":"absent", "service":SERVICES[kind], "pid":worker.process.pid,
                "exit_code":exit_code, "drain":drain, "remove":remove}

    def close(self):
        with self.changes:
            if self.closed:
                return
            for kind in list(self.workers)[::-1]:
                self._remove_worker(kind)
            self.closed = True
            self.owner.close()

    def pin(self, kind):
        return pin_service(self.directory, kind)

    def catalog(self):
        catalog = ServiceSnapshot(self.directory, observing=True).public()
        kinds = {export:kind for kind, export in exports_for_site(self.directory).items()}
        for row in catalog["plugins"]:
            kind = next((kinds[export] for export in row.get("provides",[]) if export in kinds), None)
            if kind is not None:
                row.update(kind=kind, service=next(export for export in row["provides"] if export in kinds))
            worker = self.workers.get(kind)
            if worker is not None:
                running = worker.process.poll() is None
                row.update(kind=kind, service=SERVICES[kind], pid=worker.process.pid, process_alive=running)
                if not running and row["status"] == "ready":
                    row.update(status="offline", code="plugin.service_unavailable")
        for kind, error in list(self.start_errors.items()):
            catalog["plugins"].append({"id":SERVICES[kind], "kind":kind, "service":SERVICES[kind],
                                       "status":"offline", "process_alive":False, **error})
        return catalog

    def describe(self, kind):
        return call(self.directory, kind, "describe")

    def freeze(self, kinds, **limits):
        return call(self.directory, "query", "freeze", kinds=kinds, limits=limits)

    def snapshot(self, scene_ref):
        return call(self.directory, "query", "snapshot", scene_ref=scene_ref)

    def read(self, kind, layer_ref, *, scene_ref=None):
        if scene_ref is not None:
            return call(self.directory, "query", "read", scene_ref=scene_ref, kind=kind, layer_ref=layer_ref)
        return call(self.directory, kind, "read", layer_ref=layer_ref)

    def texture_png(self, layer_ref, *, expected=None):
        return call(self.directory, "texture", "texture_png", expected=expected, layer_ref=layer_ref)
