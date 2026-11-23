"""One-shot, read-only evaluation of a caller's current robot-frame observation."""
from collections import OrderedDict
from contextlib import contextmanager
import threading
import time

try:
    from .planner import plan, stamp
except ImportError:
    from planner import plan, stamp


VERSION = "1.1.0"
SERVICE = "planning.local_path"
MAX_AGE_NS = 250_000_000
FUTURE_TOLERANCE_NS = 50_000_000
SNAPSHOT_KEYS = {"source_id", "base_frame", "scan_stamp_ns", "calibration_ref", "profile_id", "clock_id", "rays",
                 "hits_body_m", "odom", "source_evidence", "map_anchor"}


class EvaluationError(ValueError):
    def __init__(self, status, code, detail):
        super().__init__(detail)
        self.status = status
        self.code = code

    def response(self):
        return {"status": "blocked", "error": {"code": self.code, "message": str(self)},
                "motion_authorized": False, "path_body_m": [], "planned_endpoint": None}


def require_static(snapshot):
    if not isinstance(snapshot, dict):
        raise EvaluationError(400, "planning.invalid", "expected a sensor snapshot")
    if set(snapshot) - SNAPSHOT_KEYS:
        raise EvaluationError(422, "planning.unsupported", "unknown observation fields for a static planar strategy")
    if not all(isinstance(snapshot.get(k), str) and 0 < len(snapshot[k]) <= 256
               for k in ("source_id", "base_frame", "calibration_ref")):
        raise EvaluationError(400, "planning.invalid", "missing source, body frame or calibration identity")


class SnapshotEvaluator:
    """A pinned strategy and capacity of one per process. The caller owns task-specific preferences."""
    def __init__(self, profile, *, solve=plan, wall=time.time_ns, monotonic=time.monotonic_ns):
        self.profile = profile
        self.solve = solve
        self.wall = wall
        self.monotonic = monotonic
        self.slot = threading.Lock()
        self.newest = OrderedDict()

    @contextmanager
    def computation(self):
        if not self.slot.acquire(blocking=False):
            raise EvaluationError(429, "planning.busy", "another snapshot is being evaluated; submit the newest frame")
        try:
            yield
        finally:
            self.slot.release()

    def archived_task_plan(self, snapshot):
        # /invoke owns its observation interval and receipt. Only the CPU-bound
        # computation shares capacity with machine clients, never the sampling wait.
        require_static(snapshot)
        # Historical site files without the new frame allowlist are still usable
        # by this tool, whose upstream URL is fixed at deployment.
        if "supported_body_frames" in self.profile:
            self.check_chassis(snapshot)
        with self.computation():
            return self.solve(snapshot, self.profile)

    def check_chassis(self, snap):
        frames = self.profile.get("supported_body_frames", [])
        if not isinstance(frames, list) or snap["base_frame"] not in frames:
            raise EvaluationError(422, "planning.frame_mismatch", "observation requires a site's configured chassis frame")
        if "profile_id" in snap and snap["profile_id"] != self.profile["profile_id"]:
            raise EvaluationError(409, "planning.profile_mismatch", "sensor and configured chassis profiles differ")

    def evaluate(self, request):
        start = self.monotonic()
        fields = {"schema_version", "provider_version", "profile_id", "clock_id", "objective", "snapshot", "budget_ms"}
        if (not isinstance(request, dict) or set(request) - fields or type(request.get("schema_version")) is not int
                or request["schema_version"] != 1):
            raise EvaluationError(400, "planning.invalid", "invalid local-path request envelope")
        if request.get("provider_version") != VERSION:
            raise EvaluationError(409, "planning.binding_mismatch", "expected the bound local-path provider version")
        snap = request.get("snapshot")
        require_static(snap)
        self.check_chassis(snap)
        if "profile_id" in request and request["profile_id"] != self.profile["profile_id"]:
            raise EvaluationError(409, "planning.profile_mismatch", "requested chassis profile differs from this site")
        rays = snap.get("rays")
        hits = snap.get("hits_body_m", [])
        if not isinstance(rays, list) or not isinstance(hits, list):
            raise EvaluationError(400, "planning.invalid", "expected planar rays and point arrays")
        if len(rays) > 2048 or len(hits) > 4096:
            raise EvaluationError(413, "planning.input_budget", "snapshot exceeds the fast evaluation point budget")
        if request.get("clock_id") != "unix" or snap.get("clock_id", "unix") != "unix":
            raise EvaluationError(422, "planning.unsupported", "live evaluations require site Unix acquisition time")
        aim = request.get("objective")
        if (not isinstance(aim, dict) or set(aim) - {"kind", "frame_id", "goal_body_m", "preferred_side"}
                or aim.get("kind") != "static_local_path"):
            raise EvaluationError(422, "planning.unsupported", "a static planar objective is required")
        if aim.get("frame_id") != snap["base_frame"]:
            raise EvaluationError(422, "planning.frame_mismatch", "objective and observation require a site's configured chassis frame")
        budget = request.get("budget_ms", 50)
        if type(budget) is not int or not 1 <= budget <= 250:
            raise EvaluationError(400, "planning.invalid", "budget_ms must be an integer from 1 to 250")
        if ("goal_body_m" in aim and (not isinstance(aim["goal_body_m"], list) or len(aim["goal_body_m"]) != 2)
                or "preferred_side" in aim and aim["preferred_side"] not in ("left", "right")):
            raise EvaluationError(400, "planning.invalid", "expected a planar goal or a left/right preference")
        try:
            source_time = stamp(snap.get("scan_stamp_ns"))
        except ValueError as exc:
            raise EvaluationError(400, "planning.invalid", "missing acquisition timestamp") from exc
        with self.computation():
            def fresh():
                if not -FUTURE_TOLERANCE_NS <= self.wall() - source_time <= MAX_AGE_NS:
                    raise EvaluationError(409, "planning.stale", "snapshot outside the live observation window")
            fresh()
            key = (snap["source_id"], snap["base_frame"], snap["calibration_ref"])
            if source_time < self.newest.get(key, 0):
                raise EvaluationError(409, "planning.out_of_order", "newer observation already seen for this source")
            try:
                result = self.solve(snap, self.profile, goal_body_m=aim.get("goal_body_m"),
                                    preferred_side=aim.get("preferred_side"))
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise EvaluationError(422, "planning.invalid", str(exc)[:240]) from exc
            self.newest[key] = source_time
            self.newest.move_to_end(key)
            if len(self.newest) > 32:
                self.newest.popitem(last=False)
            fresh()
            elapsed = self.monotonic() - start
            if elapsed > budget * 1_000_000:
                raise EvaluationError(503, "planning.deadline", "planning budget expired; discard this result")
            return {**result, "provider_version": VERSION, "service": SERVICE, "planning_scope": "static_local_path",
                    "elapsed_ms": elapsed/1_000_000,
                    "observation_valid_until_ns": str(source_time + MAX_AGE_NS)}
