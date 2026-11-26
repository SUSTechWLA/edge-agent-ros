import math
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from exploration.corridor_review import predicted_conflict, review_corridor
from exploration.survey_freshness import observation_issue
from exploration.survey_session import CORRIDOR, Survey


def sample(index, *, limit=None, tracks=None, velocity=0.):
    stamp = 1_000_000_000+index*125_000_000
    ranges = [2.8]*360
    if limit is not None:
        for i in range(161,201):
            ranges[i] = limit
    return dict(scan=dict(stamp_ns=stamp,frame="laser_link",ranges=ranges,range_min=.02,
                          range_max=2.8,angle_min=-math.pi,angle_max=math.pi-math.tau/360,
                          angle_increment=math.tau/360),
                pose=dict(x=0.,y=0.,yaw=0.,speed=velocity,yaw_rate=0.,stamp_ns=stamp,frame="odom"),
                dynamic=dict(odom_stamp_ns=stamp,stamp_ns=stamp,scan_seq=index+1,
                             frame_id="odom",valid=True,tracks=tracks or [],scan_ref=f"test-boot-{index}"))


def valid_native(mode="observed_corridor", count=8):
    return dict(clearance_m=.62,dynamic_clear_samples=count,footprint_ref="office.fixture.gazebo.diff_drive",
                dynamic_recheck=dict(mode=mode,corridor_clear=True,stationary=True,tracks_clear=True,
                                     scan_stamp_ns=5_000_000_001,odom_stamp_ns=5_000_000_000,dwell_ns=875_000_000))


class ScanReviewTest(unittest.TestCase):
    def evaluate(self,samples,after=0):
        return review_corridor(dict(goal=[.55,0],samples=samples,after_ns=after,
                                    clock_ns=samples[-1]["scan"]["stamp_ns"]))

    def test_recovers_only_on_continuous_fresh_scans_and_distinct_odom(self):
        samples = [sample(i) for i in range(8)]
        self.assertEqual(self.evaluate(samples)["result"],"clear")
        self.assertEqual(self.evaluate(samples,after=samples[4]["scan"]["stamp_ns"])["result"],"waiting")
        del samples[4]
        self.assertEqual(self.evaluate(samples)["result"],"waiting")
        self.assertEqual(self.evaluate([sample(1)]*8)["result"],"waiting")
        self.assertEqual(self.evaluate([sample(i,velocity=.08) for i in range(8)])["result"],"waiting")

    def test_live_occupancy_and_next_crosser_hold_the_same_margin(self):
        blocked = self.evaluate([sample(i) for i in range(7)]+[sample(7,limit=1.1)])
        self.assertEqual(blocked["reason"],"insufficient_clearance")
        self.assertEqual(blocked["required_clearance_m"],.55)
        arriving = dict(x_m=.7,y_m=1.10,visible_radius_m=.16,velocity_valid=True,vx_mps=0.,vy_mps=-.55)
        predicted = self.evaluate([sample(i) for i in range(7)]+[sample(7,tracks=[arriving])])
        self.assertEqual(predicted["reason"],"predicted_track_conflict")
        self.assertEqual(predicted["clear_samples"],0)

    def test_predicted_disk_uses_euclidean_distance_at_the_corridor_corner(self):
        origin = [.00688, -.00000643]
        goal = [.55, -.05]
        length = math.dist(origin, goal)
        ahead = [(b - a) / length for a, b in zip(origin, goal)]
        left = [-ahead[1], ahead[0]]
        # Measured campus doorway cluster after both pedestrians had left.
        wall = dict(x_m=1.446728, y_m=-1.532522, visible_radius_m=.678793,
                    velocity_valid=True, vx_mps=0., vy_mps=0.)
        self.assertFalse(predicted_conflict(wall, origin, ahead, left, length))
        self.assertTrue(predicted_conflict({**wall, "vx_mps": -.5, "vy_mps": .7},
                                           origin, ahead, left, length))
        on_boundary = dict(x_m=.3, y_m=.28 + .55 + .10, visible_radius_m=.10)
        self.assertTrue(predicted_conflict(on_boundary, [0,0], [1,0], [0,1], .55))

    def test_alternative_uses_full_previous_horizon_and_preserves_the_original_conflict_band(self):
        samples = [sample(i) for i in range(8)]
        proof = dict(samples=samples, after_ns=0, clock_ns=samples[-1]["scan"]["stamp_ns"], goal=[.4,.36])
        original = [.65,0.]
        result = review_corridor({**proof,"previous_goal":original})
        self.assertEqual(result["result"],"clear")
        self.assertEqual(result["previous_goal"],original)
        self.assertTrue(result["original_tracks_clear"])
        for s in samples:
            s["dynamic"]["tracks"] = [dict(x_m=.60,y_m=-.84,visible_radius_m=.12,velocity_valid=True,
                                           vx_mps=0.,vy_mps=.03)]
        self.assertEqual(review_corridor(proof)["result"],"clear")
        self.assertEqual(review_corridor({**proof,"previous_goal":original})["reason"],"original_track_conflict")
        for s in samples:
            s["dynamic"]["tracks"] = []
            s["scan"]["ranges"] = [1.10]*360
        proof["goal"] = [.10,0.]
        self.assertEqual(review_corridor(proof)["result"],"clear")
        self.assertEqual(review_corridor({**proof,"previous_goal":original})["reason"],"insufficient_clearance")
        self.assertEqual(review_corridor({**proof,"previous_goal":original,"samples":[samples[-1]]})["result"],"waiting")

    def test_candidate_batch_returns_only_the_chord_proven_on_all_the_latest_samples(self):
        samples = [sample(i) for i in range(8)]
        for s in samples:
            s["scan"]["ranges"][178:183] = [1.10]*5
        options = [[.65,0.],[.45,.46],[.3,.54]]
        probe = dict(samples=samples,clock_ns=samples[-1]["scan"]["stamp_ns"],after_ns=0,
                     previous_goal=[.40,-.35],goal=options[0],candidate_goals=options)
        proof = review_corridor(probe)
        self.assertEqual(proof["result"],"clear")
        self.assertEqual(proof["goal"],options[2])
        self.assertEqual(proof["clear_samples"],len(samples))
        for s in samples:
            s["scan"]["ranges"] = [1.05]*360
        self.assertEqual(review_corridor(probe)["result"],"waiting")

    def test_gateway_receipt_requires_stronger_proof_after_lost_identity(self):
        self.assertEqual(review_corridor({"native":valid_native()})["result"],"clear")
        self.assertEqual(review_corridor({"native":valid_native(count=3)})["result"],"waiting")
        too_near = {**valid_native(),"clearance_m":.549}
        self.assertEqual(review_corridor({"native":too_near})["result"],"waiting")

    def test_static_face_requires_support_and_includes_its_uncertainty(self):
        track = dict(x_m=.45,y_m=1.2,visible_radius_m=math.hypot(.65,.08)+.08,
                     visible_half_extent_x_m=.65,visible_half_extent_y_m=.08,velocity_valid=True,
                     vx_mps=0.,vy_mps=0.,early_dx_m=0.,early_dy_m=0.,late_dx_m=0.,late_dy_m=0.,
                     half_extent_rate_x_mps=0.,half_extent_rate_y_mps=0.,span_s=1.,samples=9,support_rays=12)
        def conflict(**changes):
            return predicted_conflict({**track,**changes}, [0.,0.], [1.,0.], [0.,1.], .6)
        self.assertFalse(conflict())
        self.assertTrue(conflict(samples=4))
        self.assertTrue(conflict(late_dy_m=.03))
        self.assertTrue(conflict(y_m=.98))
        self.assertTrue(conflict(y_m=1.02,vy_mps=-.03))
        self.assertTrue(conflict(visible_half_extent_x_m=None))

    def test_registered_static_surface_keeps_the_current_clearance_check(self):
        face = dict(x_m=.42,y_m=1.2,visible_radius_m=.12,velocity_valid=True,vx_mps=0.,vy_mps=-.5)
        def conflict(**changes):
            return predicted_conflict({**face,**changes}, [0.,0.], [1.,0.], [0.,1.], .6)
        self.assertTrue(conflict())
        self.assertFalse(conflict(matches_stationary_surface=True))
        self.assertTrue(conflict(matches_stationary_surface=True,y_m=.9))


class RecoverySessionTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.rover = SimpleNamespace(motion_events=[],action_results={},commands=[])
        self.rover.stop = lambda op: self.rover.commands.append(("stop",op))
        self.rover.status = lambda op: SimpleNamespace(found=True,state="RUNNING")
        self.survey = Survey(self.rover,{"clearance_timeout_s":20},Path(work.name)/"state.json",
                             Path(work.name),"")
        self.survey.service = self.review_service
        self.row = self.survey.create("task","task/step/1","去会议室",{},"navigate","会议室")
        self.row.update(map_ref={"id":"office","digest":"stable"},active=dict(
            id="task/leg/1",mode="navigate",goal=[.5,0],started=time.time()))

    def review_service(self, row, name, body):
        self.assertEqual(name,CORRIDOR)
        return review_corridor(body)

    @staticmethod
    def gateway(code, n, plan=None):
        return dict(module="edge.gateway",code=code,operation_id="task/leg/1/waypoint/1",
                    event_id=f"gateway-candidate-boot-{n}",data=dict(plan=plan or {}))

    def test_resumes_same_leg_and_map_once_with_verified_clear_receipt(self):
        self.rover.motion_events = [self.gateway("navigation.dynamic_yield",1)]
        self.survey.reconcile(self.row)
        self.assertEqual(self.row["phase"],"RECHECKING_CLEARANCE")
        self.rover.motion_events += [
            self.gateway("navigation.dynamic_recheck",2,valid_native(count=2)),
            self.gateway("navigation.dynamic_clear",3,valid_native()),
        ]
        self.survey.reconcile(self.row)
        self.survey.reconcile(self.row)
        events = [e for e in self.row["events"] if e["kind"]=="dynamic_resumed"]
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]["data"]["operation_id"],"task/leg/1")
        self.assertEqual(events[0]["data"]["task_operation_id"],"task/step/1")
        self.assertEqual(events[0]["data"]["map_ref"],{"id":"office","digest":"stable"})
        self.assertEqual(self.rover.commands,[])

    def test_stale_observation_uses_same_cancel_and_requires_gateway_stop_evidence(self):
        sample = (4_000_000_000, 9.0, {"frame":"odom"})
        clock = (sample[0], 9.0)
        self.assertIsNone(observation_issue(10.,False,clock,sample,sample,sample))
        for offset in (-1_200_000_000,250_000_000):
            self.assertIsNone(observation_issue(10.,False,clock,sample,sample,(sample[0]+offset,9.,{})))
        for offset in (-1_200_000_001,250_000_001):
            value = observation_issue(10.,False,clock,sample,sample,(sample[0]+offset,9.,{}))
            self.assertEqual((value["sensor"],value["reason"]),("scan","capture_time_outside_window"))
        stale = observation_issue(10.,False,clock,sample,(sample[0],6.99,sample[2]),sample)
        self.assertEqual((stale["sensor"],stale["reason"]),("grid","arrival_stale"))
        self.rover.observation_issue = stale
        self.rover.snapshot = lambda: None
        self.survey.advance(self.row)
        self.survey.advance(self.row)
        self.assertEqual(self.rover.commands,[("stop","task/leg/1")])
        event, = [e for e in self.row["events"] if e["kind"]=="stop_requested"]
        self.assertEqual(event["data"]["observation_issue"],stale)
        self.rover.status = lambda op: SimpleNamespace(found=True,state="RECOVERY_REQUIRED")
        self.survey.advance(self.row)
        self.assertEqual(self.row["status"],"RECOVERY_REQUIRED")

    def test_alternative_is_only_probed_after_native_static_stop_and_needs_new_paired_samples(self):
        active = self.row["active"]
        active.update(continuous=True,tracking_target=[.60,0.])
        self.survey.config["max_step"] = .65
        reply = SimpleNamespace(state="FAILED")
        snap = dict(scan_stamp_ns=123,pose=dict(x=0.,y=0.),grid={"resolution":.1})
        self.assertFalse(self.survey.enter_stationary_review(self.row,active,reply,snap))
        stop = dict(quiet_samples=4,quiet_acquisition_ms=280,quiet_arrival_ms=280,last_linear_mps=0.,last_angular_rad_s=0.)
        receipt = self.gateway("navigation.no_safe_path",1)
        receipt["data"].update(odom_stamp_ns=111,stop_evidence=stop)
        self.rover.action_results[active["id"]+"/waypoint/1"] = receipt
        self.rover.review_window = lambda:([],124)
        self.survey.navigation_plan = lambda row,snap:dict(state="WAYPOINT",goal=[.50,.25])
        probes = []
        def review(row,name,body):
            probes.append(body)
            return dict(result="waiting",reason="insufficient_clearance",clear_samples=0)
        self.survey.service = review
        active["dynamic_review"] = {"deadline": time.time()+12}
        self.assertTrue(self.survey.enter_stationary_review(self.row,active,reply,snap))
        self.survey.stationary_review(self.row,snap)
        self.assertNotIn("previous_goal",self.row["recovery"])
        active.pop("dynamic_review")
        self.assertTrue(self.survey.enter_stationary_review(self.row,active,reply,snap))
        self.survey.stationary_review(self.row,snap)
        recovery = self.row["recovery"]
        self.assertEqual(recovery["previous_goal"],[.60,0.])
        self.assertEqual(recovery["goal"],[.50,.25])
        self.assertEqual(recovery["after_ns"],snap["scan_stamp_ns"])
        recovery["last_check"] = 0
        self.survey.stationary_review(self.row,snap)
        self.assertEqual(probes[-1]["previous_goal"],[.60,0.])
        self.assertEqual(self.rover.commands,[])

    def test_timeout_requests_cancel_on_original_id_and_needs_stop_receipt(self):
        self.rover.motion_events = [self.gateway("navigation.dynamic_yield",1)]
        self.survey.reconcile(self.row)
        self.row["active"]["dynamic_review"]["deadline"] = 0
        self.survey.reconcile(self.row)
        self.assertEqual(self.rover.commands,[("stop","task/leg/1")])
        self.rover.status = lambda op: SimpleNamespace(found=True,state="CANCELED")
        self.survey.reconcile(self.row)
        self.assertEqual(self.row["status"],"RUNNING")
        receipt = self.gateway("navigation.cancel_requested",2)
        receipt["data"] = {"stop_evidence":{"quiet_samples":8,"quiet_acquisition_ms":300,"quiet_arrival_ms":300,
                                            "last_linear_mps":0.,"last_angular_rad_s":0.}}
        self.rover.action_results["task/leg/1/waypoint/1"] = receipt
        self.survey.reconcile(self.row)
        self.assertEqual(self.row["status"],"BLOCKED")
        self.assertEqual(self.rover.commands,[("stop","task/leg/1")])

    def test_sensor_failure_during_hold_keeps_the_original_review_deadline(self):
        self.rover.motion_events = [self.gateway("navigation.dynamic_yield", 1)]
        self.survey.reconcile(self.row)
        deadline = self.row["active"]["dynamic_review"]["deadline"]
        stop = self.gateway("navigation.scan_stale", 2)
        stop["data"] = dict(odom_stamp_ns=123, stop_evidence=dict(quiet_samples=8,
            quiet_acquisition_ms=320, quiet_arrival_ms=350, last_linear_mps=0., last_angular_rad_s=0.))
        self.rover.action_results["task/leg/1/waypoint/1"] = stop
        self.rover.status = lambda op: SimpleNamespace(found=True, state="FAILED")
        self.rover.snapshot = lambda: None
        self.survey.reconcile(self.row)
        self.assertIsNone(self.row["active"])
        self.assertEqual(self.row["recovery"]["operation_id"], "task/leg/1")
        self.assertEqual(self.row["recovery"]["deadline"], deadline)
        self.assertEqual(self.row["recovery"]["after_ns"], 123)
        self.assertEqual(self.row["phase"], "RECHECKING_CLEARANCE")
        self.assertFalse(self.rover.commands)


if __name__ == "__main__":
    unittest.main()
