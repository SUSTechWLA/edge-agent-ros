"""Dispatch detection of a requested target using installed, operator-owned manifests."""
from importlib import import_module
from importlib.util import find_spec
from functools import lru_cache
import hashlib
import json
from pathlib import Path


def recognize(target, rgb, k, context, directory):
    # Only local deployment manifests select code. A caller supplies a semantic
    # target and an observation; it cannot select an import path.
    installed = []
    for path in sorted(Path(directory).glob("*.json")):
        raw = path.read_bytes()
        definition = json.loads(raw)
        if definition["schema_version"] != 1:
            raise ValueError("unsupported_detector_manifest")
        if target in definition["targets"]:
            installed.append((definition, hashlib.sha256(raw).hexdigest()))
    if len(installed) != 1:
        raise ValueError("ambiguous_or_unavailable_detector:" + str(target))
    spec, manifest_sha = installed[0]
    module, method = spec["entrypoint"].split(":")
    detector = getattr(import_module(module), method)
    rows = detector(target, rgb, k, context, spec.get("settings", {}))
    height, width = rgb.shape[:2]
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("invalid_target_detections")
    for row in rows:
        box = row["bbox_xyxy"]
        if (row["label"] != target or len(box) != 4 or any(type(i) is not int for i in box)
                or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
            raise ValueError("invalid_target_detection_box")
    # Deploy implementation modules as immutable code; only manifests are
    # hot-swappable. Record the source files for the selected implementation.
    detector_id = dict(id=spec["id"], version=spec["version"], manifest_sha256=manifest_sha,
                       sources={name:source_digest(name) for name in (module,*spec.get("source_modules",[]))})
    return dict(target=target, detector=detector_id,
                detections=rows, condition="target_visible" if rows else "target_absent")


@lru_cache(maxsize=128)
def source_digest(module):
    spec = find_spec(module)
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        raise ValueError("missing_detector_source")
    return hashlib.sha256(Path(spec.origin).read_bytes()).hexdigest()
