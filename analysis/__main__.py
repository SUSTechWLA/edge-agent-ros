"""Command line entry point for offline task analysis."""
import argparse
import json
from pathlib import Path

from . import analyze, format_text


def main():
    parser = argparse.ArgumentParser(description="Summarize local exports for one task.")
    parser.add_argument("--task", type=Path, help="GET /agent/tasks?id=... JSON response")
    parser.add_argument("--console", type=Path, help="GET /api/export NDJSON file")
    parser.add_argument("--evidence", type=Path, help="query_evidence receipt JSON")
    parser.add_argument("--recording", type=Path, help="Recorder's exported recording.json")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    args = parser.parse_args()
    try:
        report = analyze(task=args.task, console=args.console, evidence=args.evidence, recording=args.recording)
    except ValueError as error:
        parser.exit(2, f"{parser.prog}: {error}\n")
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.format == "json" else format_text(report))


if __name__ == "__main__":
    main()
