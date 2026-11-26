"""Read and requalify an installed numerical operator before evaluating a real input vector."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import stat

from agent import numeric_program


REF = re.compile(r"sha256:[0-9a-f]{64}")


def document(path, size):
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("operator.invalid_file")
        raw = stream.read(size + 1)
    if not 0 < len(raw) <= size:
        raise ValueError("operator.file_size")
    return raw


class Evaluator:
    def __init__(self, directory, policy, release_digest):
        self.directory = directory.resolve(strict=True)
        self.policy, self.release_digest = policy, release_digest
        if not self.directory.is_dir() or not REF.fullmatch(release_digest or ""):
            raise ValueError("operator.invalid_release")

    def evaluate(self, request):
        if not isinstance(request, dict) or set(request) != {"operator_digest", "values"}:
            raise ValueError("operator.invalid_request")
        ref, values = request["operator_digest"], request["values"]
        if (not isinstance(ref, str) or not REF.fullmatch(ref) or not isinstance(values, list) or not 1 <= len(values) <= 32
                or any(type(v) not in (float, int) or not math.isfinite(v) for v in values)):
            raise ValueError("operator.invalid_inputs")
        item = json.loads(document(self.directory / (ref[7:] + ".json"), 65536))
        program = item["program"]
        if item["operator_digest"] != ref or "sha256:" + numeric_program.digest(program) != ref:
            raise ValueError("operator.changed_artifact")
        raw = document(self.policy, 65536)
        policy = json.loads(raw)
        if policy.get("schema_version") != 1 or item["contract"] not in policy["contracts"]:
            raise ValueError("operator.unapproved_contract")
        check = numeric_program.verify(program, policy["contracts"][item["contract"]]["cases"])
        if any(item["validation"].get(k) != v for k, v in check.items()):
            raise ValueError("operator.verification_mismatch")
        result = numeric_program.execute(program, values)
        return {"derived":{"operator_digest":ref, "contract":item["contract"], "program":program,
                           "input_values":values, "value":result["value"], "input_sha256":result["input_sha256"],
                           "validation":check},
                "runtime_digest":self.release_digest, "policy_digest":"sha256:" + hashlib.sha256(raw).hexdigest()}


def serve(evaluator):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            status = 200
            try:
                if self.path != "/evaluate":
                    raise ValueError("operator.invalid_path")
                size = int(self.headers.get("Content-Length", "-1"))
                if not 1 <= size <= 8192:
                    raise ValueError("operator.request_size")
                response = evaluator.evaluate(json.loads(self.rfile.read(size)))
            except (ValueError, OSError, KeyError, TypeError, ArithmeticError) as error:
                status = 422
                message = str(error)
                response = {"error": message if re.fullmatch(r"operator[.][a-z_]+", message) else "operator.rejected"}
            raw = json.dumps(response, allow_nan=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    return Handler


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--operators", required=True, type=Path)
    cli.add_argument("--policy", required=True, type=Path)
    cli.add_argument("--port", default=8143, type=int)
    args = cli.parse_args()
    evaluator = Evaluator(args.operators, args.policy, os.environ.get("EDGE_OPERATOR_RUNTIME_DIGEST"))
    with HTTPServer(("0.0.0.0", args.port), serve(evaluator)) as server:
        server.serve_forever()
