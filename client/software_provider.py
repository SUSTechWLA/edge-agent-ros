"""HTTP read Tool for a bounded subset of a one-function Python source file."""
import argparse
import ast
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import threading
import unicodedata


class Invalid(ValueError):
    """Stable code suitable for a public diagnostic."""


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_bytes(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise Invalid("software.file_size")
    return data


def package(folder):
    source = file_bytes(folder / "candidate.py", 2048)
    contract = json.loads(file_bytes(folder / "contract.json", 2048))
    if (not isinstance(contract, dict) or set(contract) != {"id","description","prefix"}
            or not isinstance(contract["id"], str) or not re.fullmatch(r"software\.[a-z][a-z0-9_]{0,47}",contract["id"])
            or any(not isinstance(contract[k], str) or not 1 <= len(contract[k]) <= 160 or not contract[k].isprintable()
                   for k in ("description","prefix"))
            or any(c in unicodedata.normalize("NFKC",contract["prefix"]) for c in "{}")):
        raise Invalid("software.contract_invalid")
    identity = {"source_sha256":digest(source), "provider_sha256":digest(Path(__file__).read_bytes()),
                "contract":contract}
    version = digest(json.dumps(identity,sort_keys=True,separators=(",",":")).encode())
    return source, identity, version


def program(source):
    """Translate the parsed syntax to fixed operations. The candidate is never imported."""
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (ValueError, SyntaxError, RecursionError) as exc:
        raise Invalid("software.syntax") from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0],ast.FunctionDef):
        raise Invalid("software.signature")
    fn = tree.body[0]
    args = fn.args
    if (fn.name != "transform" or fn.decorator_list or fn.returns or getattr(fn,"type_params", [])
            or args.posonlyargs or args.kwonlyargs or args.defaults or args.vararg or args.kwarg
            or len(args.args) != 1 or args.args[0].arg != "text" or args.args[0].annotation
            or len(fn.body) != 1 or not isinstance(fn.body[0],ast.Return)):
        raise Invalid("software.signature")
    node, stages = fn.body[0].value, []
    while isinstance(node,ast.Call):
        if len(stages) >= 8 or node.keywords or not isinstance(node.func,ast.Attribute):
            raise Invalid("software.expression")
        name = node.func.attr
        if name == "count":
            if (len(node.args)!=1 or not isinstance(node.args[0],ast.Constant)
                    or type(node.args[0].value) is not str or not 1<=len(node.args[0].value)<=64):
                raise Invalid("software.expression")
            stages.append((name,node.args[0].value))
        elif name in ("casefold","lower","upper","strip","split") and not node.args:
            stages.append((name,None))
        else:
            raise Invalid("software.expression")
        node = node.func.value
    if not isinstance(node,ast.Name) or node.id != "text":
        raise Invalid("software.expression")
    result_type = "text"
    for name,_ in reversed(stages):
        if result_type != "text" and not (result_type=="words" and name=="count"):
            raise Invalid("software.expression")
        result_type = {"split":"words","count":"number"}.get(name,"text")
    if result_type=="words":
        raise Invalid("software.result_type")
    return list(reversed(stages))


def evaluate(stages, text):
    if not isinstance(text,str) or len(text.encode("utf-8")) > 4096:
        raise Invalid("software.input_invalid")
    value = text
    for method, argument in stages:
        if method == "count":
            value = value.count(argument)
        elif method == "split":
            value = value.split()
        else:
            value = {"casefold":str.casefold,"lower":str.lower,"upper":str.upper,"strip":str.strip}[method](value)
    if isinstance(value,str) and len(value.encode("utf-8")) > 4096:
        raise Invalid("software.result_size")
    return value


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        operation = None
        try:
            size = int(self.headers.get("Content-Length","0"))
            if self.path != "/invoke" or not 1 <= size <= 32768:
                raise Invalid("software.request_invalid")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise Invalid("software.request_invalid")
            body = json.loads(raw)
            if not isinstance(body,dict) or set(body)!={"operation_id","version","arguments"}:
                raise Invalid("software.request_invalid")
            operation = body["operation_id"]
            if not isinstance(operation,str) or not 1<=len(operation)<=240:
                operation = None
                raise Invalid("software.operation_invalid")
            if body["version"] != self.server.version:
                raise Invalid("software.version_mismatch")
            args = body["arguments"]
            if not isinstance(args,dict) or set(args)!={"text"}:
                raise Invalid("software.input_invalid")
            value = evaluate(self.server.stages,args["text"])
            result = {"operation_id":operation,"state":"SUCCEEDED","evidence":{
                "value":value,"definition_version":self.server.version,
                "input_sha256":digest(args["text"].encode("utf-8"))}}
        except (ValueError,RecursionError) as exc:
            code = str(exc) if isinstance(exc,Invalid) else "software.request_invalid"
            result = {"operation_id":operation,"state":"BLOCKED","code":code,"detail":code}
        raw = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Server(ThreadingHTTPServer):
    request_seconds = 5

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        # Cover headers and uploads as well as the response. An idle timeout alone
        # resets on each read and cannot bound the lifetime of a connection.
        def expire():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(self.request_seconds, expire)
        try:
            request.settimeout(3)
            timer.start()
            super().process_request_thread(request, address)
        finally:
            timer.cancel()
            self.slots.release()

    def handle_error(self, request, address):
        # A peer leaving during an upload or write is an ordinary transport failure.
        import sys
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, address)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8125)
    args = parser.parse_args()
    source, _, version = package(args.package)
    stages = program(source)
    with Server(("0.0.0.0",args.port),Handler) as server:
        server.stages, server.version = stages, version
        server.serve_forever()
