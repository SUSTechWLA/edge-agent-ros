"""Read-only gateway from an operator-published fact file to the isolated software network."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import time

from edge_contracts.spatial_facts import check_batch, ns, require


def published_batch(path, actor, now_ns, max_age_ns):
    with path.open("rb") as stream:
        raw = stream.read(65537)
    require(len(raw) <= 65536, "sensing.batch_too_large")
    batch = json.loads(raw)
    batch["as_of_ns"] = now_ns
    check_batch(batch)
    require(batch["actor_ref"] == actor and batch["basis"] == "published"
            and batch["clock_id"] == "unix", "sensing.actor_mismatch")
    require(all(s["clock_id"] == "unix" and 0 <= now_ns - s["stamp_ns"] <= max_age_ns
                for s in batch["samples"]), "sensing.sample_stale")
    return batch


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--file", type=Path, required=True)
    cli.add_argument("--max-age-ns", type=int, required=True)
    args = cli.parse_args()
    if not ns(args.max_age_ns) or not 0 < args.max_age_ns <= 10**10:
        cli.error("max age must be positive and at most ten seconds")

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
                batch = published_batch(args.file, request["actor_ref"], time.time_ns(), args.max_age_ns)
                content = json.dumps(batch, allow_nan=False).encode()
            except (OSError, ValueError, KeyError, TypeError):
                return self.send_error(503, "Observation unavailable")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    ThreadingHTTPServer(("0.0.0.0", 8137), Reader).serve_forever()


if __name__ == "__main__":
    main()
