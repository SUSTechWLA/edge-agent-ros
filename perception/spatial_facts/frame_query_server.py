"""Read numeric regions from the exact image references selected by the facts service."""
import argparse
from http.server import HTTPServer
import os
from pathlib import Path
import time

from edge_contracts.spatial_facts import require
from perception.spatial_facts.frame_query import execute, parse
from perception.spatial_facts.perception_server import PublishedArtifacts, handler
from perception.spatial_facts.stationary_fusion import batch_samples, stamp


class FrameQuery(PublishedArtifacts):
    def evaluate(self, path, request):
        require(path == "/query" and isinstance(request, dict) and set(request) == {"actor_ref", "program", "observations"}
                and request["actor_ref"] == self.profile["actor_ref"] and self.ground_profile,
                "perception.invalid_request")
        program, batch = request["program"], request["observations"]
        parse(program)
        now = time.time_ns()
        samples = batch_samples(batch, self.profile)
        as_of = stamp(batch["as_of_ns"])
        require(as_of <= now and all(s["stamp_ns"] <= as_of for s in samples.values()),
                "perception.invalid_observation_time")
        ids = [self.grounding[k] for k in ("rgb_source", "depth_source", "calibration_source")]
        result = execute(program, batch, self.artifacts(samples, ids), self.ground_profile, now_ns=now)
        return {"result": result, "backend_digest": self.backend_digest, "profile_digest": self.profile_digest}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--profile", type=Path, required=True)
    cli.add_argument("--inputs", type=Path, default=Path("/inputs"))
    cli.add_argument("--port", type=int, default=8142)
    args = cli.parse_args()
    service = FrameQuery(args.profile, args.inputs, os.environ.get("EDGE_FRAME_QUERY_DIGEST"))
    with HTTPServer(("0.0.0.0", args.port), handler(service, endpoints=("/query",))) as server:
        server.serve_forever()
