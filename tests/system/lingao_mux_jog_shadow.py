"""Offline selection test using the installed vendor ModeGate. No ROS or device I/O."""
import argparse
import hashlib
import json
from pathlib import Path


ZERO = (0.,) * 6
FORWARD = (.1, 0., 0., 0., 0., 0.)
VENDOR_SHA = "2477988faf6b693cf594b06f12a8cb50f903a5b50ea7d52b867fe1ec44b01041"
LEASE_NS = 1_000_000_000
HEARTBEAT_NS = 120_000_000


class Receiver:
    """Shadow of the selector running on a single serialized mux executor."""
    def __init__(self, gate):
        self.gate = gate
        self.phase = "normal"
        self.last_heartbeat = None
        self.deadline = None
        self.reason = None
        self.generation = None
        self.owner = None
        self.sequence = 0

    def begin(self, operation, issue_ns, now_ns, *, site_ready):
        # issue_ns is the monotonic timestamp on the same Jetson boot.
        if (self.phase != "normal" or self.owner is not None or not site_ready
                or self.gate.phase != "ready" or self.gate.mode != "navigation"
                or self.gate.error or not 0 <= now_ns - issue_ns < HEARTBEAT_NS
                or any(self.gate.output(now_ns / 1e9, path_ready=True, nav_busy=False))):
            return False
        self.gate.samples.clear()
        self.owner = operation
        self.phase = "jog"
        self.deadline = issue_ns + LEASE_NS
        self.last_heartbeat = now_ns
        self.generation = self.gate.generation
        return True

    def stop(self, reason):
        if self.phase == "jog":
            self.phase = "waiting_for_standstill"
            self.reason = reason
            self.gate.samples.clear()

    def healthy(self, now_ns, site_ready):
        if now_ns >= self.deadline:
            self.stop("deadline")
        elif now_ns - self.last_heartbeat >= HEARTBEAT_NS:
            self.stop("heartbeat_expired")
        elif (not site_ready or self.gate.generation != self.generation or self.gate.mode != "navigation"
              or self.gate.phase != "ready" or self.gate.error):
            self.stop("interlock")
        return self.phase == "jog"

    def command(self, owner, seq, twist, now_ns):
        if self.phase != "jog" or not self.healthy(now_ns, True):
            return False
        if owner != self.owner or type(seq) is not int or seq != self.sequence + 1 or twist != FORWARD:
            self.stop("unexpected_command")
            return False
        self.sequence, self.last_heartbeat = seq, now_ns
        return True

    def normal_input(self, source, twist, now_ns):
        if self.phase == "normal":
            self.gate.record(source, twist, now_ns / 1e9)
        elif self.phase == "jog":
            # Zero is a stop request as well. Do not replay this queued sample on exit.
            self.stop("competing_" + source)

    def select(self, now_ns, *, path_ready=True, nav_busy=False, manual=False, estop=False):
        if self.phase == "normal":
            return self.gate.output(now_ns / 1e9, path_ready=path_ready, nav_busy=nav_busy)
        if self.phase == "jog":
            if manual or estop:
                self.stop("manual_takeover" if manual else "emergency_stop")
            elif self.healthy(now_ns, path_ready and nav_busy is False) and self.sequence:
                return FORWARD
        return ZERO

    def standstill(self, verified):
        if self.phase == "waiting_for_standstill" and verified:
            self.gate.samples.clear()
            self.phase = "normal"


def verify(vendor):
    content = Path(vendor).read_bytes()
    if hashlib.sha256(content).hexdigest() != VENDOR_SHA:
        raise RuntimeError("Unexpected vendor gate source")
    namespace = {"__name__": "__main__"}
    exec(compile(content, str(vendor), "exec"), namespace)
    gate_type = namespace["ModeGate"]
    receipt = {"scope": "offline_model_only", "vendor_gate_sha256": VENDOR_SHA,
               "requested": {"linear_x_m_s": .1, "angular_z_rad_s": 0, "duration_ns": LEASE_NS}, "cases": {}}
    start = 23_000_000_000
    for case in ("deadline", "heartbeat_expired", "competing_navigation", "competing_follow",
                 "manual_takeover", "emergency_stop", "upstream_fault", "wrong_owner", "delayed_start"):
        site = Receiver(gate_type())
        assert site.select(start) == ZERO
        delayed = 200_000_000 if case == "delayed_start" else 0
        ok = site.begin("current", start, start + delayed, site_ready=True)
        if case == "delayed_start":
            assert not ok and site.select(start + delayed) == ZERO
            receipt["cases"][case] = {"rejected": True}
            continue
        assert ok and site.select(start) == ZERO
        published = []
        # A 20 Hz simulated operator, actual monotonic deadline checked each selection.
        for offset in range(0, 1_100_000_000, 10_000_000):
            now = start + offset
            if offset % 50_000_000 == 0 and not (case == "heartbeat_expired" and offset >= 200_000_000):
                site.command("current", offset // 50_000_000 + 1, FORWARD, now)
            trigger = offset == 250_000_000
            if trigger and case.startswith("competing_"):
                site.normal_input(case.split("_", 1)[1], (.04, 0, 0, 0, 0, .02), now)
            if trigger and case == "upstream_fault":
                site.gate.fail("stock_path_error")
            if trigger and case == "wrong_owner":
                site.command("late-other", 7, FORWARD, now)
            selected = site.select(now, manual=trigger and case=="manual_takeover",
                                   estop=trigger and case=="emergency_stop")
            published.append((offset, selected))
        first_zero = next(t for t, value in published if value == ZERO)
        expected = LEASE_NS if case == "deadline" else 270_000_000 if case == "heartbeat_expired" else 250_000_000
        assert expected == first_zero
        assert all(v == (FORWARD if t < first_zero else ZERO) for t, v in published)
        assert not site.command("current", 99, FORWARD, start + LEASE_NS + 1)
        assert not site.begin("repeat", start + LEASE_NS, start + LEASE_NS + 2, site_ready=True)
        site.standstill(False)
        assert site.phase == "waiting_for_standstill"
        site.standstill(True)
        assert site.select(start + 2 * LEASE_NS) == ZERO
        receipt["cases"][case] = {"first_zero_ns_from_start": first_zero, "reason":site.reason,
                                  "no_late_restart": True}
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock_gate")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.stock_gate)
    args.out.write_text(json.dumps(result, indent=2)+"\n", encoding="utf8")
    print(json.dumps(result))
