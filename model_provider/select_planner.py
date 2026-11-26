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


KEEP_ALIVE = re.compile(r"-1|0|[1-9][0-9]{0,3}[smh]")


def fingerprint(profile):
    return hashlib.sha256(json_bytes(profile)).hexdigest() if profile is not None else "environment"


def change(directory, *, model=None, url="http://model-provider:8134", restore=None, expect=None,
           think=None, keep_alive=None, num_ctx=4096, num_predict=None, num_thread=None):
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
            if type(num_ctx) is not int or not 128 <= num_ctx <= 65536:
                raise ValueError("invalid num_ctx budget")
            options = {"temperature": 0, "num_ctx": num_ctx}
            if num_predict is not None:
                if type(num_predict) is not int or not 1 <= num_predict <= 1024:
                    raise ValueError("invalid num_predict budget")
                options["num_predict"] = num_predict
            if num_thread is not None:
                if type(num_thread) is not int or not 1 <= num_thread <= 64:
                    raise ValueError("invalid num_thread budget")
                options["num_thread"] = num_thread
            profile = {"schema_version": 1, "version": "model-router-v1",
                       "protocol": "ollama", "url": url.rstrip("/"), "model": model,
                       "options": options}
            if think is not None:
                if type(think) is not bool:
                    raise ValueError("think must be a boolean")
                profile["think"] = think
            if keep_alive is not None:
                if not ((type(keep_alive) is int and -1 <= keep_alive <= 86400)
                        or (isinstance(keep_alive, str) and KEEP_ALIVE.fullmatch(keep_alive))):
                    raise ValueError("invalid keep_alive duration")
                profile["keep_alive"] = keep_alive
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
    parser.add_argument("--think", choices=("true", "false"), help="explicitly enable or disable hybrid model thinking")
    parser.add_argument("--keep-alive", help="Ollama model residency window, e.g. 30m, 1h, 0, or -1")
    parser.add_argument("--num-ctx", type=int, default=4096, help="Ollama KV context window (128..65536)")
    parser.add_argument("--num-predict", type=int, help="maximum output tokens per planning call (1..1024)")
    parser.add_argument("--num-thread", type=int, help="CPU thread budget when running partially on CPU (1..64)")
    args = parser.parse_args()
    think_flag = None if args.think is None else (args.think == "true")
    keep = int(args.keep_alive) if args.keep_alive and re.fullmatch(r"-?[0-9]+", args.keep_alive) else args.keep_alive
    try:
        result = change(args.contracts_dir, model=args.model, restore=args.restore, url=args.url, expect=args.expect,
                        think=think_flag, keep_alive=keep, num_ctx=args.num_ctx,
                        num_predict=args.num_predict, num_thread=args.num_thread)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
