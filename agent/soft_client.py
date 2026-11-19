"""Bounded local protocol between the task owner and the optional soft plugin host."""
import json
import os
import socket
import struct


LIMIT = 1024 * 1024


class SoftError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.origin = "soft.runtime"


def encode(value):
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(payload) > LIMIT:
        raise SoftError("plugin.message_size", "soft request exceeds the wire budget")
    return struct.pack("!I", len(payload)) + payload


def request(action, **fields):
    path = os.environ["EDGE_SOFT_SOCKET"]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(12)
            conn.connect(path)
            conn.sendall(encode({"action": action, **fields}))
            with conn.makefile("rb") as stream:
                header = stream.read(4)
                size = int.from_bytes(header, "big")
                if len(header) != 4 or not 0 < size <= LIMIT:
                    raise ValueError("invalid soft reply size")
                data = stream.read(size)
                if len(data) != size:
                    raise ValueError("incomplete soft reply")
                reply = json.loads(data)
        if "error" in reply:
            raise SoftError(reply["error"], reply["detail"])
        return reply["value"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SoftError("plugin.transport", "soft runtime unavailable or invalid reply") from error


def enabled():
    return bool(os.getenv("EDGE_SOFT_SOCKET"))


def can_take_over(previous, candidate):
    if previous != candidate.get("_legacy_contract") or previous.get("effect") != "read":
        return False
    if previous.get("derived", {}).get("schema_version") == 2 and not previous.get("endpoint"):
        return True
    name = previous["id"]
    return (name in ("read_session","evidence_schema","query_evidence") and
            previous.get("endpoint") == "http://evidence:8125/invoke?tool=" + name)


def catalog(legacy):
    view = request("catalog")
    # Tombstones continue to own a migrated name after removal. Only explicitly
    # authorized v1 handoffs can shadow an installed legacy tool.
    tools = {name: cap for name, cap in legacy.items() if name not in view["claims"]}
    for cap in view["tools"]:
        previous = legacy.get(cap["id"])
        if previous is None or can_take_over(previous, cap):
            tools[cap["id"]] = cap
    return tools
