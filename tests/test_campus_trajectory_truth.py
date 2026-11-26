import math
import unittest

from system.campus_trajectory_truth import measure


def sample(i, xy, speed, yaw=0.):
    return {"type": "sample", "rover": {"xy": xy, "stamp_ns": i * 100_000_000,
                                        "frame": "world", "linear_mps": speed, "yaw": yaw}}


class ExecutedTrajectoryTest(unittest.TestCase):
    def test_world_xy_arc(self):
        track = [sample(i, [math.sin(i / 50), 1 - math.cos(i / 50)], .2, i / 50) for i in range(55)]
        result = measure(track)
        self.assertTrue(result["distributed_moving_turn"])
        self.assertGreater(result["largest_continuous_turn"]["turn_deg"], 50)
        self.assertAlmostEqual(result["distance_m"], 1.08, places=3)

    def test_stop_spin_then_straight_does_not_pass(self):
        track = [sample(i, [.02 * i, 0], .2) for i in range(26)]
        track += [sample(26 + i, [.5, 0], 0., i * math.pi / 20) for i in range(11)]
        track += [sample(37 + i, [.5, .02 * i], .2, math.pi / 2) for i in range(26)]
        result = measure(track)
        self.assertFalse(result["distributed_moving_turn"])
        self.assertAlmostEqual(result["low_speed_body_rotation_deg"], 90)

    def test_single_sharp_corner_does_not_pass(self):
        track = [sample(i, [i / 50, 0], .2) for i in range(27)]
        track += [sample(26 + i, [.52, i / 50], .2) for i in range(1, 27)]
        self.assertFalse(measure(track)["distributed_moving_turn"])


if __name__ == "__main__":
    unittest.main()
