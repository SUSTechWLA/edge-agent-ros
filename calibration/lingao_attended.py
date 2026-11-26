"""Operator-held forward commissioning jog. The watchdog only observes and revokes.

The trusted site adapter provides a nonblocking inspect() snapshot with eligible,
detail, observed_monotonic, and odom (source-checked MCU Measurement). begin(op,
deadline_monotonic), forward(op, seq, vx_m_s) and stop(op, seq) return True on
acceptance. The downstream owner must enforce the deadline across process loss,
prioritize manual takeover, and fence delayed writes, including a late begin
after stop. A raw Twist publisher cannot fulfill this contract.
"""
from contextlib import contextmanager
import json
import math
import sqlite3
import threading
import time
import uuid

from calibration.lingao_short_trial import MAX_AGE, Measurement, Trial, TrialLimits


PROFILE = "forward-50mm-100mmps"
LIMITS = TrialLimits(length=.050, speed=.100, min_final=.045, max_final=.050,
                     max_measured_speed=.105, min_measured_velocity=.025)
MAX_DRIVE = LIMITS.length / LIMITS.speed
HOLD_TTL = .150
VERIFY_TIMEOUT = 3.
SCHEMA = "edge.lingao.attended-jog.v1"


class AttendedJog:
    def __init__(self, db_path, robot_id, io, *, monotonic=time.monotonic, wall_ns=time.time_ns):
        if not robot_id or str(db_path) == ":memory:":
            raise ValueError("persistent journal and robot identity required")
        self.db_path, self.robot_id, self.io = db_path, robot_id, io
        self.scope = getattr(io, "evidence_scope", None)
        if self.scope not in ("hardware", "isolated"):
            raise ValueError("explicit adapter evidence scope required")
        self.clock, self.wall_ns = monotonic, wall_ns
        self.lock = threading.RLock()
        self.shutdown = threading.Event()
        self.thread = None
        self.closed = False
        self.ledger_fault = False
        self.active = None
        self.last_op = None
        self.probe = self._trial()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS attended_operations(
                op TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL)""")

    @contextmanager
    def _db(self):
        # Connections are thread-local to each transaction and closed on exit.
        db = None
        try:
            db = sqlite3.connect(self.db_path, timeout=1.)
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        except sqlite3.Error:
            self.ledger_fault = True
            if self.active:
                self._cut(self.clock(), "operation_journal_unavailable")
            raise
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _trial():
        return Trial("forward", lambda _progress: LIMITS.speed, limits=LIMITS)

    def _base(self, operation_id, profile, state, reason):
        phase = {"READY":"ready", "ARMED":"running", "HELD":"running", "STOPPING":"stopping",
                 "VERIFIED":"verified", "RECOVERY_REQUIRED":"recovery_required", "IDLE":"idle"}.get(state, "failed")
        return dict(schema=SCHEMA, operation_id=operation_id, robot_id=self.robot_id,
                    evidence_scope=self.scope, profile_id=profile, state=state, reason=reason,
                    phase=phase, ready=state == "READY", stopped=None, displacement_m=None,
                    peak_speed_m_s=None,
                    requested_speed_m_s=LIMITS.speed if profile == PROFILE else None,
                    nominal_distance_m=LIMITS.length if profile == PROFILE else None,
                    maximum_command_duration_s=MAX_DRIVE if profile == PROFILE else None)

    def _observe(self, now):
        data = {}
        try:
            data = self.io.inspect()
            now = self.clock()
            if data.get("evidence_scope", self.scope if self.scope == "isolated" else None) != self.scope:
                self.probe.samples.clear()
                return False, "unexpected_mcu_evidence_scope"
            if not 0 <= now - data["observed_monotonic"] <= MAX_AGE:
                self.probe.samples.clear()
                if data.get("eligible") is False and data.get("detail"):
                    return False, str(data["detail"])
                return False, "site_snapshot_stale"
            m = data["odom"]
            wall_age = (self.wall_ns() - m.stamp) * 1e-9
            if (not isinstance(m, Measurement) or not isinstance(m.stamp, int)
                    or not all(math.isfinite(v) for v in (m.arrival, m.x, m.y, m.yaw, m.vx, m.vy, m.wz))
                    or not 0 <= now - m.arrival <= MAX_AGE or not 0 <= wall_age <= MAX_AGE):
                raise ValueError("invalid_mcu_measurement")
        except Exception:
            self.probe.samples.clear()
            if isinstance(data, dict) and data.get("eligible") is False and data.get("detail"):
                return False, str(data["detail"])
            return False, "mcu_odometry_unavailable"
        prior = self.probe.last_sample
        if prior is None or (m.stamp, m.arrival) != (prior.stamp, prior.arrival):
            if prior and (m.arrival <= prior.arrival or m.stamp <= prior.stamp):
                self.probe.samples.clear()
                return False, "mcu_odometry_out_of_order"
            self.probe.observe(m)
            if self.probe.start and abs(m.wz) > .035:
                self._cut(now, "unexpected_motion")
        profiles = data.get("profiles")
        if (data.get("eligible") is not True or not isinstance(profiles, (list, tuple))
                or PROFILE not in profiles):
            return False, str(data.get("detail") or "site_interlock_unavailable")
        return True, ""

    def _site_ready(self, now):
        ok, detail = self._observe(now)
        if self.ledger_fault:
            return False, "operation_journal_unavailable"
        if self.closed or (self.scope == "hardware" and (self.thread is None or not self.thread.is_alive())):
            return False, "vehicle_watchdog_offline"
        if ok and not self.probe.stopped(self.clock()):
            return False, "initial_mcu_stop_unverified"
        return ok, detail

    def begin(self, operation_id, holder_id, *, authorized, profile_id=PROFILE):
        """Consume this id, even on denial. Only a later genuine hold can drive."""
        if not isinstance(operation_id, str) or str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError("invalid_operation_id")
        if not isinstance(holder_id, str) or not holder_id:
            raise ValueError("missing_holder")
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT op,state FROM attended_operations ORDER BY rowid LIMIT 1").fetchone()
            if row:
                if row[0] == operation_id:
                    return self._base(operation_id, profile_id, "REJECTED", "operation_already_consumed")
                reason = ("previous_operation_unresolved" if row[1] in ("ACTIVE","RECOVERY_REQUIRED")
                          else "commissioning_round_already_used")
                return self._base(operation_id, profile_id, "BLOCKED", reason)
            now = self.clock()
            ok, detail = self._site_ready(now)
            if authorized is not True or self.closed:
                reason = "operator_not_authorized"
            elif profile_id != PROFILE:
                reason = "profile_not_commissioned"
            elif not ok:
                reason = detail
            else:
                reason = ""
            state = "BLOCKED" if reason else "ARMED"
            result = self._base(operation_id, profile_id, state, reason)
            db.execute("INSERT INTO attended_operations VALUES (?,?,?)",
                       (operation_id, "BLOCKED" if reason else "ACTIVE", json.dumps(result)))
            db.commit()  # Durable claim before making the operation visible to another thread.
            self.last_op = operation_id
            if not reason:
                self.active = dict(op=operation_id, holder=holder_id, heartbeat=now, seq=0,
                                   deadline=None, write_seq=0, busy=False, opened=False,
                                   revoking=False, stop_ack=None)
            return result

    def _cut(self, now, reason):
        if self.probe.stop_time is None:
            self.probe.stop(now, reason)

    def _guard(self, now):
        ok, detail = self._observe(now)
        now = self.clock()
        a = self.active
        if not a or self.probe.stop_time is not None:
            return
        if self.ledger_fault:
            self._cut(now, "operation_journal_unavailable")
        elif self.closed:
            self._cut(now, "service_closing")
        elif now - a["heartbeat"] >= HOLD_TTL:
            self._cut(now, "operator_hold_expired")
        elif not ok:
            self._cut(now, detail)
        elif a["deadline"] is not None and (now >= a["deadline"] or self.probe.travel >= LIMITS.length-.0006):
            self._cut(now, None)

    def _finish(self, result):
        a = self.active
        result = {**self._base(a["op"], PROFILE, result["state"], result["reason"]), **result}
        result["displacement_m"] = result["body_longitudinal_displacement_m"] if self.probe.start else None
        result["peak_speed_m_s"] = result["peak_directed_mcu_velocity_m_s"] if self.probe.start else None
        result["revoke_acknowledged"] = a["stop_ack"]
        result["initial_odom_stamp_ns"] = str(self.probe.start.stamp) if self.probe.start else None
        result["final_odom_stamp_ns"] = str(self.probe.last_sample.stamp) if self.probe.last_sample else None
        with self._db() as db:
            db.execute("UPDATE attended_operations SET state=?,payload=? WHERE op=?",
                       (result["state"], json.dumps(result, allow_nan=False), a["op"]))
        self.last_op = a["op"]
        self.active = None
        self.probe = self._trial()

    def _consume_unseen(self, operation_id):
        """A release/heartbeat can overtake the POST that armed its operation."""
        if not isinstance(operation_id, str) or str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError("invalid_operation_id")
        result = self._base(operation_id, PROFILE, "BLOCKED", "no_matching_hold")
        with self._db() as db:
            db.execute("""INSERT OR IGNORE INTO attended_operations
                       SELECT ?,?,? WHERE NOT EXISTS (SELECT 1 FROM attended_operations)""",
                       (operation_id, "BLOCKED", json.dumps(result)))

    def _flush_stop(self):
        """Revoke independently of an in-flight grant/forward, then verify actual standstill."""
        with self.lock:
            a = self.active
            if not a or self.probe.stop_time is None or a["revoking"]:
                return
            if a["deadline"] is None:
                self._finish(self.probe.receipt(self.clock()))
                return
            if a["stop_ack"] is None:
                a["revoking"] = True
                a["write_seq"] += 1
                seq = a["write_seq"]
            else:
                now = self.clock()
                if self.probe.stopped(now) or now - self.probe.stop_time >= VERIFY_TIMEOUT:
                    result = self.probe.receipt(now)
                    self._finish(result)
                return
        try:
            ack = self.io.stop(a["op"], seq) is True
        except Exception:
            ack = False
        with self.lock:
            a["revoking"] = False
            a["stop_ack"] = ack
            if not ack:
                self.probe.reason = self.probe.reason or "revocation_unacknowledged"

    def heartbeat(self, operation_id, holder_id, seq, *, authorized, held):
        """A browser heartbeat is the only path issuing one nonzero request."""
        grant = forward = False
        with self.lock:
            a = self.active
            if not a or a["op"] != operation_id or a["holder"] != holder_id:
                self._consume_unseen(operation_id)
                return self._base(operation_id, PROFILE, "REJECTED", "no_matching_hold")
            now = self.clock()
            self._guard(now)  # A packet arriving after expiration cannot extend an old hold.
            if authorized is not True or held is not True:
                self._cut(now, "operator_released")
            elif type(seq) is not int or seq != a["seq"] + 1:
                self._cut(now, "invalid_hold_sequence")
            elif self.probe.stop_time is None:
                a.update(seq=seq, heartbeat=now)
                if not a["busy"]:
                    a["busy"] = True
                    grant = a["deadline"] is None
                    if grant:
                        a["deadline"] = now + MAX_DRIVE
                    forward = True
        if forward:
            try:
                if grant:
                    accepted = self.io.begin(operation_id, a["deadline"]) is True
                    with self.lock:
                        a["opened"] = accepted
                        if not accepted and self.active is a:
                            self._cut(self.clock(), "site_lease_denied")
                with self.lock:
                    now = self.clock()
                    speed = None
                    if self.active is a:
                        self._guard(now)
                        speed = self.probe.tick(now, a["opened"])
                    if speed == LIMITS.speed:
                        a["write_seq"] += 1
                        write_seq = a["write_seq"]
                    elif self.active is a and self.probe.stop_time is None:
                        self._cut(now, "initial_mcu_stop_unverified")
                if speed == LIMITS.speed and self.io.forward(operation_id, write_seq, speed) is not True:
                    raise RuntimeError("site_forward_not_acknowledged")
            except Exception:
                with self.lock:
                    if self.active is a:
                        self._cut(self.clock(), "site_io_unacknowledged")
            finally:
                with self.lock:
                    a["busy"] = False
        self._flush_stop()
        return self.status(operation_id)

    def release(self, operation_id, holder_id):
        with self.lock:
            if self.active and self.active["op"] == operation_id and self.active["holder"] == holder_id:
                self._cut(self.clock(), "operator_released")
            else:
                self._consume_unseen(operation_id)
        self._flush_stop()
        return self.status(operation_id)

    def tick(self):
        with self.lock:
            self._guard(self.clock())
        self._flush_stop()

    def status(self, operation_id=None):
        with self.lock:
            a = self.active
            if operation_id is None:
                operation_id = a["op"] if a else self.last_op
            if a and a["op"] == operation_id:
                self._observe(self.clock())
                state = "STOPPING" if self.probe.stop_time is not None else "HELD" if a["opened"] else "ARMED"
                result = self._base(operation_id, PROFILE, state, self.probe.reason or "")
                result["stopped"] = self.probe.standstill_feedback(self.clock())
                if self.probe.start:
                    result.update(displacement_m=self.probe.travel, peak_speed_m_s=self.probe.peak_speed)
                return result
            with self._db() as db:
                row = (db.execute("SELECT state,payload FROM attended_operations WHERE op=?",
                                  (operation_id,)) if operation_id else
                       db.execute("SELECT state,payload FROM attended_operations ORDER BY rowid LIMIT 1")).fetchone()
            if not row:
                ready, detail = self._site_ready(self.clock()) if operation_id is None else (False, "unknown_operation")
                result = self._base(operation_id, PROFILE, "READY" if ready else "IDLE", detail)
                if operation_id is None:
                    result["stopped"] = self.probe.standstill_feedback(self.clock())
                return result
            result = json.loads(row[1])
            if result.get("robot_id") != self.robot_id or result.get("evidence_scope") != self.scope:
                return self._base(operation_id, PROFILE, "RECOVERY_REQUIRED", "foreign_commissioning_journal")
            if row[0] == "ACTIVE":
                result.update(state="RECOVERY_REQUIRED", phase="recovery_required",
                              reason="unresolved_operation_after_restart")
            return result

    def start_watchdog(self):
        with self.lock:
            if self.closed:
                raise RuntimeError("service_closed")
            if self.thread is None:
                self.thread = threading.Thread(target=self._watch, name="attended-jog-watchdog", daemon=True)
                self.thread.start()

    def _watch(self):
        while not self.shutdown.wait(.020):
            self.tick()

    def close(self):
        with self.lock:
            self.closed = True
        self.tick()
        self.shutdown.set()
        if self.thread:
            self.thread.join(timeout=1.)
