"""One bounded upstream exchange; this helper gets request data through stdin only."""
import http.client
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

DEEPSEEK = "https://api.deepseek.com/chat/completions"
MAX_REPLY = 256 * 1024


def fetch(url, body, headers):
    target = urlsplit(url)
    connection_class = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(target.hostname, target.port, timeout=75)
    try:
        connection.request("POST", target.path, body=json.dumps(body, allow_nan=False).encode(), headers=headers)
        reply = connection.getresponse()
        status = reply.status
        if status != 200:
            # Avoid returning upstream error bodies or following redirects with authorization.
            code = {401:"auth_failed", 403:"forbidden", 402:"balance", 429:"rate_limit",
                    408:"timeout", 504:"timeout"}.get(status, "upstream_status")
            return {"error":code,"upstream_status":status}
        raw = reply.read(MAX_REPLY + 1)
        if len(raw) > MAX_REPLY:
            return {"error":"reply_too_large"}
        return {"reply":json.loads(raw)}
    finally:
        connection.close()


def exchange(provider, path, body):
    headers = {"Content-Type":"application/json"}
    if provider == "deepseek":
        try:
            with Path("/run/secrets/deepseek-api-key").open("rb") as handle:
                key = handle.read(513).strip().decode("ascii")
            if not 1 <= len(key) <= 510 or any(ord(ch)<33 or ord(ch)>126 for ch in key):
                raise ValueError("key")
        except (OSError, ValueError):
            return {"error":"key_unavailable"}
        headers["Authorization"] = "Bearer " + key
        url = DEEPSEEK
    elif provider == "ollama" and path in ("/api/chat", "/api/generate"):
        local = os.getenv("EDGE_MODEL_LOCAL_URL", "http://model:11434")
        target = urlsplit(local)
        if (target.scheme not in ("http", "https") or not target.hostname or target.username is not None
                or target.password is not None or target.query or target.fragment or target.path not in ("","/")):
            return {"error":"local_url_invalid"}
        url = local.rstrip("/") + path
    else:
        return {"error":"route_invalid"}
    try:
        return fetch(url, body, headers)
    except TimeoutError:
        return {"error":"timeout"}
    except (OSError, http.client.HTTPException):
        return {"error":"upstream_unavailable"}
    except (ValueError, UnicodeError, RecursionError):
        return {"error":"reply_invalid"}


if __name__ == "__main__":
    try:
        result = exchange(**json.load(sys.stdin))
        encoded = json.dumps(result, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, OSError, RecursionError):
        encoded = b'{"error":"reply_invalid"}'
    sys.stdout.buffer.write(encoded)
