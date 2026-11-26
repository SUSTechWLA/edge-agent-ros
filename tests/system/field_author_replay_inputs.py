"""Serve an immutable captured RGB-D observation to an isolated local author acceptance site."""
import argparse
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path

from edge_contracts.spatial_facts import check_batch, require
from perception.spatial_facts.frame_query import execute
from perception.spatial_facts.perception_server import handler


class Capture:
    def __init__(self, directory, profile, backend_digest):
        self.batch = json.loads((directory / "observations.json").read_bytes())
        self.batch["as_of_ns"] = int(self.batch["as_of_ns"])
        for sample in self.batch["samples"]:
            sample["stamp_ns"] = int(sample["stamp_ns"])
        check_batch(self.batch)
        profile_bytes = profile.read_bytes()
        self.profile = json.loads(profile_bytes)
        self.profile_digest = "sha256:" + hashlib.sha256(profile_bytes).hexdigest()
        self.backend_digest = backend_digest
        self.artifacts = {}
        for sample in self.batch["samples"]:
            if sample["source_id"] not in self.profile["mounts"]:
                continue
            ref = sample["artifact_ref"]
            suffix = ".png" if sample["media_type"] == "image/png" else ".json"
            data = (directory / (ref[7:] + suffix)).read_bytes()
            require(hashlib.sha256(data).hexdigest() == ref[7:], "replay.digest_mismatch")
            self.artifacts[ref] = data

    def evaluate(self, path, request):
        require(isinstance(request, dict) and request.get("actor_ref") == self.batch["actor_ref"],
                "perception.invalid_request")
        if path == "/observations" and set(request) == {"actor_ref"}:
            return self.batch
        require(path == "/query" and set(request) == {"program", "actor_ref", "observations"}
                and request["observations"] == self.batch, "perception.invalid_request")
        # The capture retains its source timestamps. Replay age is evaluated at capture time.
        result = execute(request["program"], self.batch, self.artifacts, self.profile,
                         now_ns=self.batch["as_of_ns"])
        return {"result": result, "backend_digest": self.backend_digest, "profile_digest": self.profile_digest}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--inputs", required=True, type=Path)
    cli.add_argument("--profile", required=True, type=Path)
    cli.add_argument("--digest", required=True)
    cli.add_argument("--role", required=True, choices=["facts", "query"])
    args = cli.parse_args()
    capture = Capture(args.inputs, args.profile, args.digest)
    port, endpoint = (8137, "/observations") if args.role == "facts" else (8142, "/query")
    with ThreadingHTTPServer(("0.0.0.0", port), handler(capture, endpoints=(endpoint,))) as server:
        server.serve_forever()
