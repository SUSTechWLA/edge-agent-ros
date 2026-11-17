"""A read-only HTTP tool for auditing the order of a supplied timestamp series."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

VERSION = "1"
MAX_BODY_BYTES = 65536
MAX_SAMPLES = 256


def audit(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"timestamps_ms"}:
        raise ValueError("需要一个 timestamps_ms 字符串参数")
    text = arguments["timestamps_ms"]
    if not isinstance(text, str):
        raise ValueError("timestamps_ms 应为 JSON 数组的文本")
    try:
        values = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ValueError("timestamps_ms 含无效 JSON") from exc
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_SAMPLES:
        raise ValueError(f"请提供 1 至 {MAX_SAMPLES} 个采样时间戳")
    if any(type(value) is not int or not -(2**63) <= value < 2**63 for value in values):
        raise ValueError("每个时间戳都必须是有符号 64 位整数毫秒值")

    backward = 0
    largest_gap = 0
    for previous, current in zip(values, values[1:]):
        delta = current - previous
        if delta < 0:
            backward += 1
        largest_gap = max(largest_gap, delta)
    return {"sample_count": len(values), "duplicate_count": len(values) - len(set(values)),
            "backward_count": backward, "max_forward_gap_ms": largest_gap}


def receipt(request):
    if not isinstance(request, dict):
        raise ValueError("expected a JSON request object")
    op = request.get("operation_id")
    if not isinstance(op, str) or not op.strip() or len(op) > 256:
        raise ValueError("expected a nonempty operation_id of at most 256 characters")
    if request.get("version") != VERSION:
        return {"operation_id": op, "state": "BLOCKED",
                "error": {"code": "timestamp.version_mismatch", "message": "提供者只支持版本 1"}}
    try:
        evidence = audit(request.get("arguments"))
    except ValueError as exc:
        return {"operation_id": op, "state": "BLOCKED",
                "error": {"code": "timestamp.invalid_series", "message": str(exc)}}
    return {"operation_id": op, "state": "SUCCEEDED", "evidence": evidence}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/invoke":
            self.reply(404, {"error": {"code": "timestamp.not_found", "message": "unknown endpoint"}})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY_BYTES:
                raise ValueError("request body must be between 1 and 65536 bytes")
            self.connection.settimeout(5)
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError("incomplete request body")
            result = receipt(json.loads(body.decode("utf-8")))
        except (ValueError, TimeoutError, RecursionError) as exc:
            self.reply(400, {"error": {"code": "timestamp.invalid_request", "message": str(exc)}})
            return
        self.reply(200, result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8124)
    options = parser.parse_args()
    with ThreadingHTTPServer((options.bind, options.port), Handler) as server:
        server.serve_forever()
