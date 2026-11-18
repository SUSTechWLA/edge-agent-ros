"""Sample a running observer and its Console at wall-clock intervals without submitting tasks."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.request import build_opener, ProxyHandler


def sample(health, console, seconds, interval):
    http = build_opener(ProxyHandler({}))

    def get(url):
        with http.open(url, timeout=4) as response:
            return json.load(response)

    rows = []
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        row = {"elapsed_s": round(elapsed, 2)}
        try:
            current = get(health + "/status")
            state = get(console + "/api/state")
            if not current["inputs"] or not state["session_id"]:
                raise ValueError("no configured sensors or no Console session")
            row.update(session=state["session_id"], cursor=state["cursor"], inputs={
                key: {"stamp_ns": v["latest"].get("stamp_ns"), "state": v["state"], "code": v["code"],
                      "event_id": v["event_id"]} for key, v in current["inputs"].items()})
        except (OSError, ValueError, KeyError) as error:
            row["error"] = str(error)
        rows.append(row)
        if elapsed >= seconds:
            break
        time.sleep(max(0, min(start+len(rows)*interval, start+seconds)-time.monotonic()))
    comparable = [(a, b) for a, b in zip(rows, rows[1:]) if "inputs" in a and "inputs" in b]
    lapses = [{"elapsed_s": b["elapsed_s"], "input": k} for a, b in comparable for k, v in a["inputs"].items()
              if not v["stamp_ns"] or not b["inputs"].get(k, {}).get("stamp_ns")
              or b["inputs"][k]["stamp_ns"] <= v["stamp_ns"]]
    errors = sum("error" in r for r in rows)
    alerts = [(r["elapsed_s"], k, v["code"]) for r in rows for k, v in r.get("inputs", {}).items()
              if v["state"] in {"waiting", "needs_attention", "rechecking"}]
    return {"passed": not errors and not lapses and not alerts and bool(comparable),
            "finished_utc": datetime.now(timezone.utc).isoformat(), "duration_s": rows[-1]["elapsed_s"],
            "samples": len(rows), "http_errors": errors, "stalled_sources": lapses, "alerts": alerts, "rows": rows}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--health", default="http://127.0.0.1:8136")
    cli.add_argument("--console", default="http://127.0.0.1:8766")
    cli.add_argument("--seconds", type=int, default=180)
    cli.add_argument("--interval", type=int, default=5)
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    if not 1 <= args.interval <= args.seconds:
        cli.error("require 1 <= interval <= seconds")
    result = sample(args.health.rstrip("/"), args.console.rstrip("/"), args.seconds, args.interval)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)
