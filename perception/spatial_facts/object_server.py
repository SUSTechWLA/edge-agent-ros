"""Local RGB-D object location backed by a private single-image inference socket."""
import argparse
import hashlib
from http.server import HTTPServer
import json
import os
from pathlib import Path
import socket
import struct
import time

from edge_contracts.spatial_facts import SpatialFactError, digest_ref, require
from perception.spatial_facts.perception_server import PublishedArtifacts, handler
from perception.spatial_facts.stationary_fusion import batch_samples, stamp


MAX_PNG = 1024 * 1024
MAX_RESULT = 512 * 1024


def read_message(stream, limit):
    header = stream.read(4)
    require(len(header) == 4, "object.model_protocol")
    count, = struct.unpack("!I", header)
    require(0 < count <= limit, "object.model_protocol")
    payload = stream.read(count)
    require(len(payload) == count, "object.model_protocol")
    return payload


def write_message(stream, raw, limit):
    require(0 < len(raw) <= limit, "object.model_protocol")
    stream.write(struct.pack("!I", len(raw)) + raw)
    stream.flush()


def socket_prediction(address, rgb, model_ref):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(6.)
            peer.connect(os.fspath(address))
            with peer.makefile("rwb") as stream:
                write_message(stream, rgb, MAX_PNG)
                record = json.loads(read_message(stream, MAX_RESULT))
    except (OSError, ValueError, UnicodeError) as exc:
        raise SpatialFactError("object.model_unavailable") from exc
    require(isinstance(record, dict), "object.model_protocol")
    if "error" in record:
        raise SpatialFactError("object.model_inference_failed")
    require(record.get("model_ref") == model_ref and record.get("rgb_ref") == "sha256:" + hashlib.sha256(rgb).hexdigest(),
            "object.model_binding_mismatch")
    return record


class ObjectPerception(PublishedArtifacts):
    def __init__(self, profile, inputs, release, address):
        super().__init__(profile, inputs, release)
        require(self.ground_profile and digest_ref(self.grounding.get("object_model_ref")),
                "perception.invalid_grounding")
        self.address = address

    def evaluate(self, path, request):
        require(path == "/locate" and isinstance(request, dict) and set(request) == {"actor_ref","target","observations"}
                and request["actor_ref"] == self.profile["actor_ref"] and isinstance(request["target"], str)
                and 0 < len(request["target"]) <= 128, "perception.invalid_request")
        now = time.time_ns()
        batch = request["observations"]
        samples = batch_samples(batch, self.profile)
        as_of = stamp(batch["as_of_ns"])
        require(as_of <= now and all(s["stamp_ns"] <= as_of for s in samples.values()), "perception.invalid_observation_time")
        ids = [self.grounding[k] for k in ("rgb_source", "depth_source", "calibration_source")]
        files = self.artifacts(samples, ids)
        from perception.spatial_facts.object_grounding import locate_object
        def predict(rgb):
            return socket_prediction(self.address, rgb, self.grounding["object_model_ref"])
        result = locate_object(request["target"], batch, files, self.ground_profile, now_ns=now, predict=predict)
        return {"result": result, "backend_digest": self.backend_digest, "profile_digest": self.profile_digest}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--profile", type=Path, required=True)
    cli.add_argument("--inputs", type=Path, default=Path("/inputs"))
    cli.add_argument("--socket", type=Path, required=True)
    cli.add_argument("--port", type=int, default=8141)
    args = cli.parse_args()
    service = ObjectPerception(args.profile, args.inputs, os.environ.get("EDGE_OBJECT_PERCEPTION_DIGEST"), args.socket)
    with HTTPServer(("0.0.0.0", args.port), handler(service, endpoints=("/locate",))) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
