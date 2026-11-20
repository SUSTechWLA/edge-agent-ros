"""Compute from immutable site-published observations on a private facts network."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import stat
import time

from edge_contracts.spatial_facts import SpatialFactError, digest_ref, identifier, require
from perception.spatial_facts.stationary_fusion import batch_samples, fuse_stationary, policy, source_windows, stamp
from perception.spatial_facts.stationary_odom import stationary_window


FORMATS = {"livox": (".json", "application/json", 65536),
           "image": (".png", "image/png", 4*1024*1024),
           "calibration": (".json", "application/json", 8192)}
MOUNT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")
MAX_INDEX = 512 * 768


def read_bytes(path, limit):
    require(path.resolve(strict=True) == path.absolute() and stat.S_ISREG(path.stat().st_mode),
            "perception.invalid_store")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(0 < len(data) <= limit, "perception.document_size")
    return data


def parse_json(data):
    try:
        return json.loads(data)
    except (RecursionError, ValueError) as error:
        raise SpatialFactError("perception.invalid_json") from error


def path_parts(value):
    require(isinstance(value, str) and 1 <= len(value) <= 160, "perception.invalid_mount")
    parts = value.split("/")
    require(1 <= len(parts) <= 4 and all(MOUNT.fullmatch(s) for s in parts), "perception.invalid_mount")
    return parts


class PublishedArtifacts:
    """Read the exact bytes selected by published, actor-scoped ROS receipts."""
    def __init__(self, profile_path, inputs, backend_digest):
        raw = read_bytes(profile_path, 32768)
        profile = parse_json(raw)
        require(isinstance(profile, dict) and profile.get("schema_version") == 1
                and identifier(profile.get("actor_ref")) and digest_ref(backend_digest), "perception.invalid_release")
        self.profile, self.inputs, self.backend_digest = profile, inputs, backend_digest
        self.profile_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        mounts = profile.get("mounts")
        require(isinstance(mounts, dict) and 2 <= len(mounts) <= 32, "perception.invalid_mount")
        self.roots = {}
        for sid, item in mounts.items():
            require(identifier(sid) and isinstance(item, dict) and item.get("format") in FORMATS,
                    "perception.invalid_mount")
            root = inputs.joinpath(*path_parts(item.get("root")))
            require(all(path != root for path, _ in self.roots.values()), "perception.duplicate_mount")
            self.roots[sid] = (root, item["format"])
        g = self.grounding = profile.get("grounding")
        self.ground_profile = None
        if g is not None:
            require(isinstance(g, dict) and all(identifier(g.get(k)) and g[k] in self.roots and
                    self.roots[g[k]][1] == fmt for k, fmt in
                    (("rgb_source", "image"), ("depth_source", "image"), ("calibration_source", "calibration")))
                    and g["rgb_source"] != g["depth_source"], "perception.invalid_grounding")
            sources = profile.get("sources", {})
            require(isinstance(sources, dict), "perception.invalid_grounding")
            depth = g.get("depth_profile", sources.get(g["depth_source"]))
            require(isinstance(depth, dict) and depth.get("kind") == "depth16", "perception.invalid_grounding")
            self.ground_profile = {**profile, "sources": {**sources, g["depth_source"]: depth}}

    def artifacts(self, samples, ids):
        files = {}
        for sid in ids:
            require(sid in samples, "perception.source_missing")
            sample = samples[sid]
            root, fmt = self.roots[sid]
            suffix, media, limit = FORMATS[fmt]
            ref = sample["artifact_ref"]
            require(sample["media_type"] == media and digest_ref(ref), "perception.invalid_artifact")
            if fmt == "calibration":
                latest = parse_json(read_bytes(root/"observations.json", 16384))
                require(isinstance(latest, dict) and latest.get("actor_ref") == self.profile["actor_ref"] and
                        latest.get("basis") == "published" and isinstance(latest.get("samples"), list)
                        and len(latest["samples"]) == 1, "perception.calibration_binding_mismatch")
                cal = latest["samples"][0]
                require(isinstance(cal, dict) and all(cal.get(k) == sample[k] for k in
                        ("source_id", "actor_ref", "clock_id", "frame_id", "media_type", "calibration_ref", "artifact_ref"))
                        and cal["calibration_ref"] == ref and stamp(cal.get("stamp_ns")) >= sample["stamp_ns"],
                        "perception.calibration_binding_mismatch")
            else:
                index = parse_json(read_bytes(root/"index.json", MAX_INDEX))
                require(isinstance(index, dict) and index.get("schema_version") == 1 and index.get("source_id") == sid
                        and index.get("actor_ref") == self.profile["actor_ref"] and isinstance(index.get("frames"), list)
                        and len(index["frames"]) <= 512, "perception.invalid_index")
                keys = ("artifact_ref", "frame_id", "clock_id", "media_type", "calibration_ref")
                expected = {**{key: sample[key] for key in keys}, "stamp_ns": str(sample["stamp_ns"])}
                require(expected in index["frames"], "perception.sample_unavailable")
            data = read_bytes(root/"artifacts"/(ref[7:] + suffix), limit)
            require("sha256:" + hashlib.sha256(data).hexdigest() == ref, "perception.digest_mismatch")
            files[ref] = data
        return files

class Perception(PublishedArtifacts):
    def __init__(self, profile_path, inputs, backend_digest):
        super().__init__(profile_path, inputs, backend_digest)
        policy(self.profile)
        for sid, cfg in self.profile["sources"].items():
            require(isinstance(cfg, dict) and sid in self.roots and
                    (cfg.get("kind"), self.roots[sid][1]) in (("livox", "livox"), ("depth16", "image")),
                    "perception.invalid_mount")

    def witness(self, windows, batch):
        first = min(a for a, _ in windows.values())
        last = max(b for _, b in windows.values())
        profile = self.profile
        deadline = time.monotonic() + .42
        while True:
            history = parse_json(read_bytes(self.inputs/"witness"/"history.json", 512*1024))
            try:
                report = stationary_window(history, first, last, now_ns=time.time_ns(), actor_ref=profile["actor_ref"],
                                           frame_id=profile.get("witness_frame", "odom"),
                                           child_frame_id=profile["body_frame"], source_id=profile["witness_source"])
                break
            except SpatialFactError as error:
                if error.code != "sensing.odometry_coverage_missing" or time.monotonic() >= deadline:
                    raise
                time.sleep(.025)
        for sample in batch["samples"]:
            if sample.get("kind") == "position" and sample["source_id"] == profile["witness_source"]:
                require(sample["localization_epoch"] == report["localization_epoch"] and
                        sample["frame_id"] == report["frame_id"] and sample["clock_id"] == report["clock_id"],
                        "perception.witness_epoch_mismatch")
        return report

    def evaluate(self, path, request):
        require(path in ("/fuse", "/ground"), "perception.endpoint_unknown")
        expected = {"actor_ref", "observations"} | ({"target"} if path == "/ground" else set())
        require(isinstance(request, dict) and set(request) == expected and
                request["actor_ref"] == self.profile["actor_ref"], "perception.invalid_request")
        batch = request["observations"]
        samples = batch_samples(batch, self.profile)
        as_of = stamp(batch["as_of_ns"])
        require(as_of <= time.time_ns() and all(sample["stamp_ns"] <= as_of for sample in samples.values()),
                "perception.invalid_observation_time")
        if path == "/fuse":
            ids = list(self.profile["sources"])
            if any(cfg["kind"] == "depth16" for cfg in self.profile["sources"].values()):
                ids.extend(sid for sid, (_, fmt) in self.roots.items() if fmt == "calibration" and sid in samples)
            files = self.artifacts(samples, ids)
            windows = source_windows(batch, files, self.profile)
            witness = self.witness(windows, batch)
            result = fuse_stationary(batch, files, self.profile, witness, now_ns=time.time_ns())
        else:
            require(self.ground_profile is not None and isinstance(request["target"], str)
                    and 0 < len(request["target"]) <= 512, "perception.invalid_target")
            from perception.spatial_facts.visual_grounding import ground_target
            ids = [self.grounding[k] for k in ("rgb_source", "depth_source", "calibration_source")]
            files = self.artifacts(samples, ids)
            result = ground_target(request["target"], batch, files, self.ground_profile, now_ns=time.time_ns())
        return {"result": result, "backend_digest": self.backend_digest, "profile_digest": self.profile_digest}


def handler(compute, endpoints=("/fuse", "/ground")):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def respond(self, status, payload):
            data = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path not in endpoints:
                return self.respond(404, {"error": {"code": "perception.endpoint_unknown"}})
            try:
                length = self.headers.get_all("Content-Length") or []
                require(len(length) == 1 and re.fullmatch(r"[1-9][0-9]{0,4}", length[0]) and
                        int(length[0]) <= 32768 and "Transfer-Encoding" not in self.headers, "perception.invalid_request")
                data = self.rfile.read(int(length[0]))
                require(len(data) == int(length[0]), "perception.invalid_request")
                payload = compute.evaluate(self.path, parse_json(data))
            except SpatialFactError as error:
                status = 400 if error.code in ("perception.invalid_request", "perception.invalid_json",
                                              "perception.invalid_target") else 422
                return self.respond(status, {"error": {"code": error.code}})
            except OSError:
                return self.respond(503, {"error": {"code": "perception.input_unavailable"}})
            self.respond(200, payload)

        def log_message(self, fmt, *args):
            pass
    return Handler


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--profile", type=Path, required=True)
    cli.add_argument("--inputs", type=Path, default=Path("/inputs"))
    cli.add_argument("--port", type=int, default=8140)
    args = cli.parse_args()
    compute = Perception(args.profile, args.inputs, os.environ.get("EDGE_NAV_PERCEPTION_DIGEST"))
    HTTPServer(("0.0.0.0", args.port), handler(compute)).serve_forever()


if __name__ == "__main__":
    main()
