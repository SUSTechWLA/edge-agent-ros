"""Deployment-owned spatial calibration adapters. Requests can select installed data, never code."""
from dataclasses import dataclass
import hashlib
from importlib import import_module
from importlib.util import find_spec
import json
import math
import os
from pathlib import Path
import re


SOURCE_DIGESTS = {}


@dataclass(frozen=True)
class Method:
    spec: dict
    sha256: str

    def public(self):
        return {**{key:self.spec[key] for key in ("id","label","target","min_fit","min_holdout")},
                "clock_supported":bool(self.spec.get("clock_supported")),
                "guided_capture":bool(self.spec.get("clip")),"geometry_type":self.spec.get("geometry_type")}

    def call(self, role, *args):
        module, function = self.spec["entrypoints"][role].split(":")
        return getattr(import_module(module), function)(*args)

    def sample_role(self, index):
        n, h = self.spec["min_fit"], self.spec["min_holdout"]
        if index < n or index >= n+h and (index-n-h) % 2 == 0:
            return "fit"
        return "holdout"

    def sample_groups(self, samples):
        groups = {"fit": [], "holdout": []}
        for i, sample in enumerate(samples):
            groups[self.sample_role(i)].append(sample)
        return groups

    def metrics(self, values, *, partial=False):
        fields = self.spec.get("metric_fields", {})
        if (not isinstance(values, dict) or set(values)-set(fields) or
                not partial and set(values) != set(fields)):
            raise ValueError("board_metric_fields_mismatch")
        checked = {}
        for key in values:
            size=fields[key]
            items = [values[key]] if size == 1 else values[key]
            if (not isinstance(items, list) or len(items) != size or
                    any(type(v) not in (int, float) or not math.isfinite(v) or not .001 <= v <= 10 for v in items)):
                raise ValueError("invalid_board_metric:" + key)
            checked[key] = float(items[0]) if size == 1 else list(map(float, items))
        if self.spec.get("geometry_type") == "four_holes" and set(checked)==set(fields):
            from calibration.board_metrics import four_hole_lengths
            four_hole_lengths(checked)
        return checked


def directory():
    return Path(os.getenv("EDGE_CALIBRATION_METHOD_DIR", Path(__file__).parent/"station/methods"))


def load(path):
    raw = path.read_bytes()
    spec = json.loads(raw)
    if (spec["schema_version"] != 1 or spec["id"] != path.stem or
            not {"observe_lidar","fit","verify"} <= set(spec["entrypoints"]) <= {"observe_lidar","fit","verify","score_view_geometry"} or
            not all(type(spec[k]) is int and 1 <= spec[k] <= 32 for k in ("min_fit","min_holdout"))):
        raise ValueError("invalid_calibration_method_manifest")
    digest = hashlib.sha256(raw)
    modules = {v.split(":")[0] for v in spec["entrypoints"].values()} | set(spec.get("source_modules",[]))
    for module in sorted(modules):
        item = find_spec(module)
        if item is None or not item.origin:
            raise ValueError("uninstalled_calibration_algorithm:" + module)
        data = Path(item.origin).read_bytes()
        actual = hashlib.sha256(data).digest()
        if SOURCE_DIGESTS.setdefault(item.origin,actual)!=actual:
            raise ValueError("algorithm_source_changed_restart_provider:" + module)
        digest.update(data)
    return Method(spec,digest.hexdigest())


def installed():
    methods = {}
    for path in sorted(directory().glob("*.json")):
        try:
            methods[path.stem] = load(path)
        except (OSError,ImportError,ValueError,TypeError,KeyError):
            # An unavailable optional adapter cannot stop the other installed methods.
            continue
    return methods


def selected(profile):
    name = profile.get("calibration_method","checkerboard")
    if not isinstance(name,str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}",name):
        raise ValueError("invalid_calibration_method_id")
    try:
        return load(directory()/(name+".json"))
    except FileNotFoundError as exc:
        raise ValueError("calibration_method_unavailable:" + str(name)) from exc
