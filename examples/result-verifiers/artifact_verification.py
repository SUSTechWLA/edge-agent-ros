"""Read-only sidecar harness shared by the two example verifier plugins."""
import errno
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
import re
import stat


MAX_BYTES = 4 * 1024 * 1024
VERSION = "1"
PLUGINS = {"image-check": "verify.image", "digest-check": "verify.digest"}
IDENTITY = ("operation_id", "service", "plugin_id", "plugin_version")
URI = re.compile(r"artifact://([0-9a-f]{64})/([a-zA-Z0-9][a-zA-Z0-9_.-]{0,127})")


class Counterevidence(ValueError):
    """The delivered candidate contradicts the declared artifact contract."""

    def __init__(self, code, **measurements):
        super().__init__(code)
        self.evidence = {"code": code, **measurements}


class Inconclusive(Exception):
    """A trustworthy observation could not be made."""


def read_artifact(root, request):
    image_field = request["outcomes"].get("image")
    uri = request["evidence"].get(image_field) if isinstance(image_field, str) else None
    match = URI.fullmatch(uri) if isinstance(uri, str) else None
    if not match:
        raise Counterevidence("artifact.invalid_reference")
    operation_dir, name = match.groups()
    if operation_dir != hashlib.sha256(request["operation_id"].encode()).hexdigest():
        raise Counterevidence("artifact.wrong_operation")

    # Linux directory-relative opens bind both path components to an already-open
    # parent. A concurrent rename cannot redirect traversal through a symlink.
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise Inconclusive("storage.requires_linux_container")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise Inconclusive("storage.unavailable") from exc
    try:
        try:
            operation_fd = os.open(operation_dir, directory_flags, dir_fd=root_fd)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=operation_fd)
            finally:
                os.close(operation_fd)
        except FileNotFoundError as exc:
            raise Counterevidence("artifact.missing") from exc
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise Counterevidence("artifact.invalid_path") from exc
            raise
    finally:
        os.close(root_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise Counterevidence("artifact.not_private_regular_file")
        if before.st_size > MAX_BYTES:
            raise Inconclusive("artifact.exceeds_verifier_limit")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_BYTES + 1)
        after = os.fstat(fd)
        if len(data) != before.st_size or before.st_mtime_ns != after.st_mtime_ns or after.st_size != before.st_size:
            raise Inconclusive("artifact.changed_while_reading")
        return name, data
    finally:
        os.close(fd)


def receipt(plugin, root, request, inspect):
    if not isinstance(request, dict) or not all(isinstance(request.get(key), str) and 0 < len(request[key]) <= 256
                                                for key in (*IDENTITY, "task_id", "trace_id", "tool")):
        raise ValueError("invalid request identity")
    if (request["plugin_id"], request["service"], request["plugin_version"]) != (plugin, PLUGINS[plugin], VERSION):
        raise ValueError("service or version not provided by this process")
    if not all(isinstance(request.get(key), dict) for key in ("outcomes", "evidence", "arguments")):
        raise ValueError("expected outcomes, evidence and arguments objects")
    try:
        name, data = read_artifact(root, request)
        observed = inspect(name, data, request["evidence"])
        verdict = "pass"
    except Counterevidence as exc:
        verdict, observed = "fail", exc.evidence
    except Inconclusive as exc:
        verdict, observed = "unknown", {"code": str(exc)}
    except OSError:
        verdict, observed = "unknown", {"code": "storage.io_error"}
    return {**{key: request[key] for key in IDENTITY}, "verdict": verdict, "evidence": observed}


class Handler(BaseHTTPRequestHandler):
    timeout = 5

    def do_POST(self):
        if self.path != "/verify":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                self.send_error(413)
                return
            request = json.loads(self.rfile.read(length))
            reply = receipt(self.server.plugin, self.server.root, request, self.server.inspect)
        except (ValueError, RecursionError, UnicodeError):
            self.send_error(400, "invalid verification request")
            return
        raw = json.dumps(reply, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
