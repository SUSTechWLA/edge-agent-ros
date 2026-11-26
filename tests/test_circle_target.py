"""Numerical regressions for spatial four-hole calibration, including negative controls."""
import copy
from pathlib import Path
import unittest

import cv2
import numpy as np

from calibration import circle_target as board


from system.four_hole_scene import K, HOLES, POSES, PROFILE, SENSORS, cloud, render


class CircleTargetTest(unittest.TestCase):
    def test_camera_requires_complete_decoding_and_metric_geometry(self):
        axis, t = POSES[0]
        rgb = render(axis,t)
        marks = board.marker_corners(rgb)
        self.assertEqual(set(marks),{1,2,3,4})
        def read(image, profile):
            return board.recognize("four_hole_board",image,K,{"solver":profile},{})[0]["features"]
        self.assertEqual(read(rgb,{})["condition"],"board_geometry_required")
        partial = read(render(axis,t,omit=(4,)),PROFILE)
        self.assertEqual(partial["condition"],"partial_markers")
        self.assertNotIn("board_plane",partial)
        mismatch = copy.deepcopy(PROFILE)
        mismatch["four_hole_board"]["marker_size_m"] = .16
        self.assertEqual(read(rgb,mismatch)["condition"],"marker_geometry_mismatch")
        plane = read(rgb,PROFILE)["board_plane"]
        np.testing.assert_allclose(plane["center_m"], t, atol=.004)
        with self.assertRaises(ValueError):
            board.dimensions({"four_hole_board":{"hole_diameter_m":float("nan")}})

    def test_point_cloud_requires_four_independent_circular_rims(self):
        axis,t = POSES[1]
        for holes in ([], HOLES[:3], HOLES*np.array([1.15,1.])):
            self.assertIsNone(board.observe_lidar(cloud(axis,t,"left",holes),PROFILE))
        sample = board.observe_lidar(cloud(axis,t,"left"),PROFILE)
        self.assertIsNotNone(sample)
        rotation,offset = SENSORS["left"]
        np.testing.assert_allclose(rotation@sample["center_m"]+offset, t, atol=.004)

    def test_partial_decoded_pixels_only_support_view_planning(self):
        axis,t=POSES[2]
        def features(omit):
            return board.recognize("four_hole_board",render(axis,t,omit=omit),K,{"solver":PROFILE},{})[0]["features"]
        for omitted in ((4,),(4,3)):
            result=features(omitted)
            self.assertEqual(result["condition"],"partial_markers")
            self.assertNotIn("board_plane",result)
            plane=result["planning_plane"]
            self.assertNotIn("holes_m",plane)
            self.assertEqual(plane["corner_count"],4*(4-len(omitted)))
            self.assertEqual(np.shape(plane["all_marker_corners_m"]),(16,3))
            self.assertEqual(plane["usage"],"view_planning_only")
            self.assertEqual(plane["marker_modules_per_side"],8)
            np.testing.assert_allclose(plane["center_m"],t,atol=.012)
            rot=cv2.Rodrigues(np.array(axis))[0]
            np.testing.assert_allclose(plane["projective_hole_centers_m"],HOLES@rot[:,:2].T+t,atol=.014)
        self.assertNotIn("planning_plane",features((1,2,4)))
        marks=board.marker_corners(render(axis,t,omit=(4,3)))
        marks[2]=marks[1]+np.array([1.,0.])
        self.assertIsNone(board._planning_plane(marks,K,PROFILE))
        marks=board.marker_corners(render(axis,t,omit=(3,)))
        marks[2][0]+=[11.,-8.]
        self.assertIsNone(board._planning_plane(marks,K,PROFILE))
        image=render(axis,t,omit=(4,))
        self.assertFalse(board.marker_corners(np.concatenate([image,image],axis=1)))

    def test_original_gazebo_partial_capture_returns_measured_and_predicted_geometry_separately(self):
        # Fixed-board/rover run fixed-cal-e3b79f8d8516 stalled here after its fifth move.
        path=Path(__file__).parent/"fixtures"/"fixed-rover-partial-markers.npz"
        with np.load(path,allow_pickle=False) as frame:
            result=board.recognize("four_hole_board",frame["rgb"],frame["k"],{"solver":PROFILE},{})[0]["features"]
        self.assertEqual(result["marker_ids"],[1,3,4])
        self.assertNotIn("board_plane",result)
        self.assertEqual(result["condition"],"partial_markers")
        pose=result["planning_plane"]
        self.assertEqual((pose["corner_count"],len(pose["all_marker_corners_m"])),(12,16))
        self.assertLess(pose["reprojection_rms_px"],1.0)
        self.assertEqual(pose["marker_modules_per_side"],8)

    def test_two_sensors_multiview_recovery_and_disjoint_validation(self):
        observations=[]
        for i,(axis,t) in enumerate(POSES):
            marks = board.marker_corners(render(axis,t))
            cam = board._camera_plane(marks,K,PROFILE)
            lidars = {role:board.observe_lidar(cloud(axis,t,role),PROFILE) for role in SENSORS}
            self.assertIsNotNone(cam)
            self.assertTrue(all(p is not None for p in lidars.values()))
            observations.append(dict(id=str(i),camera=cam,lidars=lidars))
        fit = board.fit_extrinsics(observations[:3],PROFILE)
        result = board.verify_extrinsics(fit,observations[3:],PROFILE)
        self.assertEqual(result["status"],"VERIFIED")
        for role,(rot,t) in SENSORS.items():
            tf=result["transforms"][role]
            np.testing.assert_allclose(tf["translation_m"],t,atol=.01)
            self.assertLess(tf["validation"]["max_hole_error_m"],.012)
        with self.assertRaisesRegex(ValueError,"repeated observation"):
            board.verify_extrinsics(fit,[observations[0], observations[3]],PROFILE)
        tampered = copy.deepcopy(fit)
        tampered["transforms"]["right"]["translation_m"][0]+=.05
        with self.assertRaises(ValueError):
            board.verify_extrinsics(tampered,observations[3:],PROFILE)


if __name__ == "__main__":
    unittest.main()
