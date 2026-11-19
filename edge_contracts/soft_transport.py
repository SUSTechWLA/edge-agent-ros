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
    path = os.getenv("EDGE_SOFT_SOCKET")
    if not path:
        raise SoftError("tool.binding_unavailable","original soft runtime unavailable")
    return request_at(path, action, **fields)


def request_at(path, action, *, timeout=12, **fields):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
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
        raise SoftError("plugin.transport", "plugin runtime unavailable or invalid reply") from error


def enabled():
    return bool(os.getenv("EDGE_SOFT_SOCKET"))
