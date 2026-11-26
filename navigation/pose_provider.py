"""DSH read-only tool endpoint for a geometric relative rover pose plan."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

from navigation.planar_pose import plan_pose


def invoke(wire):
    if not isinstance(wire, dict) or set(wire) - {"operation_id","version","arguments","trace"}:
        raise ValueError("invalid tool request")
    op = wire.get("operation_id")
    if not isinstance(op, str) or not 0 < len(op) <= 240 or not op.isprintable() or op.strip() != op:
        raise ValueError("invalid operation_id")
    if wire.get("version") != "1":
        return {"operation_id":op,"state":"BLOCKED","error":{"code":"tool.binding_unavailable"}}
    try:
        plan = plan_pose(wire.get("arguments"))
    except ValueError as exc:
        return {"operation_id":op,"state":"BLOCKED","error":{"code":"navigation.invalid_pose","message":str(exc)}}
    return {"operation_id":op, "state":"SUCCEEDED", "evidence":plan}


class Handler(BaseHTTPRequestHandler):
    def reply(self, result):
        raw = json.dumps(result, allow_nan=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            self.reply({"id":"plan_rover_pose","version":"1","ok":True})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/plan":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                self.send_error(413 if size > 16384 else 400)
                return
            self.connection.settimeout(3)
            result = invoke(json.loads(self.rfile.read(size)))
        except (ValueError, RecursionError):
            self.send_error(400)
            return
        except TimeoutError:
            self.send_error(408)
            return
        self.reply(result)


if __name__ == "__main__":
    with ThreadingHTTPServer(("",8151), Handler) as server:
        server.serve_forever()
