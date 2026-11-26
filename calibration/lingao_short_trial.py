"""One 10 mm commissioning trial. Velocity requests are distinct from MCU evidence."""
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid


VENDOR_SHA256 = "9e90d676ef6ae9fc7df4b04cea6fac6c6577f066703810f4a51fa5fe5b5270bc"
LENGTH, SPEED = .010, .020
MAX_AGE = .15
MAX_DRIVE = LENGTH/SPEED


class VendorStraightLine:
    """Reuse the installed straight-line controller without creating its ROS publisher."""
    def __init__(self, file, signed_distance):
        path = Path(file)
        # Compile the very bytes checked here, including on a host with stale .pyc files.
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != VENDOR_SHA256:
            raise ValueError("uncommissioned_vendor_source")
        module = {"__name__":"commissioned_lingao_move_relative","__file__":str(path)}
        exec(compile(source,str(path),"exec"),module)
        self.step = module["MoveRelative"]._drive_step
        self.args = SimpleNamespace(distance=signed_distance)
        self._target_distance_m, self._drive_speed_m_s = abs(signed_distance), SPEED
        self.cmd_vel_pub = SimpleNamespace(publish=self.capture)

    def travelled_distance_m(self):
        return self.progress

    def capture(self, command):
        self.speed = command.linear.x

    def _stop(self, _reason):
        self.speed = 0.

    def candidate(self, progress):
        self.progress, self.speed = progress, None
        self.step(self)
        return self.speed


class Claims:
    """A crash leaves an unresolved claim. A second invocation cannot repeat it."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS trials(
            operation TEXT PRIMARY KEY, robot TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT)""")
        self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS unresolved_trial
            ON trials((1)) WHERE state IN ('RUNNING','RECOVERY_REQUIRED')""")

    def claim(self, op, robot):
        if str(uuid.UUID(op))!=op or not robot:
            raise ValueError("invalid_operation_identity")
        with self.db:
            self.db.execute("INSERT INTO trials VALUES(?,?,'RUNNING',NULL)",(op,robot))

    def finish(self, op, receipt):
        with self.db:
            self.db.execute("UPDATE trials SET state=?,receipt=? WHERE operation=? AND state='RUNNING'",
                            (receipt["state"],json.dumps(receipt,allow_nan=False),op))


@dataclass(frozen=True)
class Measurement:
    stamp: int
    arrival: float
    x: float
    y: float
    yaw: float
    vx: float
    vy: float
    wz: float


@dataclass(frozen=True)
class TrialLimits:
    length: float = LENGTH
    speed: float = SPEED
    min_final: float = .0075
    max_final: float = .012
    max_measured_speed: float = .026
    min_measured_velocity: float = .005


class Trial:
    def __init__(self, direction, candidate, *, limits=TrialLimits()):
        if direction not in ("reverse","forward"):
            raise ValueError("unknown_direction")
        self.sign = -1 if direction=="reverse" else 1
        self.candidate = candidate
        self.limits = limits
        self.samples = deque()
        self.start = self.started = None
        self.last_sample = None
        self.reason = None
        self.stop_time = None
        self.travel = self.peak_travel = self.peak_speed = 0.
        self.proved_velocity = False
        self.sent = 0

    def observe(self, m):
        if self.last_sample and (m.stamp<=self.last_sample.stamp or m.arrival-self.last_sample.arrival>MAX_AGE
                                 or m.stamp-self.last_sample.stamp>MAX_AGE*1e9):
            self.samples.clear()
            if self.start and self.stop_time is None:
                self.stop(m.arrival,"odom_discontinuous")
        self.last_sample = m
        self.samples.append(m)
        while self.samples and self.samples[0].arrival<m.arrival-1.8:
            self.samples.popleft()
        if self.start:
            dx,dy = m.x-self.start.x,m.y-self.start.y
            c,s = math.cos(self.start.yaw),math.sin(self.start.yaw)
            self.travel = self.sign*(dx*c+dy*s)
            self.peak_travel=max(self.peak_travel,self.travel)
            self.peak_speed=max(self.peak_speed,self.sign*m.vx)
            self.proved_velocity |= self.sign*m.vx>=self.limits.min_measured_velocity
            yaw_change=math.remainder(m.yaw-self.start.yaw,2*math.pi)
            if (self.travel<-.0015 or self.sign*m.vx<-.004 or abs(-s*dx+c*dy)>.003 or
                    abs(yaw_change)>.035 or self.travel>self.limits.max_final or
                    math.hypot(m.vx,m.vy)>self.limits.max_measured_speed):
                self.reason=self.reason or "unexpected_motion"
                self.stop(m.arrival,self.reason)

    def standstill_feedback(self, now):
        """Report observed motion immediately; a positive stop requires a full MCU window."""
        rows = self.samples
        if not rows or not 0 <= now - rows[-1].arrival <= MAX_AGE:
            return None
        first, last = rows[0], rows[-1]
        if not all(math.hypot(r.vx, r.vy) <= .003 and abs(r.wz) <= .015 and
                   math.hypot(r.x-first.x, r.y-first.y) <= .0015 for r in rows):
            return False
        if len(rows) < 3 or last.arrival-first.arrival < 1.5 or last.stamp-first.stamp < 1_500_000_000:
            return None
        return True

    def stopped(self, now):
        return self.standstill_feedback(now) is True

    def stop(self, now, reason=None):
        self.reason=self.reason or reason
        if self.stop_time is None:
            self.stop_time=now
            self.samples.clear()  # Stop evidence must be entirely after relinquishing velocity.

    def tick(self, now, permitted):
        if self.stop_time is not None:
            return None
        if not permitted or not self.last_sample or now-self.last_sample.arrival>MAX_AGE:
            if self.start is not None:
                self.stop(now,"interlock_or_odometry_lost")
            return None
        if self.start is None:
            if not self.stopped(now): return None
            self.start,self.started=self.last_sample,now
        if self.travel>=self.limits.length-.0006 or now-self.started>=self.limits.length/self.limits.speed:
            self.stop(now)
            return 0.
        speed=self.candidate(self.travel)
        if speed != self.sign*self.limits.speed:
            self.stop(now,"invalid_vendor_candidate")
            return 0.
        self.sent+=1
        return speed

    def receipt(self, now):
        stopped=self.stopped(now) and self.stop_time is not None
        passed=(self.sent>0 and stopped and self.proved_velocity and self.reason is None
                and self.limits.min_final<=self.travel<=self.limits.max_final)
        state="VERIFIED" if passed else "RECOVERY_REQUIRED" if self.sent and not stopped else "FAILED"
        return dict(state=state,reason=self.reason or ("verified" if passed else "travel_or_stop_unverified"),
                    candidate_writes=self.sent,signed_progress_m=self.travel,peak_progress_m=self.peak_travel,
                    body_longitudinal_displacement_m=self.sign*self.travel,
                    peak_directed_mcu_velocity_m_s=self.peak_speed,stopped=stopped,
                    initial_odom_xy_yaw=[self.start.x,self.start.y,self.start.yaw] if self.start else None,
                    final_odom_xy_yaw=[self.last_sample.x,self.last_sample.y,self.last_sample.yaw] if self.last_sample else None,
                    stopped_window_ns=(self.samples[-1].stamp-self.samples[0].stamp) if stopped else None)
