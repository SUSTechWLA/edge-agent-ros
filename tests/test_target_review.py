"""Keep optical geometry, pixel origin and grounded mask semantics verifiable offline."""
import unittest

try:
    from task_recorder.target_review import includes, mask_rows, unproject
except ImportError:
    mask_rows = None


@unittest.skipUnless(mask_rows, "requires render dependencies")
class TargetReviewTest(unittest.TestCase):
    def test_mask_pixel_and_open_bounds_use_original_image_coordinates(self):
        mask = dict(stride=4, grid_width=3, grid_height=2, runs=[[0,1,3], [1,0,1]])
        self.assertEqual(mask_rows(mask, (10,5), (9,1), (4,0,10,4)), {0:[(1,3)], 1:[(0,1)]})
        with self.assertRaisesRegex(ValueError,"outside source mask"):
            mask_rows(mask,(10,5),(0,0),(0,0,10,5))
        with self.assertRaisesRegex(ValueError,"outside boxed image"):
            mask_rows(mask,(10,5),(4,4),(4,0,10,4))

    def test_optical_xyz_uses_axial_depth_and_metric_intrinsics(self):
        self.assertEqual(unproject(10,32,2,[5,0,20,0,4,30,0,0,1]),(-4.,1.,2))

    def test_catalog_match_requires_complete_identity(self):
        self.assertTrue(includes({"tools":[{"name":"perception.mark"}]},"perception.mark"))
        self.assertFalse(includes({"tools":[{"name":"perception.mark_legacy"}]},"perception.mark"))
