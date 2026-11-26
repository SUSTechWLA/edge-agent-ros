"""Fixed-target metric viewpoint planning without a robot transport."""
from copy import deepcopy
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from calibration import board_solver, circle_target, intrinsic_collection, intrinsic_information, intrinsic_joint_fit, view_planner
from calibration.methods import selected
from calibration.targets import rotation_xyzw
from edge_contracts.spatial_facts import rotation


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.k = np.array([[520.,0.,320.],[0.,520.,240.],[0.,0.,1.]])
        self.points = np.array([[-.62,-.4,2.],[.62,-.4,2.],[.62,.4,2.],[-.62,.4,2.]])
        uv = self.points @ self.k.T
        self.plane = dict(center_m=[0.,0.,2.], normal=[0.,0.,1.], offset_m=2., reprojection_rms_px=.4,
                          corners_px=(uv[:,:2]/uv[:,2:]).tolist())
        self.camera = dict(width=640, height=480, k=self.k, plane=self.plane)
        self.profile = {"solver":{},"robot_id":"rover","base_frame":"base_link",
                        "camera":{"serial":"d435-unit","frame":"d435_optical"}}
        self.obs = {"archive_sha256":"a"*64, "camera_stamp_ns":"1234", "camera_frame":"d435_optical",
                    "profile_sha256":"b"*64, "method_sha256":"c"*64, "camera_pose_observed":True}

    def plan(self, prior=(), op="inspect/step/1"):
        return view_planner.plan_views("inspect", op, self.obs, self.camera, list(prior), self.profile)

    def test_camera_views_preserve_observed_features_and_are_diverse(self):
        result = self.plan()
        self.assertGreater(result["candidate_count"],2)
        self.assertLessEqual(result["candidate_count"],6)
        self.assertFalse(result["base_goal_available"] or result["reachability_checked"] or result["lidar_visibility_predicted"])
        self.assertEqual(result["goal_frame"], "d435_optical")
        self.assertEqual(result["candidate_refs"], self.plan()["candidate_refs"])
        self.assertNotEqual(result["candidate_refs"], self.plan(op="inspect/step/2")["candidate_refs"])
        seen = [{"camera":self.plane}]
        for row in result["candidates"]:
            p = row["camera_pose"]
            self.assertNotIn("base_goal",row)
            r = np.array(rotation([0,0,0], p["rotation_xyzw"]))[:,:3]
            pts = (self.points-np.array(p["position_m"])) @ r
            uv = pts @ self.k.T
            uv = uv[:,:2]/uv[:,2:]
            self.assertTrue(np.all(uv >= 0) and np.all(uv < [640,480]))
            observed = {"camera":row["predicted_camera"]}
            self.assertTrue(board_solver.distinct_pose(observed,seen))
            seen.append(observed)
        for row in self.plan(seen)["candidates"]:
            self.assertTrue(board_solver.distinct_pose({"camera":row["predicted_camera"]}, seen))

    def test_unmeasured_mount_yields_only_optical_goals(self):
        r = np.array([[0.,0.,1.],[-1.,0.,0.],[0.,-1.,0.]])
        t = np.array([.20,-.06,.55])
        mount = {"method":"surveyed_mount","reference":"fixture-survey-17","robot_id":"rover","serial":"d435-unit",
                 "parent_frame":"base_link","child_frame":"d435_optical","translation_m":t.tolist(),
                 "rotation_xyzw":rotation_xyzw(r)}
        self.profile["extrinsics"] = {"rgbd":mount}
        result = self.plan()
        self.assertTrue(result["base_goal_available"])
        for row in result["candidates"]:
            goal = row["base_goal"]
            yaw = goal["yaw_rad"]
            c,s = math.cos(yaw), math.sin(yaw)
            rz = np.array([[c,-s,0],[s,c,0],[0,0,1]])
            final_optical = r @ np.array(row["camera_pose"]["position_m"]) + t
            np.testing.assert_allclose(rz @ t + [goal["x_m"],goal["y_m"],0.], final_optical, atol=1e-10)
            self.assertEqual(goal["mount_reference"], mount["reference"])
        for changed in ({"serial":"other"},{"reference":None},{"parent_frame":"map"},{"rotation_xyzw":[0,0,0,0]}):
            self.profile["extrinsics"]["rgbd"]={**mount,**changed}
            self.assertFalse(self.plan()["base_goal_available"])

    def test_stale_source_geometry_and_infeasible_views(self):
        bad = deepcopy(self.plane)
        bad["corners_px"][0][0] = -2.
        with self.assertRaisesRegex(ValueError,"footprint"):
            view_planner.measured_footprint(bad,self.k,np.array([]),640,480)
        proposed = [(np.zeros(3),np.eye(3),None), (np.array([3.,0,0]),np.eye(3),None),
                    (np.array([0,0,4.]),np.diag([-1.,1.,-1.]),None)]
        with patch.object(view_planner, "candidate_poses", return_value=proposed):
            result=self.plan()
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["rejected"], {"duplicate":1,"cropped_or_small":1,"back_facing":1})
        self.camera = {}
        self.assertEqual(self.plan()["outcome"], "no_measured_camera_pose")

    def test_nominal_scanner_envelope_rejects_unobservable_goals(self):
        self.profile["extrinsics"] = {"rgbd":{
            "method":"surveyed_mount", "reference":"camera-survey", "robot_id":"rover", "serial":"d435-unit",
            "parent_frame":"base_link", "child_frame":"d435_optical", "translation_m":[0,0,.4],
            "rotation_xyzw":[-.5,.5,-.5,.5]}}
        self.profile["solver"].update(lidar_frames={"front":"front_scan"}, capture_range_m=[.2,10],
                                      four_hole_board={"hole_diameter_m":.24})
        self.plane["holes_m"] = self.points.tolist()
        envelope = {"frame":"front_scan", "reference":"scanner-survey", "translation_m":[0,0,.4],
                    "rotation_xyzw":[0,0,0,1], "elevation_deg":[-70,70]}
        self.profile["capture_view_envelope"] = envelope
        result = self.plan()
        self.assertTrue(result["base_goal_available"])
        self.assertFalse(result["lidar_visibility_predicted"])
        self.assertEqual(result["nominal_lidar_envelope_ref"], "scanner-survey")
        for low,high in ((50,70),(-1,1)):
            envelope["elevation_deg"] = [low,high]
            result = self.plan()
            self.assertEqual(result["candidate_count"], 0)
            self.assertGreater(result["rejected"]["outside_sensor_envelope"], 0)
        envelope["frame"] = "unrelated_sensor"
        with self.assertRaisesRegex(ValueError,"binding_mismatch"):
            self.plan()

    def test_parked_partial_target_can_supply_recovery_goals(self):
        root = Path(__file__).resolve().parents[1]
        profile = json.loads((root/"calibration/station/gazebo-fixed-rover.json").read_bytes())
        profile["solver"]["four_hole_board"] = profile["four_hole_board"]
        with np.load(root/"tests/fixtures/fixed-rover-partial-markers.npz",allow_pickle=False) as frame:
            rgb,k = frame["rgb"],frame["k"]
        features = circle_target.recognize("four_hole_board",rgb,k,{"solver":profile["solver"]},{})[0]["features"]
        self.assertNotIn("board_plane",features)
        camera = dict(width=rgb.shape[1],height=rgb.shape[0],k=k,plane=features["planning_plane"])
        obs = dict(self.obs,camera_frame=profile["camera"]["frame"])
        profile.pop("capture_navigation")
        tracking = view_planner.plan_views("track","track/step/1",obs,camera,[],profile)
        self.assertFalse(tracking["base_goal_available"])  # A cropped start cannot promise continuous full-board visibility.
        profile.pop("view_tracking")
        plan = view_planner.plan_views("recover","recover/step/1",obs,camera,[],profile)
        self.assertTrue(plan["base_goal_available"])
        self.assertGreater(plan["rejected"]["low_marker_resolution"],0)
        self.assertTrue(all(o["min_marker_module_px"] >= 2.5 for o in plan["view_options"]))

    def test_path_projection_catches_turning_away_even_when_both_endpoints_see_target(self):
        mount = (np.array([[0.,0.,1.],[-1.,0.,0.],[0.,-1.,0.]]), np.array([0.,0.,.4]), "mount")
        visible = dict(x_m=.16, y_m=.022, yaw_rad=-.10)
        side = dict(x_m=.02, y_m=.60, yaw_rad=-.20)
        for pose, valid in ((visible,True),(side,False)):
            margin = view_planner.path_margin(self.points, pose, mount, self.k, np.array([]), 640, 480)
            self.assertEqual(margin > 19.2, valid)

    def test_active_views_require_clock_matched_pose_and_corridor_clearance(self):
        root = Path(__file__).resolve().parents[1]
        profile = json.loads((root/"calibration/station/gazebo-fixed-rover.json").read_bytes())
        profile["solver"]["four_hole_board"] = profile["four_hole_board"]
        marker = circle_target.SIGNS
        tags = np.concatenate([p*[.55,.35]+.10*marker for p in marker])
        points = np.column_stack([tags[:,0],tags[:,1]-.10,np.full(len(tags),2.93)])
        uv = points @ self.k.T
        plane = dict(center_m=[0,-.1,2.93],normal=[0,0,1],offset_m=2.93,reprojection_rms_px=.3,
                     corners_px=(uv[:,:2]/uv[:,2:]).tolist(),all_marker_corners_m=points.tolist(),
                     holes_m=[[x*.25, y*.20-.10, 2.93] for x,y in marker],
                     marker_modules_per_side=8)
        obs=dict(self.obs,camera_stamp_ns="1000000000",camera_frame=profile["camera"]["frame"])
        camera=dict(width=640,height=480,k=self.k,plane=plane)

        def plan():
            return view_planner.plan_views("a","op",obs,camera,[],profile)

        self.assertEqual(plan()["outcome"],"missing_time_matched_boot_pose")
        pose=dict(position_m=[0,0,0],orientation_xyzw=[0,0,0,1],yaw_rad=0,frame="boot_odom",
                  epoch="a"*32+":1",stamp_ns=1_000_000_000)
        scan=dict(frame="fixture_clearance",stamp_ns=1_000_000_000,range_m=8,
                  angle_min=-math.pi,angle_step=math.tau/360,ranges_m=[None]*360)
        camera["navigation"]=dict(robot=pose,scan=scan)
        candidates=plan()
        self.assertTrue(candidates["reachability_checked"])
        self.assertTrue({"left","right"} <= {row["board_view"] for row in candidates["view_options"]})
        scan["ranges_m"]=[.25]*360
        blocked=plan()
        self.assertFalse(blocked["base_goal_available"])
        self.assertGreater(blocked["rejected"]["outside_observed_clearance"],0)

        scan["ranges_m"]=[None]*360
        collection = dict(aim="roll_left",joint=True,needs_pair=False,missing_cells=[],verified_move=True,
                          samples=[{"points":plane["corners_px"],"tilt_deg":[0.,0.]}]*3)
        reachable = view_planner.plan_views("a","op",obs,camera,[],profile,collection=collection)
        self.assertEqual(reachable["outcome"],"operator_angle_needed")
        self.assertGreater(reachable["capture_reachability"]["evaluated_safe_views"],0)
        self.assertEqual(reachable["capture_reachability"]["useful_safe_views"],0)
        self.assertFalse(reachable["base_goal_available"])
        collection["verified_move"] = False
        self.assertFalse(view_planner.plan_views("a","op",obs,camera,[],profile,
                                                collection=collection)["camera_operator_needed"])
        camera.pop("navigation")
        collection["verified_move"] = True
        incomplete = view_planner.plan_views("a","op",obs,camera,[],profile,collection=collection)
        self.assertFalse(incomplete["reachability_checked"])
        self.assertNotIn("capture_reachability",incomplete)

    def test_observability_is_scored_for_the_same_training_or_holdout_slot(self):
        method=selected({"calibration_method":"four_hole_board"})
        prior=[{"camera":dict(self.plane,center_m=[0,0,z],offset_m=z)} for z in (1.0,1.4)]
        for role in ("fit","holdout"):
            plan=view_planner.plan_views("a","op",self.obs,self.camera,prior,self.profile,method=method)
            options=plan["view_options"]
            self.assertTrue(0<len(options)<=4)
            self.assertTrue(all(o["sample_role"]==role for o in options))
            if role=="fit":
                self.assertTrue(any(o["anchor_ratio"]>=circle_target.ANCHOR_MIN_RATIO and abs(o["image_offset_px"])>1
                                    for o in options))
            prior.append({"camera":self.plane})

    def test_mobile_joint_reserves_paired_holdout_before_losing_overlap_to_tilt(self):
        fit = {"role":"fit", "cells":sorted(intrinsic_collection.BOTTOM), "tilt_deg":[0,0], "lidar":{}}
        held = {**fit, "role":"holdout"}
        samples = [fit]*4
        self.assertEqual(intrinsic_collection.report(samples,True)["next_pose"],"pitch_down")
        self.assertEqual(intrinsic_collection.report(samples,True,mobile=True)["next_pose"],"holdout")
        samples += [held]*3
        pending = intrinsic_collection.report(samples,True,mobile=True)
        self.assertEqual(pending["next_pose"],"pitch_down")
        self.assertFalse(pending["camera_fit_ready"])
        samples += [dict(fit,tilt_deg=tilt,lidar=None) for tilt in ([-20,-15],[20,15],[0,0])]
        self.assertTrue(intrinsic_collection.report(samples,True,mobile=True)["camera_fit_ready"])

    def test_information_favors_a_different_plane_orientation_and_diminishes_on_repeats(self):
        flat = self.points
        pitch = .45
        turn = np.array([[1.,0,0],[0,math.cos(pitch),-math.sin(pitch)],[0,math.sin(pitch),math.cos(pitch)]])
        centre = flat.mean(axis=0)
        tilted = (flat-centre)@turn.T+centre
        size, distorted = (640,480), np.array([])
        initial = intrinsic_information.accumulated_information(
            [{"all_marker_corners_m":flat.tolist()}],self.k,distorted,size)
        tilt_gain = intrinsic_information.information_gain(initial,tilted,self.k,distorted,size)
        flat_gain = intrinsic_information.information_gain(initial,flat,self.k,distorted,size)
        repeated = initial + intrinsic_information.view_information(tilted,self.k,distorted,size)
        self.assertGreater(tilt_gain,flat_gain)
        self.assertLess(intrinsic_information.information_gain(repeated,tilted,self.k,distorted,size),tilt_gain)
        old = {"points":self.plane["corners_px"],"tilt_deg":[0.,0.]}
        self.assertFalse(intrinsic_collection.repeat_view(dict(old,tilt_deg=[20.,0.]),old,4.5))

    def test_metric_refinement_recovers_focal_length_and_lidar_depth_bias(self):
        spacing = np.array([.5,.4])
        corners = np.concatenate([p*[.55,.35]+.1*circle_target.SIGNS for p in circle_target.SIGNS])
        object_points = np.column_stack([corners,np.zeros(len(corners))])
        board_holes = np.column_stack([circle_target.SIGNS * spacing/2,np.zeros(4)])
        reference_t = np.array([.02,.20,.045])
        initial_t = [.02,.20,.010]
        initial_mount = dict(transforms={"front":dict(translation_m=initial_t,rotation_xyzw=[0,0,0,1])})
        images, paired = [], []
        sample_ids = [str(i) for i in range(7)]
        for i, sample_id in enumerate(sample_ids):
            board_r = np.array([.13*(i-2),.11*(i%3-1),.07*(i%2)])
            board_t = np.array([.09*(i-3),.08*(i%3-1),2.4+.14*i])
            uv, _ = cv2.projectPoints(object_points,board_r,board_t,self.k,None)
            images.append(uv[:,0,:])
            rotation = cv2.Rodrigues(board_r)[0]
            centres = board_holes @ rotation.T + board_t
            def plane(holes):
                centre = holes.mean(axis=0)
                return dict(holes_m=holes.tolist(),center_m=centre.tolist(),normal=rotation[:,2].tolist(),
                            offset_m=float(centre @ rotation[:,2]))
            if i<4:
                paired.append(dict(id=sample_id,camera=plane(centres),lidars={"front":plane(centres-reference_t)}))
        initial_k = self.k.copy()
        initial_k[0,0] -= 7.
        initial_k[1,1] -= 7.
        refined, _, _, measured_t = intrinsic_joint_fit.refine(
            images,object_points,paired,sample_ids,initial_k,np.zeros(5),initial_mount,
            dict(pixel_sigma_px=.6,hole_sigma_m=.006,hole_spacing_m=spacing))
        np.testing.assert_allclose(refined,self.k,atol=.03)
        np.testing.assert_allclose(measured_t,reference_t,atol=.0003)

    def test_unique_multiview_holes_recover_measured_weak_anchor_poses(self):
        root=Path(__file__).resolve().parents[1]
        obs=json.loads((root/"tests/fixtures/fixed-four-hole-weak-anchors.json").read_bytes())["observations"]
        profile=json.loads((root/"calibration/station/gazebo-fixed-rover.json").read_bytes())
        config=dict(profile["solver"],four_hole_board=profile["four_hole_board"])
        method=selected(profile)
        samples=method.sample_groups(obs)
        quality=circle_target.score_view_geometry([o["camera"] for o in samples["fit"]])
        self.assertLess(quality["value"],quality["preferred_minimum"])
        candidate=circle_target.fit_extrinsics(samples["fit"],config)
        self.assertEqual(candidate["transforms"]["front"]["initializer"],"unique_training_hole_correspondence")
        result=circle_target.verify_extrinsics(candidate,samples["holdout"],config)
        self.assertLess(result["transforms"]["front"]["validation"]["max_hole_error_m"],.025)
        self.assertFalse(set(result["fit_ids"]) & set(result["validation_ids"]))

    def test_indistinguishable_hole_assignments_are_rejected(self):
        obs=[]
        for z in (1.,1.3,1.6):
            points=[[x,y,z] for x in (-.25,.25) for y in (-.20,.20)]
            plane=dict(normal=[0,0,1],center_m=[0,0,z],offset_m=z,holes_m=points)
            obs.append(dict(camera=plane,lidars={"front":plane}))
        with self.assertRaisesRegex(ValueError,"ambiguous"):
            circle_target._constellation_fit(obs,"front",np.array([.5,.4]))
