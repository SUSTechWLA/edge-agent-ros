"""Replay the archived ROS-derived board measurements without Gazebo or an LLM."""
import gzip
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

from calibration.time_offset import estimate_offsets


def replay(proof):
    with ZipFile(proof / "autocalibration-20261125/run-04/recordings.zip") as archive:
        spatial = json.loads(archive.read("verified-extrinsics.json"))
    for case in ("moving", "static"):
        prefix = proof / "soft-sync-20261125" / case
        check = json.loads(prefix.with_suffix(".json").read_bytes())
        raw = gzip.decompress(prefix.with_name(case + "-planes.json.gz").read_bytes())
        assert sha256(raw).hexdigest() == check["capture_sha256"]
        assert spatial["revision"] == check["spatial"]["before"] == check["spatial"]["after"]
        try:
            estimate = estimate_offsets(json.loads(raw)["traces"], spatial["report"]["transforms"])
        except ValueError as exc:
            if case != "static" or "insufficient motion excitation" not in str(exc):
                raise
            print("Static target correctly rejected.")
            continue
        assert case == "moving"
        for role, expected in check["sources"].items():
            source = estimate["sources"][role]
            assert source["offset_ns"] == expected["offset_ns"]
            assert abs(source["offset_ns"] - expected["expected_offset_ns"]) <= source["uncertainty_ns"]
            print(role, source["offset_ns"], "ns")


if __name__ == "__main__":
    replay(Path(__file__).resolve().parents[2] / "artifacts/verification")
