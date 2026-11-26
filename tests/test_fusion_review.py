"""Check lineage and per-source cell masks in archived stationary fusion."""
import unittest

try:
    from task_recorder.fusion_review import validate_result
except ImportError:
    validate_result = None


@unittest.skipUnless(validate_result, "requires render dependencies")
class FusionReviewTest(unittest.TestCase):
    def setUp(self):
        source = dict(first_ns="120", last_ns="170", translation_m=[0, 0, .2],
                      quaternion_xyzw=[0, 0, 0, 1])
        self.data = dict(type="perception.stationary_fusion", actor_ref="r", body_frame="body",
                         clock_id="unix", source_ids=["a", "b"], sources={"a": source, "b": dict(source)},
                         witness=dict(actor_ref="r", body_frame="body", clock_id="unix", sample_count=5,
                                      first_ns="100", last_ns="190", max_linear_mps=0., max_angular_radps=0.,
                                      max_displacement_m=0.), occupied_cells=3, overlap_cells=1,
                         grid=dict(width=4, height=4, resolution_m=.2, unknown="unobserved", origin_xy_m=[-.4, -.4],
                                   cells=[[0, 0, 1, 1], [1, 0, 2, 3], [2, 1, 1, 2]]))

    def test_shared_cell_counts_once_per_contributing_layer(self):
        self.assertEqual(validate_result(self.data), [2, 2])

    def test_impossible_or_duplicated_cell_is_refused(self):
        self.data["grid"]["cells"][1][2] = 1
        with self.assertRaisesRegex(ValueError, "mask mismatch"):
            validate_result(self.data)
        self.data["grid"]["cells"][1] = [0, 0, 2, 3]
        with self.assertRaisesRegex(ValueError, "coordinate"):
            validate_result(self.data)

    def test_witness_must_cover_source_capture(self):
        self.data["witness"]["first_ns"] = "140"
        with self.assertRaisesRegex(ValueError, "does not cover"):
            validate_result(self.data)
