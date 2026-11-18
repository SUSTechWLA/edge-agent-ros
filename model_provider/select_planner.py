"""Atomically select the model for future planning attempts in an existing Agent."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from author_tool import json_bytes, read_json, sync_directory, write_atomic, writer_lock


def fingerprint(profile):
    return hashlib.sha256(json_bytes(profile)).hexdigest() if profile is not None else "environment"


def change(directory, *, model=None, url="http://model-provider:8134", restore=None, expect=None):
    """Use semantic profile digests so a formatting-only edit still matches the same configuration."""
    if not directory.is_dir() or (model is None) == (restore is None):
        raise ValueError("existing contract directory and one model or restore target required")
    current = directory / "planner-profile.json"
    history = directory / ".planner-history"
    if current.is_symlink() or history.is_symlink():
        raise ValueError("unexpected symlink in planner configuration")
    with writer_lock(history):
        old = read_json(current, 16384) if current.exists() else None
        if current.exists() and not isinstance(old, dict):
            raise ValueError("existing planner profile must be an object")
        before = fingerprint(old)
        if expect is not None and expect != before:
            raise ValueError("active profile differs from expected revision")
        if restore is not None:
            if restore == "environment":
                profile = None
            else:
                if not re.fullmatch(r"[a-f0-9]{64}", restore):
                    raise ValueError("invalid saved revision")
                saved = history / (restore + ".json")
                if saved.is_symlink():
                    raise ValueError("unexpected saved revision symlink")
                profile = read_json(saved, 16384)
                if not isinstance(profile, dict) or fingerprint(profile) != restore:
                    raise ValueError("saved revision does not match its content")
        else:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}", model):
                raise ValueError("invalid model alias")
            endpoint = urlsplit(url)
            if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                    or endpoint.username is not None or endpoint.query or endpoint.fragment
                    or any(c.isspace() for c in url) or endpoint.port == 0):
                raise ValueError("invalid adapter URL")
            profile = {"schema_version": 1, "version": "model-router-v1",
                       "protocol": "ollama", "url": url.rstrip("/"), "model": model,
                       "options": {"temperature": 0, "num_ctx": 4096}}
        after = fingerprint(profile)
        if before != after:
            if old is not None:
                archive = history / (before + ".json")
                if archive.is_symlink() or (archive.exists() and fingerprint(read_json(archive, 16384)) != before):
                    raise ValueError("prior profile archive is inconsistent")
                if not archive.exists():
                    write_atomic(archive, old)
            if profile is None:
                current.unlink()
                sync_directory(directory)
            else:
                write_atomic(current, profile)
        return {"previous": before, "active": after, "changed": before != after,
                "model": profile.get("model") if profile else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--model", help="configured alias, e.g. qwen2.5:1.5b or deepseek-flash")
    target.add_argument("--restore", help="previous digest from a receipt, or environment")
    parser.add_argument("--url", default="http://model-provider:8134", help="address reachable from the Agent")
    parser.add_argument("--contracts-dir", type=Path, default=ROOT / "contracts")
    parser.add_argument("--expect", help="only apply when the active digest matches")
    args = parser.parse_args()
    try:
        result = change(args.contracts_dir, model=args.model, restore=args.restore, url=args.url, expect=args.expect)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
