"""Read a bounded set of operator-installed design contracts for one planning request."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

FIELDS = {"id", "version", "reference", "owner", "inputs", "outputs", "acceptance", "rollback"}


class ContractError(ValueError):
    pass


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("contract_invalid")
        result[key] = value
    return result


def read_contract(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        raise ContractError("contract_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ContractError("contract_invalid")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ContractError("contract_budget")
    data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_keys)
    if (not isinstance(data, dict) or set(data) != FIELDS
            or not all(isinstance(v, str) and v.strip() and len(v) <= 384 and v.isprintable()
                       for v in data.values())
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,62}", data["id"])
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", data["version"])
            or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_./-]{0,175}", data["reference"])
            or any(part in ("", ".", "..") for part in data["reference"].split("/"))):
        raise ContractError("contract_invalid")
    return data


def fingerprint(interfaces):
    raw = json.dumps(interfaces, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > 16384:
        raise ContractError("contract_budget")
    return hashlib.sha256(raw).hexdigest()


def snapshot(directory, builtins):
    """Return the exact model-visible interfaces and their content fingerprint."""
    interfaces = dict(builtins)
    if directory is not None:
        try:
            folder = Path(directory)
            if folder.is_symlink():
                raise ContractError("contract_invalid")
            with os.scandir(folder) as entries:
                for index, entry in enumerate(entries):
                    if index >= 64:
                        raise ContractError("contract_budget")
                    if not entry.name.endswith(".json"):
                        continue
                    if len(interfaces) - len(builtins) >= 12:
                        raise ContractError("contract_budget")
                    document = read_contract(Path(entry.path))
                    name = document["id"]
                    if name in interfaces:
                        raise ContractError("contract_conflict")
                    interfaces[name] = document
        except ContractError:
            raise
        except OSError as exc:
            raise ContractError("contract_unavailable") from exc
        except (ValueError, RecursionError) as exc:
            raise ContractError("contract_invalid") from exc
    interfaces = dict(sorted(interfaces.items()))
    return interfaces, fingerprint(interfaces)
