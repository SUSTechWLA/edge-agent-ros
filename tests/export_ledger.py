"""Export the running core's SQLite ledger as one consistent, read-only JSON snapshot."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def snapshot(path: Path) -> dict:
    path = path.resolve(strict=True)
    # The running core owns these files. A reader must not initialize missing
    # WAL sidecars, nor use immutable=1 (which can miss uncheckpointed writes).
    for suffix in ("-wal", "-shm"):
        if not Path(str(path) + suffix).is_file():
            raise RuntimeError(f"live SQLite WAL sidecar missing: {path.name}{suffix}")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError(f"SQLite integrity_check failed: {integrity!r}")
        missions = connection.execute(
            "SELECT mission_id, state, detail FROM missions ORDER BY mission_id"
        ).fetchall()
        steps = connection.execute(
            "SELECT mission_id, step, x, y, operation_id, operation_id AS action_id, "
            "action_result, evidence_x, evidence_y, odom_stamp_ns "
            "FROM steps ORDER BY mission_id, step"
        ).fetchall()
        return {"integrity_check": "ok", "missions": [dict(m) for m in missions],
                "steps": [dict(s) for s in steps]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("/var/lib/edge-agent/edge.db"))
    args = parser.parse_args()
    try:
        result = snapshot(args.db)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"ledger export failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
