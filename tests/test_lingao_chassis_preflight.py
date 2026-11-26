import json
import math
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import lingao_chassis_preflight as preflight


class ChassisPreflightTest(unittest.TestCase):
    def setUp(self):
        self.now = time.time_ns()
        tick = time.monotonic()
        self.samples = [(self.now-i*100_000_000, tick-i/10, ("odom", "base"), 0., 0., (0., 0., 0.))
                        for i in reversed(range(20))]

    def test_requires_fresh_continuous_measured_stop(self):
        self.assertTrue(preflight.stopped(self.samples, self.now)[0])
        for invalid in (self.samples[:4], self.samples[:5]+self.samples[12:],
                        self.samples[:-1]+self.samples[-2:-1], self.samples[-1:]*20):
            self.assertFalse(preflight.stopped(invalid, self.now)[0])
        self.assertFalse(preflight.stopped(self.samples, self.now+1_000_000_000)[0])
        self.assertFalse(preflight.stopped(self.samples, self.now-1000)[0])

    def test_motion_inside_window_is_not_hidden_by_last_sample(self):
        for column, value in ((2, ("another", "base")), (3, .03), (4, .05),
                              (3, math.nan), (5, (.1, 0., 0.))):
            rows = self.samples.copy()
            row = list(rows[8])
            row[column] = value
            rows[8] = tuple(row)
            self.assertFalse(preflight.stopped(rows, self.now)[0])

    def test_report_replacement_and_write_failure_preserve_last_complete_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "state.json"
            preflight.write_report(path, {"sequence": 1})
            preflight.write_report(path, {"sequence": 2})
            with self.assertRaises(ValueError):
                preflight.write_report(path, {"bad": math.nan})
            with patch.object(preflight.os, "replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    preflight.write_report(path, {"sequence": 3})
            self.assertEqual(json.loads(path.read_text()), {"sequence": 2})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_watch_keeps_one_context_and_reobserves_each_round(self):
        ros, signals = Mock(), Mock()
        reports = []
        argv = ["observer", "--task-id", "task", "--robot-id", "lingao", "--out", "unused", "--watch"]
        with patch.dict("sys.modules", {"rclpy": ros, "rclpy.signals": signals}), patch("sys.argv", argv), \
                patch.object(preflight, "inspect_ros", side_effect=lambda _: [
                    preflight.check("observed", False, "missing")]) as observe, \
                patch.object(preflight, "web_status", return_value=preflight.check("web", True, "idle")), \
                patch.object(preflight, "write_report", side_effect=lambda path, r: reports.append(r)), \
                patch.object(preflight.time, "sleep", side_effect=[None, None, KeyboardInterrupt]), \
                patch.dict(preflight.os.environ, {}, clear=False), self.assertRaises(KeyboardInterrupt):
            preflight.main()
        ros.init.assert_called_once_with(signal_handler_options=signals.SignalHandlerOptions.NO)
        ros.shutdown.assert_called_once_with()
        self.assertEqual(observe.call_count, 3)
        self.assertEqual([r["status"] for r in reports], ["blocked"]*3)

    def test_advisory_probe_does_not_block_vendor_motion_method(self):
        checks = [preflight.check("Nav2", False, "no server", required=False),
                  preflight.check("passive rear", None, "unknown visibility", required=False)]
        with patch.object(preflight, "inspect_ros", return_value=checks), \
                patch.object(preflight, "web_status", return_value=preflight.check("web", True, "idle")):
            result = preflight.snapshot(SimpleNamespace(task_id="task", robot_id="rover"))
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["checks"][0]["ok"])
        self.assertIsNone(result["checks"][1]["ok"])
        self.assertEqual(set(result["checks"][0]), {"label", "ok", "detail"})

    def test_depth_probe_checks_real_pixels_excluding_row_padding(self):
        msg = SimpleNamespace(width=2, height=2, step=6, encoding="16UC1",
                              data=bytes([0,0,0,8,9,9, 3,0,0,0,9,9]))
        self.assertEqual(preflight.depth_sample(msg), (2,4))
        msg.data = msg.data[:-1]
        self.assertEqual(preflight.depth_sample(msg), (0,0))


if __name__ == "__main__":
    unittest.main()
