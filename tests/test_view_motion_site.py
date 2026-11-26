"""Original task provenance, closed-loop commands and independent protection for fixed-board pose motions."""
import math
import hashlib
import json
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"agent"))
from physical_ledger import PhysicalError
from navigation.view_motion_site import CONTROL, absolute_goal, guard_site, resolve_goal
from navigation import target_tracking
from navigation.view_motion_actors import Poses, command_to_goal, scan_clear
from navigation.wheel_gyro_odometry import WheelGyroOdometry


class MotionTest(unittest.TestCase):
    def setUp(self):
        self.ref="view."+"a"*20
        self.goal={"reference_frame":"base_link","x_m":-.40,"y_m":.25,"yaw_rad":.18,"mount_reference":"fixture"}
        self.evidence={"outcome":"candidate_views","base_goal_available":True,"goal_frame":"base_link",
                       "source":{"task_id":"cal","operation_id":"cal/step/1","planner_sha256":"f"*64,
                                 "archive_sha256":"d"*64,"stamp_ns":"123456"},
                       "candidates":[{"candidate_ref":self.ref,"base_goal":self.goal}]}
        self.step={"tool":"plan_capture_views","status":"SUCCEEDED","binding":"original",
                   "operation_id":"cal/step/1","evidence":self.evidence}
        self.task={"id":"cal","state":"PLANNING","steps":[self.step]}

    def resolve(self, index=2):
        return resolve_goal("cal",index,self.ref,self.task,"original","f"*64)

    def test_only_original_latest_installed_observation_can_admit_pose(self):
        self.assertEqual(self.resolve()["translation_heading"],[-.4,.25,.18])
        for target, attrs in ((self.goal, {"mount_reference":None}), (self.goal,{"reference_frame":"map"}),
                              (self.goal,{"x_m":4}), (self.evidence,{"base_goal_available":False}),
                              (self.step,{"binding":"other"}),(self.evidence["source"],{"operation_id":"other/step/1"}),
                              (self.evidence["source"],{"planner_sha256":"e"*64})):
            with patch.dict(target,attrs), self.assertRaises(PhysicalError):
                self.resolve()
        self.task["steps"].append({"tool":"move_to_pose","physical_pin":{"binding":"p"},"status":"SUCCEEDED"})
        with self.assertRaisesRegex(PhysicalError,"intervened"):
            self.resolve(3)

    def test_world_anchor_and_forward_controller_include_final_heading(self):
        target=absolute_goal((1,1,math.pi/2),(-.4,.25,.18))
        self.assertAlmostEqual(target[0],.75)
        self.assertAlmostEqual(target[1],.60)
        x,y,yaw=1.,1.,math.pi/2
        stage="pivot"
        for _ in range(10000):
            v,w,stage=command_to_goal((x,y,yaw),target,stage)
            self.assertTrue(0 <= v <= .10 and abs(w) <= .12)
            if stage=="arrived":
                break
            x+=.02*v*math.cos(yaw)
            y+=.02*v*math.sin(yaw)
            yaw+=.02*w
        self.assertEqual(stage,"arrived")
        self.assertLess(math.dist((x,y),target[:2]),.031)
        self.assertLess(abs(math.remainder(yaw-target[2],2*math.pi)),.017)

    def test_tracking_requires_stamped_visibility_and_respects_steering_limits(self):
        row = dict(version=1, frame="cam", target="board", stamp_ns=2_000_000_000,
                   provider="1"*64, box=[.06,.2,.60,.8])
        observed = target_tracking.observation(json.dumps(row), "cam", "board")
        self.assertTrue(target_tracking.ready(observed,.1,2_030_000_000,1_000_000_000))
        self.assertFalse(target_tracking.ready(observed,.61,2_030_000_000,1_000_000_000))
        self.assertFalse(target_tracking.ready(observed,.1,2_720_000_000,1_000_000_000))
        self.assertFalse(target_tracking.ready(observed,.1,2_030_000_000,2_010_000_000))
        self.assertFalse(target_tracking.ready(dict(row,box=None),.1,2_030_000_000,1_000_000_000))
        v,w = target_tracking.tracked_command(.1,.115,"drive",row["box"],.12)
        self.assertLess(v,.1)
        self.assertEqual(w,.12)
        self.assertEqual(target_tracking.tracked_command(.1,.07,"drive",[.99,.2,1.,.8],.12),(0.,0.))

    def test_tracking_site_admits_only_forward_visibility_bound_proposals(self):
        with self.assertRaisesRegex(PhysicalError,"authorized target-tracking plan"):
            resolve_goal("cal",2,self.ref,self.task,"original","f"*64,"board")
        self.goal.update(x_m=.4,tracking={"target":"board","min_margin_px":24})
        self.assertEqual(resolve_goal("cal",2,self.ref,self.task,"original","f"*64,"board")["tracking_target"],"board")
        self.goal.update(x_m=-.4)
        with self.assertRaises(PhysicalError):
            resolve_goal("cal",2,self.ref,self.task,"original","f"*64,"board")
        self.assertTrue(resolve_goal("cal",2,self.ref,self.task,"original","f"*64,"board",True)["reverse"])

    def test_reverse_keeps_rover_facing_forward_and_checks_rear_clearance(self):
        v,w,phase = command_to_goal((0,0,0),(-.25,0,0),"pivot",reverse_mps=.07)
        self.assertEqual((v,w,phase),(-.07,0.,"drive"))
        scan=SimpleNamespace(angle_min=-math.pi,angle_increment=math.tau/360,range_min=.05,ranges=[math.inf]*360)
        self.assertTrue(scan_clear(scan,v,w))
        scan.ranges[0]=.35
        self.assertFalse(scan_clear(scan,v,w))
        self.assertTrue(scan_clear(scan,-v,w))

    def test_program_and_feedback_match_commissioned_chassis_contract(self):
        self.assertEqual(CONTROL["command_limits"],{"forward_mps":.1,"yaw_radps":.12})
        self.assertEqual((CONTROL["command"]["topic"],CONTROL["selected"]["topic"],CONTROL["safe"]["topic"]),
                         ("/cmd_vel","/mode_mux/selected_cmd_vel","/cmd_vel_safe"))
        self.assertEqual(CONTROL["odometry"],{"topic":"/lingao/odom","type":"nav_msgs/msg/Odometry",
                                            "parent_frame":"odom","child_frame":"base_foot"})
        self.assertEqual(CONTROL["stop"]["latch_value"],"stop")
        self.assertEqual(CONTROL["stationary_seconds"],1.5)
        self.assertEqual(command_to_goal((0,0,0),(1,0,0),"drive")[:2],(.10,0.))
        self.assertEqual(command_to_goal((0,0,0),(0,0,1),"final")[:2],(0.,.12))

    def test_standalone_field_package_loads_same_profile_and_refuses_unqualified_changes(self):
        from navigation.view_motion_site import CONTROL_FILE
        with tempfile.TemporaryDirectory() as temp:
            package=Path(temp)/"lingao_control"
            shutil.copytree(ROOT/"scripts"/"lingao_control",package,ignore=shutil.ignore_patterns("build","__pycache__"))
            # A field installation needs only this directory; no repository-relative fallback.
            profile=package/"lingao-chassis-control.json"
            self.assertEqual(hashlib.sha256(profile.read_bytes()).digest(),hashlib.sha256(CONTROL_FILE.read_bytes()).digest())
            probe=[sys.executable,"-B","-c","import brake; print(brake.CONTROL_SHA)"]
            def field():
                return subprocess.run(probe,cwd=package,capture_output=True,text=True)
            result=field()
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),hashlib.sha256(CONTROL_FILE.read_bytes()).hexdigest())
            for section,key,value in (("command_limits","forward_mps",.11),("command","topic","/cmd_vel_safe"),
                                      ("stop","latch_value","resume"),("odometry","child_frame","base_link")):
                changed=json.loads(CONTROL_FILE.read_bytes())
                changed[section][key]=value
                profile.write_text(json.dumps(changed))
                result=field()
                self.assertNotEqual(result.returncode,0)
                self.assertIn("differs from the commissioned field release",result.stderr)

    def test_scan_and_sim_context_fail_closed(self):
        with patch.dict("os.environ",{"ROS_DOMAIN_ID":"61","GZ_PARTITION":"edge_fixed_rover_197"}):
            with self.assertRaises(RuntimeError):
                guard_site()
        scan=SimpleNamespace(angle_min=-math.pi,angle_increment=math.tau/360,range_min=.05,ranges=[math.inf]*360)
        self.assertTrue(scan_clear(scan,.10,0))
        scan.ranges[180]=.37
        self.assertFalse(scan_clear(scan,.10,0))
        scan.ranges=[math.inf]*360
        scan.ranges[90]=.35
        self.assertFalse(scan_clear(scan,0,.35))
        scan.ranges[90]=math.nan
        self.assertFalse(scan_clear(scan,.10,0))
        self.assertFalse(scan_clear(None,.1,0))

    def test_source_anchor_requires_stationary_bracket_and_survives_no_clock_reset(self):
        poses=Poses()
        poses.put(1000,(0,0,0))
        poses.put(2000,(0,0,0))
        self.assertEqual(poses.anchor(1500),(0,0,0))
        poses.put(3000,(.04,0,0))
        with self.assertRaisesRegex(ValueError,"not_stationary"):
            poses.anchor(2500)
        poses.put(500,(0,0,0))
        with self.assertRaisesRegex(ValueError,"unavailable"):
            poses.anchor(1500)

    def test_wheel_gyro_fusion_integrates_independent_heading_with_acquisition_clock(self):
        poses=Poses()
        fusion=WheelGyroOdometry(poses.put,poses.rows.clear)
        # Wheel-yaw slip is unobservable from wheel speed; a distinct gyro records the chassis yaw.
        t=100_000_000
        for i in range(201):
            fusion.push("gyro",t,.06)
            if i % 2 == 0:
                fusion.push("wheel",t,0.)
            t+=10_000_000
        self.assertAlmostEqual(poses.rows[-1][2][2],.12)
        for i in range(101):
            fusion.push("gyro",t,0.)
            if i % 2 == 0:
                fusion.push("wheel",t,.10)
            t+=10_000_000
        x,y,yaw=poses.rows[-1][2]
        self.assertAlmostEqual(math.hypot(x,y),.1,delta=.001)
        self.assertAlmostEqual(math.atan2(y,x),.12,delta=.001)
        old=fusion.epoch
        fusion.push("gyro",t+500_000_000,0.)
        self.assertGreater(fusion.epoch,old)
        self.assertFalse(poses.rows)
