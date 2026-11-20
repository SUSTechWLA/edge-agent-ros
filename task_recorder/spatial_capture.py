"""Archive bounded live observations plus the exact artifact bytes selected by a real task."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from urllib.request import Request, urlopen

from edge_contracts.spatial_facts import check_batch, digest_ref, identifier


MAX_BYTES = 128 * 1024 * 1024
FILE_LIMIT = 4 * 1024 * 1024
EXTENSIONS = {"application/json": ".json", "image/png": ".png", "image/jpeg": ".jpg"}


def put(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
        except BaseException:
            stream.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class Archive:
    def __init__(self, root, sources):
        self.root = root
        self.sources = sources
        root.mkdir(exist_ok=False)
        self.total = 0
        self.saved = set()

    def copy(self, sample):
        if sample["kind"] != "artifact":
            return
        ref = sample["artifact_ref"]
        if not digest_ref(ref) or sample["source_id"] not in self.sources or sample["media_type"] not in EXTENSIONS:
            raise ValueError("unrecognized artifact type, scope or digest")
        filename = ref[7:] + EXTENSIONS[sample["media_type"]]
        if filename in self.saved:
            return
        path = self.sources[sample["source_id"]] / "artifacts" / filename
        if path.resolve(strict=True) != path.absolute():
            raise ValueError("unexpected artifact path")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as content:
            if not stat.S_ISREG(os.fstat(content.fileno()).st_mode):
                raise ValueError("expected a regular sensor artifact")
            raw = content.read(FILE_LIMIT + 1)
        if (len(raw) > FILE_LIMIT or self.total + len(raw) > MAX_BYTES
                or hashlib.sha256(raw).hexdigest() != ref[7:]):
            raise ValueError("sensor evidence exceeds quota or differs from task digest")
        put(self.root / filename, raw)
        self.saved.add(filename)
        self.total += len(raw)


def fetch(url, actor):
    request = Request(url, json.dumps({"actor_ref": actor}).encode(), {"Content-Type": "application/json"})
    with urlopen(request, timeout=1.5) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("gateway batch too large")
    result = json.loads(raw)
    check_batch(result)
    if result["actor_ref"] != actor or result["basis"] != "published":
        raise ValueError("unexpected observation binding")
    return result


def complete(samples, expected, actor):
    return {s["source_id"] for s in samples} >= expected and all(s["actor_ref"] == actor for s in samples)


def receipt_samples(task, key, actor, expected):
    if task["id"] != key or task["state"] != "SUCCEEDED" or len(task["steps"]) != 1:
        raise ValueError("task did not complete a single observation read")
    step = task["steps"][0]
    if step["tool"] != "read_spatial_observations" or step["args"] != {"actor_ref": actor}:
        raise ValueError("unexpected task tool or arguments")
    data = step["evidence"]
    if data["basis"] != "published" or data["actor_ref"] != actor or not complete(data["samples"], expected, actor):
        raise ValueError("required observations missing from original task receipt")
    if any(not 0 <= int(data["as_of_ns"]) - int(s["stamp_ns"]) <= 10**9 for s in data["samples"]):
        raise ValueError("task received observations outside the one-second live budget")
    return data["samples"]


def record(options):
    out = options.out
    out.mkdir(parents=True, exist_ok=True)
    sources = {name: Path(path) for name, path in options.source}
    if (len(sources) != len(options.source) or not all(identifier(name) for name in [*sources, options.actor])
            or not options.required or not all(identifier(name) for name in options.required)):
        raise ValueError("expected distinct named artifact sources and required observations")
    needed = set(options.required)
    archive = Archive(out / "artifacts", sources)
    start = time.monotonic()
    complete_since = tail_start = None
    count = 0
    task_file = out / "task.json"
    with (out / "sensor-timeline.jsonl").open("x", encoding="utf8", buffering=1) as log:
        while time.monotonic() - start < options.timeout:
            tick = time.monotonic()
            if task_file.is_file() and tail_start is None:
                for sample in receipt_samples(json.loads(task_file.read_bytes()), options.task, options.actor, needed):
                    archive.copy(sample)
                tail_start = tick
            if tail_start is not None and tick - tail_start >= options.tail:
                result = {"task_id": options.task, "batches": count, "artifact_files": len(archive.saved),
                          "bytes": archive.total}
                put(out / "capture.json", json.dumps(result).encode())
                return result
            try:
                batch = fetch(options.url, options.actor)
                for sample in batch["samples"]:
                    archive.copy(sample)
            except (OSError, ValueError) as error:
                log.write(json.dumps({"received_ns": str(time.time_ns()), "samples": [],
                                      "source_gaps": [{"reason": type(error).__name__ + ": " + str(error)[:160]}]}) + "\n")
                complete_since = None
            else:
                log.write(json.dumps({"received_ns": str(time.time_ns()), **batch}, separators=(",", ":")) + "\n")
                count += 1
                if complete(batch["samples"], needed, options.actor):
                    complete_since = tick if complete_since is None else complete_since
                    if tick - complete_since >= options.lead and not (out / "ready.json").exists():
                        put(out / "ready.json", json.dumps({"task_id": options.task, "ready_ns": str(time.time_ns())}).encode())
                else:
                    complete_since = None
            time.sleep(max(0, 0.2 - (time.monotonic() - tick)))
    raise RuntimeError("timed out waiting for a complete live task receipt")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--url", default="http://spatial-facts:8137/observations")
    cli.add_argument("--actor", required=True)
    cli.add_argument("--task", required=True)
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument("--source", nargs=2, action="append", default=[], metavar=("SOURCE", "ROOT"))
    cli.add_argument("--required", nargs="+", required=True)
    cli.add_argument("--lead", type=float, default=3.)
    cli.add_argument("--tail", type=float, default=5.)
    cli.add_argument("--timeout", type=float, default=45.)
    args = cli.parse_args()
    if not (0 < args.lead <= 15 and 0 < args.tail <= 15 and args.lead + args.tail < args.timeout <= 120):
        cli.error("lead/tail must be positive and fit within the bounded time budget")
    print(json.dumps(record(args)), flush=True)
