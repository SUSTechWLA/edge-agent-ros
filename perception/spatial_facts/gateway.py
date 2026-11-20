"""Read-only gateway from an operator-published fact file to the isolated software network."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import time

from edge_contracts.spatial_facts import MAX_SOURCES, check_batch, identifier, ns, require


def read_source(path, actor, now_ns):
    # Paths are fixed at process startup; HTTP inputs never participate in file selection.
    # Reject symlinked input and non-regular files, including a replaced FIFO.
    path = Path(path)
    require(path.resolve(strict=True) == path.absolute(), "sensing.invalid_source_file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "sensing.invalid_source_file")
        raw = stream.read(65537)
    require(len(raw) <= 65536, "sensing.batch_too_large")
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
    args = cli.parse_args()
    if not ns(args.max_age_ns) or not 0 < args.max_age_ns <= 10**10:
        cli.error("max age must be positive and at most ten seconds")
    names = [name for name, _ in args.optional_source]
    if len(names) >= MAX_SOURCES or len(set(names)) != len(names) or not all(identifier(name) for name in names):
        cli.error("optional sources require unique bounded identifiers")

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
                                        args.optional_source)
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
