"""ROAM-style semantic-spatial vocabulary, sliding memory, and monotonic cost synthesis."""
from collections import deque
import math
import re

LABEL_RE = re.compile(r"[a-z][a-z0-9_]{1,47}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
DIRECTIONAL_TOKENS = frozenset({
    "left", "right", "ahead", "behind", "front", "rear",
    "forward", "backward", "near", "far", "north", "south",
    "east", "west", "upper", "lower",
})
NOISE_LABELS = frozenset({
    "sky", "cloud", "clouds", "shadow", "shadows", "sunlight", "glare",
})
OTHER_FALLBACK = {"risk": 1.0, "traversable": 0.0, "preferred": 0.0}
CHANNELS = ("risk", "traversable", "preferred")


def _unit_scalar(value):
    return type(value) in (int, float) and math.isfinite(value) and 0.0 <= value <= 1.0


def validate_semantic_prior(vocabulary, *, max_labels=16):
    """Validate non-directional snake_case terrain/object concepts and inject conservative 'other'."""
    if not isinstance(vocabulary, dict):
        raise ValueError("semantic_prior_not_object")
    cleaned = {}
    for raw_label, spec in vocabulary.items():
        if not isinstance(raw_label, str) or not LABEL_RE.fullmatch(raw_label):
            raise ValueError(f"invalid_semantic_label:{raw_label!r}")
        if raw_label == "other":
            continue
        tokens = raw_label.split("_")
        if any(tok in DIRECTIONAL_TOKENS for tok in tokens):
            raise ValueError(f"directional_label_forbidden:{raw_label}")
        if raw_label in NOISE_LABELS or any(tok in NOISE_LABELS for tok in tokens):
            raise ValueError(f"non_surface_noise_label_forbidden:{raw_label}")
        if not isinstance(spec, dict) or set(spec) != set(CHANNELS):
            raise ValueError(f"invalid_semantic_channels:{raw_label}")
        if not all(_unit_scalar(spec[ch]) for ch in CHANNELS):
            raise ValueError(f"semantic_scalar_out_of_range:{raw_label}")
        cleaned[raw_label] = {ch: round(float(spec[ch]), 4) for ch in CHANNELS}
    if not 1 <= len(cleaned) <= max_labels:
        raise ValueError("semantic_label_budget_exceeded")
    cleaned["other"] = dict(OTHER_FALLBACK)
    return cleaned


class SemanticPriorMemory:
    """Sliding N-frame memory with moving-average smoothing and TTL expiration."""

    def __init__(self, *, window=3, ttl_ns=15_000_000_000):
        if type(window) is not int or not 1 <= window <= 10:
            raise ValueError("invalid_memory_window")
        if type(ttl_ns) is not int or ttl_ns <= 0:
            raise ValueError("invalid_memory_ttl")
        self.window = window
        self.ttl_ns = ttl_ns
        self._frames = deque(maxlen=window)

    def update(self, *, stamp_ns, source_ref, vocabulary, memory_note=""):
        if type(stamp_ns) is not int or stamp_ns <= 0:
            raise ValueError("invalid_prior_stamp")
        if self._frames and stamp_ns < self._frames[-1]["stamp_ns"]:
            raise ValueError("non_monotonic_prior_stamp")
        if not isinstance(source_ref, str) or not DIGEST_RE.fullmatch(source_ref):
            raise ValueError("invalid_prior_source_ref")
        if not isinstance(memory_note, str) or len(memory_note) > 400:
            raise ValueError("invalid_prior_memory_note")
        validated = validate_semantic_prior(vocabulary)
        self._frames.append({
            "stamp_ns": stamp_ns,
            "source_ref": source_ref,
            "note": memory_note.strip(),
            "vocabulary": validated,
        })
        return self.snapshot(stamp_ns)

    def snapshot(self, now_ns):
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("invalid_query_stamp")
        if not self._frames:
            return {
                "active": False,
                "stale": True,
                "vocabulary": {"other": dict(OTHER_FALLBACK)},
                "memory": [],
                "source_ref": None,
                "stamp_ns": 0,
                "valid_until_ns": 0,
            }
        latest = self._frames[-1]
        valid_until = latest["stamp_ns"] + self.ttl_ns
        stale = now_ns > valid_until or now_ns < latest["stamp_ns"]
        labels = sorted({k for f in self._frames for k in f["vocabulary"] if k != "other"})
        smoothed = {}
        for label in labels:
            appearances = [f["vocabulary"][label] for f in self._frames if label in f["vocabulary"]]
            smoothed[label] = {
                ch: round(sum(item[ch] for item in appearances) / len(appearances), 4)
                for ch in CHANNELS
            }
        smoothed["other"] = dict(OTHER_FALLBACK)
        return {
            "active": not stale,
            "stale": stale,
            "vocabulary": smoothed,
            "memory": [f["note"] for f in self._frames if f["note"]],
            "source_ref": latest["source_ref"],
            "stamp_ns": latest["stamp_ns"],
            "valid_until_ns": valid_until,
        }


def synthesize_semantic_field(logits_by_cell, vocabulary, *, temperature=1.0):
    """Temperature-scaled softmax expected value synthesis (ROAM Eq. 1-3)."""
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("invalid_softmax_temperature")
    vocab = validate_semantic_prior(vocabulary)
    labels = tuple(vocab.keys())
    if not isinstance(logits_by_cell, list) or not 1 <= len(logits_by_cell) <= 262144:
        raise ValueError("invalid_logits_grid")
    risk_out, trav_out, pref_out = [], [], []
    inv_t = 1.0 / float(temperature)
    for cell_logits in logits_by_cell:
        if not isinstance(cell_logits, dict):
            raise ValueError("cell_logits_not_object")
        raw = []
        for label in labels:
            val = cell_logits.get(label, 0.0 if label == "other" else -12.0)
            if type(val) not in (int, float) or not math.isfinite(val):
                raise ValueError("non_finite_logit")
            raw.append(float(val) * inv_t)
        peak = max(raw)
        weights = [math.exp(v - peak) for v in raw]
        norm = sum(weights)
        probs = [w / norm for w in weights]
        risk_out.append(round(sum(p * vocab[l]["risk"] for p, l in zip(probs, labels)), 4))
        trav_out.append(round(sum(p * vocab[l]["traversable"] for p, l in zip(probs, labels)), 4))
        pref_out.append(round(sum(p * vocab[l]["preferred"] for p, l in zip(probs, labels)), 4))
    return {"risk": risk_out, "traversable": trav_out, "preferred": pref_out}


def validate_semantic_layers(layers, cell_count, *, now_ns=None):
    """Validate grid-aligned semantic cost channels and evaluate TTL freshness."""
    if not isinstance(layers, dict) or type(cell_count) is not int or cell_count <= 0:
        raise ValueError("semantic_layers_invalid")
    stamp = layers.get("stamp_ns")
    valid_until = layers.get("valid_until_ns")
    source_ref = layers.get("source_ref")
    if (type(stamp) is not int or stamp <= 0 or type(valid_until) is not int
            or valid_until < stamp or not isinstance(source_ref, str)
            or not DIGEST_RE.fullmatch(source_ref)):
        raise ValueError("semantic_layers_provenance_invalid")

    def _channel(name, default=None):
        seq = layers.get(name)
        if seq is None and default is not None:
            return [default] * cell_count
        if not isinstance(seq, list) or len(seq) != cell_count or not all(_unit_scalar(v) for v in seq):
            raise ValueError(f"semantic_layer_{name}_invalid")
        return [float(v) for v in seq]

    risk = _channel("risk")
    traversable = _channel("traversable", default=1.0)
    preferred = _channel("preferred", default=0.5)
    risk_block = float(layers.get("risk_block", 0.80))
    min_traversable = float(layers.get("min_traversable", 0.35))
    risk_weight = float(layers.get("risk_weight", 4.0))
    pref_weight = float(layers.get("pref_weight", 2.5))
    if (not 0.05 <= risk_block <= 1.0 or not 0.0 <= min_traversable <= 1.0
            or not 0.0 <= risk_weight <= 32.0 or not 0.0 <= pref_weight <= 32.0):
        raise ValueError("semantic_layer_thresholds_invalid")
    stale = bool(now_ns is not None and (type(now_ns) is not int or now_ns > valid_until or now_ns < stamp))
    if now_ns is not None and type(now_ns) is not int:
        raise ValueError("semantic_query_stamp_invalid")
    return {
        "stale": stale,
        "source_ref": source_ref,
        "stamp_ns": stamp,
        "valid_until_ns": valid_until,
        "risk": risk,
        "traversable": traversable,
        "preferred": preferred,
        "risk_block": risk_block,
        "min_traversable": min_traversable,
        "risk_weight": risk_weight,
        "pref_weight": pref_weight,
    }


def monotonic_cell_admitted(geometric_clear, *, risk=0.0, traversable=1.0, stale=False,
                            risk_block=0.80, min_traversable=0.35):
    """Monotonic safety floor: visual semantics never erase a geometric obstacle."""
    if not geometric_clear:
        return False
    if stale:
        return True
    return traversable >= min_traversable and risk < risk_block


def conditional_preference_cost(*, risk, preferred, robot_preferred,
                                risk_weight=4.0, pref_weight=2.5, pref_threshold=0.5):
    """ROAM conditional virtual height/cost increment: lifts off-trail penalty during detours."""
    on_preferred = 1.0 if robot_preferred >= pref_threshold else 0.0
    return 1.0 + risk_weight * float(risk) + on_preferred * pref_weight * (1.0 - float(preferred))


def corridor_semantic_score(*, clearance_margin_m, path_margin_px, lidar_gap=0.0,
                            fills_missing_side=False, new_rgb_cells=0, robot_on_preferred=False):
    """Project continuous corridor risk and conditional preference cost for active view planning."""
    clearance_risk = max(0.0, min(1.0, (0.35 - float(clearance_margin_m)) / 0.35))
    margin_risk = max(0.0, min(1.0, (96.0 - float(path_margin_px)) / 96.0))
    gap_risk = max(0.0, min(1.0, float(lidar_gap) / 0.20))
    risk = round(min(0.79, 0.50 * clearance_risk + 0.35 * margin_risk + 0.15 * gap_risk), 4)
    preferred = round(min(1.0, (0.65 if fills_missing_side else 0.25) + min(0.35, 0.08 * max(0, int(new_rgb_cells)))), 4)
    cost = round(conditional_preference_cost(
        risk=risk,
        preferred=preferred,
        robot_preferred=1.0 if robot_on_preferred else 0.0,
    ), 4)
    return {
        "corridor_risk": risk,
        "preferred_zone": preferred,
        "semantic_cost": cost,
    }
