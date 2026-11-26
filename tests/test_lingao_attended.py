import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from calibration.lingao_attended import AttendedJog, HOLD_TTL, PROFILE
from calibration.lingao_short_trial import Measurement


class Site:
    """Isolated input and fenced actuator stub; no network, ROS or devices."""
    evidence_scope = "isolated"

    def __init__(self):
        self.ns = 10_000_000_000
        self.m = None
        self.allowed = True
        self.profiles = [PROFILE]
        self.age = 0.
        self.revoked = set()
        self.deadlines = {}
        self.commands = []
        self.stops = []
        self.revocation = threading.Event()
        self.speed = 0.
        self.x = 0.
        self.stop_ack = True
        self.take_sample(0.)

    def clock(self):
        return self.ns / 1e9

    def take_sample(self, delta, *, x=None, vx=None):
        self.ns += round(delta * 1e9)
        if vx is not None:
            self.speed = vx
        self.x = self.x + delta * self.speed if x is None else x
        self.m = Measurement(self.ns, self.clock(), self.x, 0., 0., self.speed, 0., 0.)

    def inspect(self):
        return dict(eligible=self.allowed, detail="manual_takeover_or_interlock",
                    observed_monotonic=self.clock()-self.age, profiles=self.profiles, odom=self.m)

    def begin(self, op, deadline):
        if op in self.revoked or self.clock() >= deadline:
            return False
        self.deadlines[op] = deadline
        return True

    def forward(self, op, seq, speed):
        if op in self.revoked or self.clock() >= self.deadlines[op]:
            return False
        self.commands.append((op, seq, speed, self.clock()))
        return True

    def stop(self, op, seq):
        self.revoked.add(op)
        self.stops.append((op, seq))
        self.revocation.set()
        return self.stop_ack


class AttendedTrialTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name, "attended.sqlite")
        self.site = Site()
        self.jog = self.new_core()
        self.addCleanup(self.jog.close)
        self.op = str(uuid.uuid4())
        for _ in range(36):
            self.frame(.05)

    def new_core(self):
        return AttendedJog(self.path, "stub-rover", self.site,
                           monotonic=self.site.clock, wall_ns=lambda: self.site.ns)

    def frame(self, delta, **kw):
        self.site.take_sample(delta, **kw)
        self.jog.tick()

    def arm(self, **kw):
        return self.jog.begin(self.op, "local-holder", authorized=kw.pop("authorized", True), **kw)

    def hold(self, seq, **kw):
        return self.jog.heartbeat(self.op, kw.pop("holder","local-holder"), seq,
                                  authorized=kw.pop("authorized",True), held=kw.pop("held",True))

    def settle(self):
        for _ in range(39):
            self.frame(.05, vx=0.)
        return self.jog.status(self.op)

    def test_distinct_100mmps_profile_forward_stop_and_independent_mcu_receipt(self):
        self.assertEqual(self.jog.status()["phase"], "ready")
        self.assertEqual(self.arm()["state"], "ARMED")
        self.assertEqual(self.site.commands, [])
        self.hold(1)
        for seq in range(2, 12):
            self.frame(.05, vx=.100, x=round((seq-1)*.005, 3))
            self.hold(seq)
        self.assertEqual({round(r[2],5) for r in self.site.commands}, {.100})
        self.assertTrue(all(r[3] < self.site.deadlines[self.op] for r in self.site.commands))
        self.assertEqual(len(self.site.stops), 1)
        self.jog.release(self.op, "local-holder")  # Release after normal completion is harmless.
        receipt = self.settle()
        self.assertEqual(receipt["state"], "VERIFIED")
        self.assertEqual(receipt["phase"], "verified")
        self.assertTrue(receipt["stopped"])
        self.assertEqual(receipt["evidence_scope"], "isolated")
        self.assertEqual(receipt["profile_id"], PROFILE)
        self.assertAlmostEqual(receipt["signed_progress_m"], .050)
        self.assertGreaterEqual(receipt["stopped_window_ns"], 1_500_000_000)
        restarted = self.new_core()
        self.assertEqual(restarted.status(self.op), receipt)
        second = restarted.begin(str(uuid.uuid4()), "local-holder", authorized=True)
        self.assertEqual(second["reason"], "commissioning_round_already_used")
        self.assertEqual(restarted.status(), receipt)

    def test_denied_then_permitted_can_never_start(self):
        self.assertEqual(self.arm(authorized=False)["state"], "BLOCKED")
        self.assertEqual(self.arm()["state"], "REJECTED")
        self.assertEqual(self.hold(1)["state"], "REJECTED")
        self.assertFalse(self.site.commands)

    def test_overtaking_release_cannot_arm_later(self):
        self.jog.release(self.op, "local-holder")
        self.assertEqual(self.arm()["state"], "REJECTED")
        self.assertFalse(self.site.commands)

    def test_overtaking_heartbeat_cannot_arm_later(self):
        self.hold(1)
        self.assertEqual(self.arm()["state"], "REJECTED")
        self.assertFalse(self.site.commands)

    def test_other_profile_has_no_authority_even_when_site_is_ready(self):
        result = self.arm(profile_id="forward-10mm-20mmps")
        self.assertEqual(result["reason"], "profile_not_commissioned")
        self.assertFalse(self.site.commands)
        self.assertFalse(self.site.commands)

    def test_site_must_expose_commissioned_profile(self):
        self.site.profiles = []
        self.assertEqual(self.arm()["state"], "BLOCKED")

    def test_release_before_first_heartbeat_and_wrong_holder(self):
        self.arm()
        self.assertEqual(self.hold(1, holder="other")["state"], "REJECTED")
        self.jog.release(self.op, "local-holder")
        self.assertEqual(self.hold(1)["state"], "REJECTED")
        self.assertFalse(self.site.commands)

    def test_release_during_forward_drops_late_writes_then_cannot_resume(self):
        gate, pending = threading.Event(), threading.Event()
        orig = self.site.forward
        def delayed(*args):
            pending.set()
            gate.wait(2.)
            return orig(*args)
        self.site.forward = delayed
        self.arm()
        worker = threading.Thread(target=lambda: self.hold(1))
        worker.start()
        try:
            self.assertTrue(pending.wait(1.))
            self.jog.release(self.op, "local-holder")
            self.assertTrue(self.site.revocation.is_set())
        finally:
            gate.set()
            worker.join(1.)
        self.assertFalse(self.site.commands)
        self.assertEqual(self.hold(2)["state"], "STOPPING")
        self.assertEqual(self.settle()["reason"], "operator_released")

    def test_hold_expiration_before_late_heartbeat_latches(self):
        self.arm()
        self.hold(1)
        self.frame(.06)
        self.frame(.06)
        self.site.take_sample(HOLD_TTL + .01 - .12)
        self.hold(2)
        self.assertEqual(len(self.site.commands), 1)
        self.assertEqual(self.jog.status(self.op)["reason"], "operator_hold_expired")

    def test_watchdog_revokes_on_disconnect_without_more_browser_calls(self):
        self.jog.start_watchdog()
        self.arm()
        self.hold(1)
        self.site.take_sample(.16)
        self.assertTrue(self.site.revocation.wait(1.))
        self.assertEqual(self.site.stops[0][0], self.op)
        self.assertEqual(len(self.site.commands), 1)

    def test_mcu_clock_age_and_site_snapshot_and_proof_of_stop(self):
        self.arm()
        self.hold(1)
        self.site.m = Measurement(self.site.ns-170_000_000, self.site.clock(), 0., 0., 0., 0., 0., 0.)
        self.jog.tick()
        self.assertEqual(self.jog.status(self.op)["reason"], "mcu_odometry_unavailable")
        self.site.ns += 4_000_000_000  # no new MCU sample
        self.jog.tick()
        self.assertEqual(self.jog.status(self.op)["state"], "RECOVERY_REQUIRED")
        self.op = str(uuid.uuid4())
        self.assertEqual(self.arm()["reason"], "previous_operation_unresolved")

    def test_manual_takeover_and_lost_interlock_cannot_restart(self):
        self.arm()
        self.hold(1)
        self.site.allowed = False
        self.jog.tick()
        self.site.allowed = True
        self.hold(2)
        self.assertEqual(len(self.site.commands), 1)
        self.assertEqual(self.settle()["state"], "FAILED")
        self.assertFalse(self.arm()["ready"])

    def test_expired_site_snapshot_never_enables(self):
        self.site.age = .16
        self.assertEqual(self.arm()["state"], "BLOCKED")

    def test_readonly_mcu_stopped_is_independent_of_jog_permission(self):
        ready = self.jog.status()
        self.assertTrue(ready["ready"])
        self.assertTrue(ready["stopped"])
        self.site.allowed = False
        state = self.jog.status()
        self.assertEqual(state["phase"], "idle")
        self.assertTrue(state["stopped"])
        self.assertFalse(state["ready"])
        self.frame(.05, vx=.02)
        state = self.jog.status()
        self.assertIs(state["stopped"], False)
        self.assertFalse(state["ready"])
        self.assertFalse(self.site.commands)

    def test_stale_missing_or_incomplete_mcu_feedback_is_unknown_until_reconfirmed(self):
        self.site.allowed = False
        self.site.ns += 160_000_000
        self.assertIsNone(self.jog.status()["stopped"])
        self.site.m = None
        self.assertIsNone(self.jog.status()["stopped"])
        for _ in range(20):
            self.frame(.05, vx=0.)
        self.assertIsNone(self.jog.status()["stopped"])
        for _ in range(12):
            self.frame(.05, vx=0.)
        self.assertIs(self.jog.status()["stopped"], True)
        self.site.age = .16
        self.assertIsNone(self.jog.status()["stopped"])
        self.assertFalse(self.site.commands)

    def test_wrong_direction_never_verifies(self):
        self.arm()
        self.hold(1)
        self.frame(.05, vx=-.020, x=-.002)
        self.assertEqual(self.settle()["reason"], "unexpected_motion")

    def test_no_actual_motion_never_verifies(self):
        self.arm()
        for seq in range(1, 12):
            self.hold(seq)
            self.frame(.05, vx=0.)
        receipt = self.settle()
        self.assertEqual(receipt["state"], "FAILED")
        self.assertTrue(receipt["stopped"])
        self.assertEqual(receipt["signed_progress_m"], 0.)

    def test_open_operation_is_not_replayed_or_replaced_after_process_loss(self):
        self.arm()
        self.hold(1)
        other = self.new_core()
        self.assertEqual(other.status()["state"], "RECOVERY_REQUIRED")
        new = str(uuid.uuid4())
        self.assertEqual(other.begin(new,"another",authorized=True)["reason"], "previous_operation_unresolved")
        self.assertEqual(other.heartbeat(self.op,"local-holder",2,authorized=True,held=True)["state"],"REJECTED")
        self.assertEqual(len(self.site.commands), 1)

    def test_failed_ledger_latches_and_cannot_issue_further_commands(self):
        self.arm()
        self.hold(1)
        with patch("calibration.lingao_attended.sqlite3.connect", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                self.jog.status(str(uuid.uuid4()))
        self.hold(2)
        self.assertTrue(self.site.revocation.is_set())
        self.assertEqual(len(self.site.commands), 1)
        self.assertEqual(self.settle()["reason"], "operation_journal_unavailable")

    def test_slow_grant_returning_after_revoke_cannot_generate_forward(self):
        pending, resume = threading.Event(), threading.Event()
        orig = self.site.begin
        def delayed(op, deadline):
            pending.set()
            resume.wait(2.)
            return orig(op, deadline)
        self.site.begin = delayed
        self.arm()
        thread = threading.Thread(target=lambda:self.hold(1))
        thread.start()
        try:
            self.assertTrue(pending.wait(1.))
            self.jog.release(self.op, "local-holder")
            self.assertTrue(self.site.revocation.is_set())
        finally:
            resume.set()
            thread.join(1.)
        self.assertFalse(self.site.commands)

    def test_operator_overshoot_never_verifies(self):
        self.arm()
        self.hold(1)
        self.frame(.05, x=.052, vx=.09)
        self.assertEqual(self.settle()["reason"], "unexpected_motion")

    def test_watchdog_never_writes_nonzero_and_late_seq_cannot_refresh_hold(self):
        self.arm()
        self.hold(1)
        self.frame(.03)
        self.assertEqual(len(self.site.commands), 1)
        self.hold(1)
        self.hold(2)
        self.assertEqual(self.jog.status(self.op)["reason"], "invalid_hold_sequence")
        self.assertEqual(len(self.site.commands), 1)

    def test_isolated_evidence_cannot_be_rendered_as_hardware_result(self):
        self.arm(authorized=False)
        self.site.evidence_scope = "hardware"
        restarted = self.new_core()
        result = restarted.status(self.op)
        self.assertEqual(result["phase"], "recovery_required")
        self.assertEqual(result["reason"], "foreign_commissioning_journal")
        self.assertIsNone(result["stopped"])


if __name__ == "__main__":
    unittest.main()
