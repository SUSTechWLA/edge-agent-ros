"""Read-only gateway from an operator-published fact file to the isolated software network."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import time

from edge_contracts.spatial_facts import MAX_SOURCES, check_batch, identifier, ns, require


def read_regular(path, limit):
    # Paths are fixed by the deployment. Never follow a symlink or block on a FIFO.
    path = Path(path)
    require(path.resolve(strict=True) == path.absolute(), "sensing.invalid_source_file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "sensing.invalid_source_file")
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, "sensing.batch_too_large")
    return raw


def optional_sources(directory, explicit=(), *, environment=None):
    """Resolve trusted per-provider manifests once, before accepting queries."""
    sources = list(explicit)
    directory = Path(directory)
    try:
        directory_mode = directory.lstat().st_mode
    except FileNotFoundError:
        files = []
    else:
        require(stat.S_ISDIR(directory_mode) and directory.resolve(strict=True) == directory.absolute(),
                "sensing.invalid_source_directory")
        files = []
        for path in directory.iterdir():
            if path.suffix == ".json":
                require(len(files) < MAX_SOURCES, "sensing.too_many_sources")
                files.append(path)
    manifests = [read_regular(manifest, 16384) for manifest in sorted(files)]
    for key, value in sorted((environment or {}).items()):
        if key.startswith("EDGE_SPATIAL_INPUT_"):
            require(len(key) > len("EDGE_SPATIAL_INPUT_"), "sensing.invalid_source_manifest")
            require(len(manifests) < MAX_SOURCES, "sensing.too_many_sources")
            require(len(value) <= 16384 and len(value.encode("utf8")) <= 16384, "sensing.batch_too_large")
            manifests.append(value)
    for payload in manifests:
        records = json.loads(payload)
        require(isinstance(records, list) and 1 <= len(records) < MAX_SOURCES, "sensing.invalid_source_manifest")
        for record in records:
            require(isinstance(record, dict) and set(record) == {"source_id", "file"}
                    and isinstance(record["file"], str) and 0 < len(record["file"]) <= 1024
                    and record["file"].isprintable(), "sensing.invalid_source_manifest")
            source_file = Path(record["file"])
            require(source_file.is_absolute() and ".." not in source_file.parts, "sensing.invalid_source_path")
            sources.append((record["source_id"], source_file))
            require(len(sources) < MAX_SOURCES, "sensing.too_many_sources")
    names = [name for name, _ in sources]
    require(len(names) < MAX_SOURCES and all(identifier(name) for name in names), "sensing.invalid_source_manifest")
    require(len(names) == len(set(names)), "sensing.duplicate_source")
    return sources


def read_source(path, actor, now_ns):
    raw = read_regular(path, 65536)
    batch = json.loads(raw)
    require(isinstance(batch, dict)
            and set(batch) <= {"schema_version", "actor_ref", "clock_id", "basis", "samples", "as_of_ns"},
            "sensing.invalid_batch")
    batch["as_of_ns"] = now_ns
    check_batch(batch)
    require(batch["actor_ref"] == actor and batch["basis"] == "published"
            and batch["clock_id"] == "unix", "sensing.actor_mismatch")
    require(all(s["clock_id"] == "unix" for s in batch["samples"]), "sensing.clock_mismatch")
    return batch


def published_batch(path, actor, now_ns, max_age_ns, optional=()):
    require(identifier(actor) and ns(now_ns) and ns(max_age_ns), "sensing.invalid_request")
    batch = read_source(path, actor, now_ns)
    def fresh(sample):
        return 0 <= now_ns - sample["stamp_ns"] <= max_age_ns
    require(all(fresh(s) for s in batch["samples"]), "sensing.sample_stale")
    claimed = {s["source_id"] for s in batch["samples"]}
    require(len(claimed) == len(batch["samples"]), "sensing.duplicate_source")
    require(len(claimed) + len(optional) <= MAX_SOURCES, "sensing.too_many_sources")
    gaps = []
    for name, source in optional:
        require(identifier(name) and name not in claimed, "sensing.duplicate_source")
        claimed.add(name)
        try:
            other = read_source(source, actor, now_ns)
        except FileNotFoundError:
            gaps.append({"source_id": name, "reason": "sensing.source_missing"})
            continue
        require(len(other["samples"]) == 1 and other["samples"][0]["source_id"] == name,
                "sensing.source_mismatch")
        sample = other["samples"][0]
        if not fresh(sample):
            gaps.append({"source_id": name, "reason": "sensing.sample_stale"})
        else:
            batch["samples"].append(sample)
    if gaps:
        batch["source_gaps"] = gaps
    check_batch(batch)
    require(len(json.dumps(batch, ensure_ascii=True).encode()) <= 65536, "sensing.batch_too_large")
    return batch


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--file", type=Path, required=True)
    cli.add_argument("--max-age-ns", type=int, required=True)
    cli.add_argument("--optional-source", action="append", nargs=2, default=[], metavar=("SOURCE_ID", "FILE"))
    cli.add_argument("--source-dir", type=Path, default=Path("/etc/edge/spatial-sources.d"),
                     help="directory of immutable site-approved optional source manifests")
    args = cli.parse_args()
    if not ns(args.max_age_ns) or not 0 < args.max_age_ns <= 10**10:
        cli.error("max age must be positive and at most ten seconds")
    try:
        optional = optional_sources(args.source_dir, args.optional_source, environment=os.environ)
    except (OSError, ValueError, RecursionError, RuntimeError) as error:
        cli.error("invalid optional source deployment: " + str(error))

    class Reader(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/observations":
                return self.send_error(404)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 512:
                    return self.send_error(400)
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, dict) or set(request) != {"actor_ref"}:
                    return self.send_error(400)
                batch = published_batch(args.file, request["actor_ref"], time.time_ns(), args.max_age_ns,
                                        optional)
                content = json.dumps(batch, allow_nan=False).encode()
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, RecursionError):
                return self.send_error(503, "Observation unavailable")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    ThreadingHTTPServer(("0.0.0.0", 8137), Reader).serve_forever()


if __name__ == "__main__":
    main()
