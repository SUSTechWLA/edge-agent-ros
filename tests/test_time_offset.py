import unittest

import numpy as np

from calibration.time_offset import estimate_offsets


EPOCH = 1_810_200_341_022_637_209
TF = {
    "left": dict(parent_frame="color", child_frame="left", rotation_xyzw=[0, 0, 0, 1],
                 translation_m=[.07, 0, .02]),
    "right": dict(parent_frame="color", child_frame="right",
                  rotation_xyzw=[0, np.sin(.2), 0, np.cos(.2)], translation_m=[0, .04, -.06]),
}


def distance(t):
    return 1.65 + .27*np.sin(2.6*t) + .11*np.cos(4.3*t + .016*t*t)


def capture(path=distance, rate=32, drift_ns=0):
    tracks = {}
    for i, (role, offset) in enumerate((("camera", 0), ("left", 69_000_000), ("right", -113_000_000))):
        samples = []
        times = np.arange(i*.012, 12., 1 / (rate-i))
        for t in times:
            d = float(path(t))
            # Generate local lidar planes by inverse projection of the same camera-frame plane.
            normal = [0, 0, 1] if role != "right" else [-np.sin(.4), 0, np.cos(.4)]
            local_d = d - TF.get(role, {}).get("translation_m", [0, 0, 0])[2]
            stamp = EPOCH + round(t*1e9) - offset
            if role == "right":
                stamp += round(drift_ns * t / 12)
            samples.append(dict(stamp_ns=stamp, plane=dict(normal=normal, offset_m=local_d)))
        tracks[role] = samples
    return tracks


class ClockOffsetTests(unittest.TestCase):
    def test_separate_origins_cadences_and_both_offset_signs(self):
        traces = capture()
        result = estimate_offsets(traces, TF)
        self.assertEqual(result["status"], "VERIFIED")
        for role, expected in (("left", 69_000_000), ("right", -113_000_000)):
            src = result["sources"][role]
            self.assertLessEqual(abs(src["offset_ns"] - expected), 1_000_000)
            self.assertLessEqual(abs(src["holdout_offset_ns"] - expected), 1_000_000)
            self.assertLessEqual(src["holdout_rmse_m"], .002)
            self.assertGreater(src["uncertainty_ns"], 0)
            self.assertGreaterEqual(src["valid_raw_ns"][0], traces[role][0]["stamp_ns"])
            self.assertLessEqual(src["valid_raw_ns"][1], traces[role][-1]["stamp_ns"])

    def test_static_and_monotone_rejected(self):
        for trajectory in (lambda t: 1.4, lambda t: 1.4+.1*t,
                           lambda t: 1.4+.1*t+.008*np.sin(94*t)):
            with self.assertRaisesRegex(ValueError, "insufficient motion excitation"):
                estimate_offsets(capture(trajectory), TF)

    def test_periodic_alias_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous|inconsistent"):
            estimate_offsets(capture(lambda t: 1.5+.23*np.sin(10*np.pi*t), rate=150), TF)

    def test_clock_drift_on_second_lidar_never_verifies_pair(self):
        with self.assertRaisesRegex(ValueError, "right: held-out offset inconsistent"):
            estimate_offsets(capture(drift_ns=115_000_000), TF)

    def test_missing_samples_duplicate_stamps_and_disjoint_clocks(self):
        for invalid in ("left_missing", "left_epoch", "camera_duplicate", "camera_gaps"):
            with self.subTest(invalid=invalid):
                traces = capture()
                if invalid == "left_missing":
                    traces.pop("left")
                elif invalid == "left_epoch":
                    for row in traces["left"]:
                        row["stamp_ns"] += 900_000_000_000
                elif invalid == "camera_duplicate":
                    traces["camera"][1]["stamp_ns"] = traces["camera"][0]["stamp_ns"]
                else:
                    traces["camera"] = [r for i, r in enumerate(traces["camera"]) if i % 20 < 3]
                with self.assertRaises(ValueError):
                    estimate_offsets(traces, TF)

    def test_clock_reset_or_wrong_plane_never_passes(self):
        traces = capture()
        traces["left"][-1]["stamp_ns"] = traces["left"][0]["stamp_ns"]
        with self.assertRaisesRegex(ValueError, "regressing"):
            estimate_offsets(traces, TF)
        traces = capture()
        for row in traces["left"]:
            row["plane"]["normal"] = [1, 0, 0]
        with self.assertRaises(ValueError):
            estimate_offsets(traces, TF)

    def test_timestamp_type_bounds_and_clock_gap(self):
        for value in (True, -.3, 1.0, -1, 2**63, str(EPOCH)):
            with self.subTest(stamp=value):
                traces = capture()
                traces["right"][2]["stamp_ns"] = value
                with self.assertRaisesRegex(ValueError, "invalid acquisition stamp"):
                    estimate_offsets(traces, TF)
        traces = capture()
        traces["right"][100:180] = []
        with self.assertRaisesRegex(ValueError, "discontinuous"):
            estimate_offsets(traces, TF)

    def test_large_time_local_mismatch_and_very_slow_cadence(self):
        traces = capture()
        traces["right"][80]["plane"]["offset_m"] += .13
        with self.assertRaisesRegex(ValueError, "local plane mismatch"):
            estimate_offsets(traces, TF)
        with self.assertRaises(ValueError):
            estimate_offsets(capture(rate=7), TF)

    def test_long_recording_stable_or_drifting(self):
        traces = capture()
        for role, rows in traces.items():
            original = list(rows)
            for repeat in range(1, 8):
                rows.extend(dict(row, stamp_ns=row["stamp_ns"] + repeat*12_000_000_000)
                            for row in original)
        # Drop small seams from the analytic restart by using a genuinely continuous signal.
        offsets = {"camera": 0, "left": 69_000_000, "right": -113_000_000}
        for role, rows in traces.items():
            for row in rows:
                t = (row["stamp_ns"]-EPOCH+offsets[role])/1e9
                row["plane"] = dict(row["plane"], offset_m=distance(t)
                                    - TF.get(role, {}).get("translation_m", [0, 0, 0])[2])
        proof = estimate_offsets(traces, TF)
        self.assertLessEqual(abs(proof["sources"]["left"]["offset_ns"]-offsets["left"]), 1_000_000)
        for row in traces["right"]:
            row["stamp_ns"] += round((row["stamp_ns"] - EPOCH)*.0015)
        with self.assertRaisesRegex(ValueError, "held-out offset inconsistent"):
            estimate_offsets(traces, TF)


if __name__ == "__main__":
    unittest.main()
