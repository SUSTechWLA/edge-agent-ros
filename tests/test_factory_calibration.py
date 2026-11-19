"""SDD-038. Synthetic, fixed-jig inputs; no production driver or physical survey involved."""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def fixture(robot_id="test-rover", environment="simulation"):
    sources={key:{"serial":"fixture-"+key, "topic":"/test/"+key, "frame":key+"_link"}
             for key in ("lidar","rgbd","imu")}
    extrinsics={key:{"robot_id":robot_id,"serial":src["serial"],"parent_frame":"base_link",
                     "child_frame":src["frame"],"translation_m":[.1,.02,.25],"rotation_xyzw":[0,0,0,1],
                     "method":"surveyed_mount","reference":"synthetic-jig-1"} for key,src in sources.items()}
    k=[200.,0.,159.5,0.,200.,119.5,0.,0.,1.]
    profile={"schema_version":1,"robot_id":robot_id,"environment":environment,"base_frame":"base_link",
             "sensors":sources,"extrinsics":extrinsics,"camera_intrinsics":{
                 "serial":"fixture-rgbd","frame":"rgbd_link","reference":"synthetic-calibration",
                 "width":320,"height":240,"k":k}}
    capture={"schema_version":1,"robot_id":robot_id,"environment":environment,"clock_domain":"fixture_ros",
             "stationary_reference":"synthetic-jig-braked","sensors":copy.deepcopy(sources)}
    for key,src in capture["sensors"].items():
        src["samples"]=[]
        for index in range(7):
            stamp=1_000_000_000+index*200_000_000
            values={"lidar":{"point_count":600,"finite_xyz_count":570},
                    "imu":{"accel_mps2":[0.,0.,9.81],"gyro_rad_s":[.002,-.001,0.]},
                    "rgbd":{"width":320,"height":240,"depth_valid_count":72000,"color_stamp_ns":stamp,
                            "info_stamp_ns":stamp,"k":list(k)}}
            src["samples"].append({"stamp_ns":stamp,"received_ns":stamp+1_000_000,**values[key]})
    return profile,capture


class FactoryTests(unittest.TestCase):
    def setUp(self):
        from calibration.factory import status, verify
        self.verify,self.status=verify,status
        td=tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.state=Path(td.name)
        self.profile,self.capture=fixture()

    def run_check(self, op="row1"):
        return self.verify(self.profile,self.capture,self.state,op)

    def current(self,environment="simulation"):
        return self.status(self.state,"test-rover",environment)

    def test_static_observables_and_scope(self):
        self.assertEqual(self.current()["status"],"NEEDS_CALIBRATION")
        self.assertFalse((self.state/"module-events.sqlite").exists())
        result=self.run_check()
        self.assertEqual(result["status"],"VERIFIED",result)
        self.assertEqual(len(result["revision"]),64)
        self.assertEqual(result["calibration"]["gyro_bias_rad_s"],[.002,-.001,0])
        self.assertAlmostEqual(result["statistics"]["imu"]["accel_mean_mps2"][2],9.81)
        self.assertEqual(result["statistics"]["lidar"]["samples"],7)
        self.assertEqual(result["statistics"]["timing"]["worst_nearest_skew_ns"],0)
        self.assertEqual(result,self.current())
        self.assertFalse(result["motion_authorized"])
        self.assertEqual(self.current("hardware")["status"],"NEEDS_CALIBRATION")
        self.profile["environment"]=self.capture["environment"]="hardware"
        self.assertNotEqual(self.run_check("hardware-fixture")["revision"],result["revision"])
        self.assertEqual(self.current(None)["issues"][0]["code"],"environment.ambiguous")

    def test_damaged_measurements(self):
        p,c=self.profile,self.capture
        def setat(path,value):
            def change(profile,capture):
                data=profile if path[0]=="p" else capture
                for k in path[1:-1]:
                    data=data[k]
                data[path[-1]]=value
            return change
        cases=[(("p","extrinsics","imu"),None,"extrinsic.unmeasured"),
               (("p","extrinsics","lidar","rotation_xyzw"),[0,0,0,0],"extrinsic.transform"),
               (("p","extrinsics","rgbd","parent_frame"),"map","extrinsic.identity"),
               (("p","extrinsics","lidar","method"),["invalid"],"extrinsic.unmeasured"),
               (("p","camera_intrinsics","reference"),"","camera.intrinsics_missing"),
               (("c","stationary_reference"),"","capture.fixture"),
               (("c","robot_id"),"another","capture.identity"),
               (("c","sensors","rgbd","serial"),"swapped","source.mismatch"),
               (("c","sensors","imu","samples",2,"accel_mps2"),[0,0,1],"imu.stationarity"),
               (("c","sensors","imu","samples",2,"gyro_rad_s"),[0,.2,0],"imu.stationarity"),
               (("c","sensors","imu","samples",2,"gyro_rad_s"),[float("nan"),0,0],"imu.invalid"),
               (("c","sensors","rgbd","samples",2,"depth_valid_count"),0,"camera.depth_invalid"),
               (("c","sensors","rgbd","samples",2,"k"),[150,0,159,0,150,119,0,0,1],"camera.intrinsics_mismatch"),
               (("c","sensors","rgbd","samples",2,"info_stamp_ns"),1_000_000,"camera.sync"),
               (("c","sensors","lidar","samples",2,"finite_xyz_count"),3,"lidar.invalid_cloud"),
               (("c","sensors","lidar","samples",2,"stamp_ns"),1_200_000_000,"capture.repeated_stamp"),
               (("c","sensors","lidar","samples",2,"received_ns"),8_000_000_000,"capture.delay"),
               (("c","sensors","imu","samples"),[],"capture.count")]
        for index,(path,value,code) in enumerate(cases):
            with self.subTest(path=path,code=code):
                self.profile,self.capture=copy.deepcopy(p),copy.deepcopy(c)
                setat(path,value)(self.profile,self.capture)
                result=self.run_check(f"bad-{index}")
                self.assertEqual(result["status"],"NEEDS_CALIBRATION")
                self.assertIn(code,{i["code"] for i in result["issues"]})
                self.assertIsNone(result["revision"])
                json.dumps(result,allow_nan=False)

    def test_source_clocks_must_overlap(self):
        for r in self.capture["sensors"]["imu"]["samples"]:
            r["stamp_ns"] += 60_000_000_000
            r["received_ns"] += 60_000_000_000
        self.assertEqual(self.run_check()["issues"][0]["code"],"capture.overlap")

    def test_independent_target_validation(self):
        pose=self.profile["extrinsics"]["lidar"]
        pose.update(method="target_fit",input_sha256="6"*64,fit_rms_m=.01,validation_count=4,
                    validation_rms_m=.014,validation_max_m=.018)
        self.assertEqual(self.run_check()["status"],"VERIFIED")
        pose["validation_max_m"]=.12
        self.assertEqual(self.run_check("bad")["issues"][0]["code"],"extrinsic.validation")

    def test_refusal_preserves_revision_but_blocks_latest_admission(self):
        good=self.run_check()
        self.profile["extrinsics"]["imu"]["reference"]=""
        failed=self.run_check("failed")
        self.assertEqual(failed["last_verified_revision"],good["revision"])
        self.assertIsNone(self.current()["revision"])
        self.profile,self.capture=fixture()
        self.assertEqual(self.run_check(),good)
        self.assertEqual(self.current(),failed)
        self.assertEqual(self.run_check("stale")["issues"][0]["code"],"capture.superseded")
        with closing(sqlite3.connect(self.state/"module-events.sqlite")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM factory_revision").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM factory_input").fetchone()[0],3)
            events=[json.loads(row[0]) for row in db.execute("SELECT event FROM module_bus_events ORDER BY seq")]
            self.assertEqual(events[1]["parent_event_id"],events[0]["event_id"])
        self.profile["extrinsics"]["imu"]["reference"]="reissued-document"
        self.assertEqual(self.run_check("stale-updated-paperwork")["issues"][0]["code"],"capture.superseded")

    def test_missing_capture_file_suspends_known_vehicle(self):
        verified=self.run_check()
        path=self.state/"profile.json"
        path.write_text(json.dumps(self.profile),encoding="utf-8")
        result=subprocess.run([sys.executable,"-m","calibration.factory","verify","--profile",str(path),
                               "--capture",str(self.state/"absent.json"),"--state",str(self.state),
                               "--operation","sensor-file-missing"],cwd=Path(__file__).resolve().parents[1],
                              capture_output=True,timeout=15)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertEqual(self.current(),json.loads(result.stdout))
        self.assertEqual(self.current()["last_verified_revision"],verified["revision"])
        self.assertIsNone(self.current()["revision"])

    def test_parallel_replay_and_conflict_are_durable(self):
        with ThreadPoolExecutor(4) as pool:
            results=list(pool.map(lambda _:self.run_check(), range(8)))
        self.assertTrue(all(r==results[0] for r in results))
        self.capture["clock_domain"]="different-domain"
        first=self.run_check()
        self.assertEqual(first["issues"][0]["code"],"operation.conflict")
        self.assertEqual(self.run_check(),first)
        self.assertEqual(self.current(),results[0])
        with closing(sqlite3.connect(self.state/"module-events.sqlite")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM factory_attempt").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM module_bus_events").fetchone()[0],2)

    def test_abort_rolls_back_inputs_revision_event_and_result(self):
        original=self.run_check()
        with closing(sqlite3.connect(self.state/"module-events.sqlite")) as db:
            db.execute("CREATE TRIGGER fail BEFORE INSERT ON factory_attempt BEGIN SELECT RAISE(ABORT,'disk fixture'); END")
        self.capture["stationary_reference"]="new fixture run"
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_check("power-loss")
        self.assertEqual(original,self.current())
        with closing(sqlite3.connect(self.state/"module-events.sqlite")) as db:
            for table,count in (("factory_revision",1),("factory_input",2),("factory_attempt",1),("module_bus_events",1)):
                self.assertEqual(db.execute("SELECT count(*) FROM "+table).fetchone()[0],count)


if __name__=="__main__":
    unittest.main()
