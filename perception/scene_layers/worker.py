"""Serve one approved read-only map representation or scene query on loopback."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading

from edge_contracts.service_directory import ServiceSnapshot
from .core import Archive, SceneError, check_read, freeze_scene, validated_descriptor
from .transport import MAX_REQUEST, SceneRuntimeError, call, pin_service


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def implementation_ref(kind):
    root = Path(__file__).resolve().parents[2]
    sources = ["perception/scene_layers/" + name for name in ("core.py", "transport.py", "worker.py")]
    geometry = {
        "occupancy": ["perception/rover_plan_field/scene.py"],
        "texture": ["perception/rover_plan_field/rgb_scene.py", "perception/rover_plan_field/scene.py"],
        "grid2d": ["task_recorder/global_map.py", "edge_contracts/occupancy.py"],
    }
    manifest = {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                for name in [*sources, *geometry.get(kind, [])]}
    return "sha256:" + hashlib.sha256(encode(manifest)).hexdigest()


class Snapshots:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, ref):
        if not isinstance(ref, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", ref):
            raise SceneRuntimeError("scene.invalid_reference")
        return self.directory / (ref[7:] + ".json")

    def save(self, ref, manifest):
        path = self.path(ref)
        encoded = encode(manifest)
        if len(encoded) > MAX_REQUEST:
            raise SceneRuntimeError("scene.snapshot_too_large")
        # Never overwrite an older snapshot with a different interpretation.
        if path.is_file():
            if json.loads(path.read_bytes()) != manifest:
                raise SceneRuntimeError("scene.content_mismatch")
            return
        # The scene ref binds all contents. Concurrent publishers of this ref
        # write equivalent manifests and readers see only completed files.
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as output:
            tmp = Path(output.name)
            try:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                output.close()
                tmp.unlink(missing_ok=True)
                raise
        try:
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def get(self, ref):
        try:
            with self.path(ref).open("rb") as stream:
                raw = stream.read(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST:
                raise SceneRuntimeError("scene.snapshot_mismatch")
            manifest = json.loads(raw)
            entry = manifest["layers"][0]
        except FileNotFoundError as exc:
            raise SceneRuntimeError("scene.snapshot_missing") from exc
        except (IndexError, KeyError, ValueError, TypeError, RecursionError) as exc:
            raise SceneRuntimeError("scene.snapshot_mismatch") from exc
        # Verify a frozen manifest even when callers are only reviewing its metadata.
        check_read(ref, manifest, entry["kind"], entry["layer_ref"])
        return manifest


class Query:
    def __init__(self, directory, snapshots):
        self.directory = directory
        self.snapshots = snapshots

    def request(self, operation, args):
        if operation == "freeze":
            kinds = args["kinds"]
            if (not isinstance(kinds, list) or not 1 <= len(kinds) <= 16 or
                    not all(isinstance(kind, str) for kind in kinds) or len(kinds) != len(set(kinds))
                    or "query" in kinds):
                raise SceneRuntimeError("scene.invalid_layer_request")
            catalog = ServiceSnapshot(self.directory)
            layers = []
            for kind in kinds:
                pin = pin_service(self.directory, kind, catalog=catalog)
                layer = call(self.directory, kind, "describe", expected=pin["binding"])
                if not isinstance(layer, dict) or layer.get("kind") != kind:
                    raise SceneRuntimeError("scene.invalid_descriptor")
                provider = layer.get("provider", {})
                if (not isinstance(provider, dict) or provider.get("binding") != pin["binding"] or
                        not re.fullmatch(r"sha256:[0-9a-f]{64}", str(provider.get("implementation_ref")))):
                    raise SceneRuntimeError("scene.invalid_provider_identity")
                layers.append({**layer, "provider":{**pin, "implementation_ref":provider["implementation_ref"]}})
            result = freeze_scene(layers, **args.get("limits", {}))
            if result["status"] == "ready":
                self.snapshots.save(result["scene_ref"], result["scene"])
            return result
        manifest = self.snapshots.get(args["scene_ref"])
        if operation == "snapshot":
            return manifest
        if operation == "read":
            kind, ref = args["kind"], args["layer_ref"]
            layer = check_read(args["scene_ref"], manifest, kind, ref)
            result = call(self.directory, kind, "read", expected=layer["provider"]["binding"], layer_ref=ref)
            returned = validated_descriptor(result.get("layer", {}))
            if returned != layer:
                raise SceneRuntimeError("scene.layer_mismatch")
            return result
        raise SceneRuntimeError("scene.unsupported_operation")


class WorkerServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def write_result(self, status, body, content_type, binding=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if binding:
            self.send_header("X-DSH-Binding", binding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        s = self.server
        if self.path != "/invoke":
            self.send_error(404)
            return
        try:
            self.connection.settimeout(5)
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise SceneRuntimeError("scene.invalid_request")
            length = int(self.headers["Content-Length"])
            if not 0 < length <= MAX_REQUEST:
                raise SceneRuntimeError("scene.request_too_large")
            req = json.loads(self.rfile.read(length))
            if (not isinstance(req, dict) or set(req) != {"operation","arguments","binding"}
                    or not isinstance(req["arguments"], dict) or not isinstance(req["binding"], str)):
                raise SceneRuntimeError("scene.invalid_request")
            # Direct/stale callers cannot bypass removal or drain through a cached URL.
            pin = pin_service(s.directory, s.kind, req["binding"])
            if pin["endpoint"] != s.endpoint:
                raise SceneRuntimeError("verification.binding_unavailable")
            producer = {**pin, "implementation_ref":s.implementation_ref}
            args, operation = req["arguments"], req["operation"]
            if s.kind == "query":
                result = s.backend.request(operation, args)
            elif operation == "describe":
                result = {**s.backend.describe(s.kind), "provider": producer}
            elif operation == "read":
                result = s.backend.read(s.kind, args["layer_ref"])
                result["layer"] = {**result["layer"], "provider": producer}
            elif operation == "texture_png" and s.kind == "texture":
                data = s.backend.texture_png(args["layer_ref"])
                return self.write_result(200, data, "image/png", pin["binding"])
            else:
                raise SceneRuntimeError("scene.unsupported_operation")
            self.write_result(200, encode(result), "application/json", pin["binding"])
        except (SceneError, SceneRuntimeError) as exc:
            self.write_result(409, encode({"error":{"code": exc.code, "message": str(exc)}}), "application/json")
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            self.write_result(400, encode({"error":{"code":"scene.invalid_request","message":str(exc)[:200]}}),
                              "application/json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("occupancy","texture","grid2d","query"))
    parser.add_argument("archive_dir", type=Path)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("ready_file", type=Path)
    parser.add_argument("--grid-manifest-ref")
    args = parser.parse_args()
    try:
        if args.kind == "grid2d":
            from .core import GridArchive
            backend = GridArchive(args.archive_dir, args.grid_manifest_ref)
        elif args.kind == "query":
            backend = Query(args.state_dir/"runtime", Snapshots(args.state_dir/"snapshots"))
        else:
            backend = Archive(args.archive_dir)
        if args.kind != "query":
            backend.describe(args.kind)
    except (OSError, ValueError, KeyError, TypeError) as error:
        args.ready_file.with_suffix(".error").write_bytes(encode({
            "code":getattr(error,"code","scene.archive_invalid"), "message":str(error)[:240]}))
        raise
    with WorkerServer(("127.0.0.1", 0), Handler) as server:
        server.kind, server.backend = args.kind, backend
        server.implementation_ref = implementation_ref(args.kind)
        server.directory = args.state_dir/"runtime"
        server.endpoint = "http://127.0.0.1:%d/invoke" % server.server_port
        tmp = args.ready_file.with_suffix(".tmp")
        tmp.write_text(server.endpoint, encoding="ascii")
        os.replace(tmp, args.ready_file)
        # Only the local parent owns stdin. EOF starts a graceful shutdown even
        # after a parent crash; server_close waits for admitted request threads.
        def watch_parent():
            sys.stdin.buffer.read(1)
            server.shutdown()
        threading.Thread(target=watch_parent, daemon=True).start()
        server.serve_forever(poll_interval=.05)


if __name__ == "__main__":
    main()
