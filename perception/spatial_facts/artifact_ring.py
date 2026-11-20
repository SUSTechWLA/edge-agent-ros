"""Single-writer bounded content-addressed storage for site-published spatial evidence."""
import hashlib
import json
import os
import re

from edge_contracts.spatial_facts import MEDIA_TYPE, check_sample, digest_ref, identifier, ns, require


def replace_bytes(path, data):
    # One writer owns both directories. A fixed temporary filename keeps repeated crashes bounded.
    temporary = path.parent / ".image-pending"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(temporary, flags, 0o600), "wb") as stream:
        stream.write(data)
    os.replace(temporary, path)


class ArtifactRing:
    """Single-writer volume. Readers copy by receipt digest before the frame leaves the ring."""

    def __init__(self, directory, actor, source, *, media_type, suffix, retain=96, max_bytes=1024*1024):
        require(identifier(actor) and identifier(source), "sensing.invalid_request")
        require(MEDIA_TYPE.fullmatch(media_type) and re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) and
                type(retain) is int and 2 <= retain <= 512 and type(max_bytes) is int and 1 <= max_bytes <= 4*1024*1024,
                "sensing.invalid_artifact_store")
        self.media_type, self.suffix, self.retain, self.max_bytes = media_type, suffix, retain, max_bytes
        self.root, self.actor, self.source = directory, actor, source
        self.store = directory / "artifacts"
        self.store.mkdir(exist_ok=True)
        require(self.store.resolve(strict=True) == self.store.absolute(), "sensing.invalid_artifact_directory")
        self.index = self.root / "index.json"
        self.frames = []
        if self.index.exists():
            require(self.index.resolve(strict=True) == self.index.absolute() and self.index.is_file(),
                    "sensing.invalid_artifact_index")
            with self.index.open("rb") as stream:
                raw = stream.read(self.retain * 768 + 1)
            require(len(raw) <= self.retain * 768, "sensing.invalid_artifact_index")
            row = json.loads(raw)
            require(isinstance(row, dict) and row.get("schema_version") == 1 and row.get("actor_ref") == actor
                    and row.get("source_id") == source and isinstance(row.get("frames"), list)
                    and len(row["frames"]) <= self.retain, "sensing.invalid_artifact_index")
            for item in row["frames"]:
                require(isinstance(item, dict) and digest_ref(item.get("artifact_ref")) and
                        isinstance(item.get("stamp_ns"), str) and re.fullmatch(r"[1-9][0-9]{0,18}", item["stamp_ns"])
                        and ns(int(item["stamp_ns"])), "sensing.invalid_artifact_index")
            self.frames = row["frames"]
        self.prune()

    def prune(self):
        keep = {item["artifact_ref"][7:] + self.suffix for item in self.frames}
        for path in self.store.glob("*" + self.suffix):
            if re.fullmatch(r"[0-9a-f]{64}", path.stem) and path.name not in keep:
                path.unlink()

    def publish(self, sample, data):
        check_sample(sample, self.actor)
        require(sample["source_id"] == self.source and sample["media_type"] == self.media_type
                and len(data) <= self.max_bytes and sample["artifact_ref"] == "sha256:" + hashlib.sha256(data).hexdigest(),
                "sensing.invalid_artifact")
        if self.frames and int(self.frames[-1]["stamp_ns"]) >= sample["stamp_ns"]:
            return
        path = self.store / (sample["artifact_ref"][7:] + self.suffix)
        if path.exists() or path.is_symlink():
            require(path.resolve(strict=True) == path.absolute() and path.is_file() and path.stat().st_size == len(data)
                    and path.read_bytes() == data, "sensing.artifact_conflict")
        else:
            replace_bytes(path, data)
        frame = {key: sample[key] for key in ("frame_id", "clock_id", "artifact_ref", "media_type", "calibration_ref")}
        frames = (self.frames + [{**frame, "stamp_ns": str(sample["stamp_ns"])}])[-self.retain:]
        index = {"schema_version": 1, "actor_ref": self.actor, "source_id": self.source, "frames": frames}
        batch = {"schema_version": 1, "basis": "published", "clock_id": sample["clock_id"],
                 "actor_ref": self.actor, "samples": [sample]}
        replace_bytes(self.index, json.dumps(index).encode())
        self.frames = frames
        replace_bytes(self.root / "observations.json", json.dumps(batch).encode())
        self.prune()
