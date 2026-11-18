"""Measure catch-up after an actual Console outage with two durable subscribers in isolated Docker."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener
import uuid


MODULE = "probe.backlog"
SOURCE = "journal:" + MODULE
KIND = "probe.sample"
SEED = 64
FILE = Path(__file__).resolve()


def describe(pairs):
    """An ordered digest also detects receiving the right ID paired with the wrong ordinal."""
    pairs = list(pairs)
    unique = len({event_id for event_id, _ in pairs})
    raw = json.dumps(pairs, separators=(",", ":")).encode()
    return {"rows": len(pairs), "unique_ids": unique, "duplicate_ids": len(pairs)-unique,
            "ordered_id_and_ordinal_sha256": hashlib.sha256(raw).hexdigest()}


def publish(trace, start, stop):
    from module_journal import ModuleJournal
    started = time.monotonic()
    with closing(ModuleJournal("/sources/" + MODULE + "/module-events.sqlite")) as journal:
        for first in range(start, stop, 32):
            with journal.db:
                for ordinal in range(first, min(first+32, stop)):
                    eid = f"{trace}.{ordinal:06d}"
                    journal.emit({"event_id": eid, "trace_id": trace, "module": MODULE,
                                  "kind": KIND, "code": KIND, "data": {"ordinal": ordinal}}, key=eid)
        rows = journal.db.execute("""SELECT event_id,json_extract(event,'$.data.ordinal')
                                     FROM module_bus_events ORDER BY seq""")
        return {**describe(rows), "source_id": journal.source_id,
                "publish_seconds": round(time.monotonic()-started, 3)}


def consume(name, mode):
    from event_sink import Sink, SinkError
    from module_journal import ModuleJournal
    selector = {"source": SOURCE}
    if mode == "replay":
        # Same facts, independent selector with a fresh cursor starting at zero.
        selector["kind"] = KIND
    module = "probe." + name
    with closing(Sink("/private/copied.sqlite", "http://console:8080", selector)) as sink, \
            closing(ModuleJournal("/sources/" + module + "/module-events.sqlite")) as journal:
        sink.bind_journal(journal, module)
        started = time.monotonic()
        pages = 0
        try:
            while True:
                more = sink.copy()
                pages += 1
                if not more:
                    break
        except SinkError as exc:
            if mode != "fault" or exc.reason != "subscription.source_unavailable":
                raise
        else:
            if mode == "fault":
                raise AssertionError("stopped Console unexpectedly accepted a read")
        elapsed = time.monotonic() - started
        rows = sink.db.execute("""SELECT json_extract(event,'$.event_id'),json_extract(event,'$.data.ordinal')
                                  FROM events ORDER BY seq""")
        notices = [json.loads(raw) for raw, in journal.db.execute(
            "SELECT event FROM module_bus_events ORDER BY seq")]
        checkpoint = sink.db.execute("SELECT cursor FROM checkpoints WHERE selector=?",
                                     (sink.key,)).fetchone()
        return {"copy": describe(rows), "cursor": checkpoint[0] if checkpoint else None,
                "seconds": round(elapsed, 3), "committed_page_reads": pages,
                "notices": [{k: event[k] for k in ("kind", "event_id", "parent_event_id", "data")}
                            for event in notices]}


def orchestrate(image, total, out):
    project = "edge-backlog-" + uuid.uuid4().hex[:12]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "EDGE_"))}
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    image_id = info["Id"]
    expected = describe((f"{project}.{n:06d}", n) for n in range(total))
    report = {"passed": False, "started_utc": datetime.now(timezone.utc).isoformat(), "project": project,
              "image": image, "image_id": image_id, "architecture": info["Architecture"],
              "revision": (info["Config"].get("Labels") or {}).get("org.opencontainers.image.revision"),
              "topology": "Real ModuleJournal, ROS Console/JournalIngress/History HTTP API, two production Sink "
                          "instances on distinct persistent volumes, independent host HTTP witness. Synthetic "
                          "numbered facts, batches of up to 32 per producer transaction. No robot, Gazebo or model.",
              "expected": expected, "pre_outage_rows": SEED, "outage_backlog": total-SEED}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=project) as temp:
        compose_file = Path(temp)/"compose.json"
        common = {"image": image_id, "pull_policy": "never", "cap_drop": ["ALL"],
                  "security_opt": ["no-new-privileges:true"]}
        observer = {**common, "command": ["python3", "/opt/edge/console/server/main.py"],
                    "environment": {"ROS_DOMAIN_ID": "161"}, "ports": ["127.0.0.1::8080"],
                    "volumes": ["sources:/var/lib/edge-module-sources:ro", "archive:/var/lib/edge-console"]}
        mounts = [FILE.as_posix()+":/probe.py:ro", "sources:/sources"]
        helper = {**common, "entrypoint": ["python3", "-B", "/probe.py"]}
        services = {"console": observer, "publisher": {**helper, "network_mode": "none", "volumes": mounts}}
        for name in ("alpha", "beta"):
            services[name] = {**helper, "volumes": mounts+[name+":/private"]}
        compose_file.write_text(json.dumps({"services": services, "volumes": {
            name: {} for name in ("sources", "archive", "alpha", "beta")}}), encoding="utf-8")

        def compose(*args):
            result = subprocess.run(["docker", "compose", "-p", project, "-f", str(compose_file), *args],
                                    env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=240)
            if result.returncode:
                raise RuntimeError(result.stderr[-1800:] or result.stdout[-1800:])
            return result.stdout.strip()

        def worker(service, *args):
            return json.loads(compose("run", "--rm", "--no-deps", "-T", service, *map(str, args)))

        unrelated_proxies_disabled = build_opener(ProxyHandler({}))

        def get(base, route, **query):
            with unrelated_proxies_disabled.open(base+route+"?"+urlencode(query), timeout=4) as response:
                return json.load(response)

        def wait_stats(base, source, count):
            deadline = time.monotonic()+210
            while time.monotonic() < deadline:
                try:
                    stats = get(base, "/api/event-stats", source=source)
                    if stats["count"] >= count:
                        return stats
                except OSError:
                    pass
                time.sleep(.15)
            raise TimeoutError(f"Console did not expose {count} facts from {source}")

        def read_public(base, source):
            rows, after = [], 0
            while True:
                page = get(base, "/api/events", source=source, after=after, limit=200)
                rows.extend(page["events"])
                if not page["has_more"]:
                    return rows
                after = rows[-1]["seq"]

        try:
            seed = worker("publisher", "publish", project, 0, SEED)
            compose("up", "--no-build", "-d", "console")
            base = "http://" + compose("port", "console", "8080")
            old_archive = wait_stats(base, SOURCE, SEED)["archive_id"]
            warm = {n: worker(n, "consume", n, "normal") for n in ("alpha", "beta")}
            assert all(w["copy"]["rows"] == SEED for w in warm.values())
            compose("stop", "-t", "3", "console")
            with ThreadPoolExecutor(max_workers=2) as pool:
                fault = pool.submit(worker, "alpha", "consume", "alpha", "fault")
                source = worker("publisher", "publish", project, SEED, total)
                first = fault.result()
            repeated = worker("alpha", "consume", "alpha", "fault")
            assert repeated["notices"] == first["notices"] and len(first["notices"]) == 1
            assert first["notices"][0]["kind"] == "subscription.blocked"
            assert first["cursor"] == repeated["cursor"] == warm["alpha"]["cursor"]
            assert seed["source_id"] == source["source_id"] and all(source[key] == value for key,value in expected.items())
            report.update(producer=source, first_fault=first["notices"][0], repeated_outage_notices=1)
            started = time.monotonic()
            compose("start", "console")
            base = "http://" + compose("port", "console", "8080")
            stats = wait_stats(base, SOURCE, total)
            report["console_start_to_backlog_visible_seconds"] = round(time.monotonic()-started, 3)
            assert stats["archive_id"] == old_archive and stats["count"] == total
            report["archive_id"] = old_archive
            report["archive_http_witness"] = describe((e["event_id"], e["data"]["ordinal"])
                                                      for e in read_public(base, SOURCE))
            assert report["archive_http_witness"] == expected
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {n: pool.submit(worker, n, "consume", n, "normal") for n in warm}
                readers = {n: future.result() for n, future in futures.items()}
            assert all(r["copy"] == expected for r in readers.values())
            report["consumers"] = readers
            for name in readers:
                replay = worker(name, "consume", name, "replay")
                assert replay["copy"] == expected and replay["committed_page_reads"] >= (total+31)//32
                readers[name]["full_replay_seconds"] = replay["seconds"]
                readers[name]["full_replay_pages"] = replay["committed_page_reads"]
            assert not readers["beta"]["notices"]
            wait_stats(base, "journal:probe.alpha", 2)
            notices = read_public(base, "journal:probe.alpha")
            assert [r["kind"] for r in notices] == ["subscription.blocked", "subscription.restored"]
            assert notices[1]["cause_seq"] == notices[0]["seq"]
            assert notices[1]["parent_event_id"] == notices[0]["event_id"] == first["notices"][0]["event_id"]
            report["public_fault_and_restoration"] = [{k: n[k] for k in (
                "kind", "seq", "cause_seq", "event_id", "parent_event_id")} for n in notices]
            report["passed"] = True
        finally:
            try:
                compose("down", "--volumes", "--remove-orphans", "-t", "3")
            finally:
                out.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:2] in (["publish"], ["consume"]):
        sys.path[:0] = ["/opt/edge/client", "/opt/edge/agent"]
        result = (publish(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
                  if sys.argv[1] == "publish" else consume(sys.argv[2], sys.argv[3]))
        print(json.dumps(result))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--image", required=True)
        parser.add_argument("--facts", type=int, default=1088, help="total facts including 64 warm-up facts")
        parser.add_argument("--out", required=True, type=Path)
        args = parser.parse_args()
        if not 1064 <= args.facts <= 10000:
            parser.error("--facts must leave 1000 or more pending after warm-up and be at most 10000")
        orchestrate(args.image, args.facts, args.out)
