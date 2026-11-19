"""Admission scope and causal capture contracts without any local ROS installation."""
from pathlib import Path
import base64
import json
from tempfile import TemporaryDirectory
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_recorder.ros_source import Sources, task_event, check_signal_scope, imu_metrics, scan_metrics


class SourceTest(unittest.TestCase):
    def test_gazebo_source_requires_deployment_and_opt_in(self):
        args=SimpleNamespace(sim=True,laser_scan="/scan",imu="/imu")
        for deployment in ("", "hardware", "admin"):
            with self.subTest(deployment=deployment), patch.dict("os.environ",{"EDGE_RECORDING_DEPLOYMENT":deployment}):
                with self.assertRaises(ValueError): check_signal_scope(args)
        with patch.dict("os.environ",{"EDGE_RECORDING_DEPLOYMENT":"gazebo"}):
            check_signal_scope(args)
            args.sim=False
            with self.assertRaises(ValueError): check_signal_scope(args)
        with patch.dict("os.environ",{"EDGE_RECORDING_DEPLOYMENT":"hardware"}):
            args.laser_scan=args.imu=None
            check_signal_scope(args)
            args.sim=True
            with self.assertRaises(ValueError): check_signal_scope(args)

    def test_laser_uses_only_finite_in_range_returns_and_retains_invalid_counts(self):
        scan=SimpleNamespace(range_min=.1,range_max=2,ranges=[float("inf"),float("nan"),float("-inf"),.2,4.,1.3])
        metrics,reason=scan_metrics(scan)
        self.assertEqual(reason,"")
        self.assertEqual(metrics,dict(beam_count=6,return_count=2,no_return_count=1,invalid_count=3,nearest_m=.2))
        for values,reason in (([float("inf")],"no_valid_ranges"),([.4]*65537,"scan_exceeds_beam_limit")):
            scan.ranges=values
            result,error=scan_metrics(scan)
            self.assertEqual(error,reason)
            self.assertNotIn("nearest_m",result)
        scan.ranges=[.4]
        scan.range_max=float("nan")
        self.assertEqual(scan_metrics(scan)[1],"invalid_range_limits")

    def test_imu_rejects_missing_measurement_or_nonfinite_gyro(self):
        m=SimpleNamespace(angular_velocity=SimpleNamespace(x=0.,y=-.3,z=.1),angular_velocity_covariance=[0.]*9)
        self.assertEqual(imu_metrics(m)[0]["angular_z_rad_s"],.1)
        m.angular_velocity_covariance[0]=-1.
        self.assertEqual(imu_metrics(m),({},"gyro_unavailable"))
        m.angular_velocity_covariance[0]=0.
        m.angular_velocity.z=float("inf")
        self.assertEqual(imu_metrics(m),({},"nonfinite_gyro"))

    def test_rate_and_invalid_header_evidence_use_separate_clocks(self):
        captured=[]
        s=Sources.__new__(Sources)
        s.args=SimpleNamespace(sensor_hz=4)
        s.signal_next={}
        s.sink=SimpleNamespace(emit=lambda kind,stamp,**fields:captured.append((stamp,fields)))
        m=SimpleNamespace(header=SimpleNamespace(frame_id="laser",stamp=SimpleNamespace(sec=3,nanosec=0)),
                          range_min=.1,range_max=2.,ranges=[.3])
        with patch("task_recorder.ros_source.time.monotonic",side_effect=[10,10.01,10.25,10.5]), \
             patch("task_recorder.ros_source.time.time_ns",return_value=9000):
            s.sensor("laser_scan",m,"/scan",scan_metrics)
            m.header.stamp.sec=1000
            s.sensor("laser_scan",m,"/scan",scan_metrics)
            m.header.stamp.sec=0
            s.sensor("laser_scan",m,"/scan",scan_metrics)
            m.header.stamp.sec=1
            m.header.frame_id=""
            s.sensor("laser_scan",m,"/scan",scan_metrics)
        self.assertEqual([stamp for stamp,_ in captured],[3000000000,9000,1000000000])
        invalid=captured[1][1]
        self.assertEqual((invalid["valid"],invalid["stamp_origin"],invalid["header_stamp_ns"],invalid["reason"]),
                         (False,"receipt",0,"invalid_header_stamp"))
        self.assertEqual(captured[2][1]["reason"],"invalid_frame_id")
        self.assertEqual(invalid["clock_domain"],"observer_unix_ns")

    def test_exact_decision_maps_include_consecutive_revisions_in_audit_mode(self):
        origin=SimpleNamespace(position=SimpleNamespace(x=0.,y=0.,z=0.),orientation=SimpleNamespace(x=0.,y=0.,z=0.,w=1.))
        stamp=SimpleNamespace(sec=5,nanosec=0)
        grid=SimpleNamespace(data=[0,-1],info=SimpleNamespace(width=2,height=1,origin=origin,resolution=.1),
                             header=SimpleNamespace(frame_id="map",stamp=stamp))
        for audit,expected in ((False,[5000000000]), (True,[5000000000,5250000000])):
            with self.subTest(audit=audit):
                s=Sources.__new__(Sources)
                stamps=[]
                s.last={}
                s.args=SimpleNamespace(all_map_revisions=audit,map="/map")
                s.sink=SimpleNamespace(emit=lambda kind,t,**kw:stamps.append(t))
                stamp.nanosec=0
                s.grid(grid)
                stamp.nanosec=250000000
                s.grid(grid)
                s.grid(grid)
                self.assertEqual(stamps,expected)

    def test_map_provenance_only_records_observed_identity_and_valid_planar_grid(self):
        s=Sources.__new__(Sources)
        s.args=SimpleNamespace(all_map_revisions=True,map="/my/grid")
        s.last={}
        s.sink=Mock()
        origin=SimpleNamespace(position=SimpleNamespace(x=0.,y=-2.,z=0.),orientation=SimpleNamespace(x=0.,y=0.,z=0.,w=1.))
        msg=SimpleNamespace(data=[-1,56],header=SimpleNamespace(frame_id="odom",stamp=SimpleNamespace(sec=5,nanosec=10)),
                            info=SimpleNamespace(width=2,height=1,origin=origin,resolution=.1,map_load_time=SimpleNamespace(sec=0,nanosec=0)))
        s.grid(msg,{"publisher_gid":bytes(range(16))})
        row=s.sink.emit.call_args.kwargs
        self.assertEqual((row["topic"],row["source"],row["map_load_time_ns"],row["publisher_id"]),
                         ("/my/grid","nav_msgs/msg/OccupancyGrid",0,bytes(range(16)).hex()))
        msg.header.stamp.sec=6
        s.grid(msg,{"source_timestamp":100})
        self.assertNotIn("publisher_id",s.sink.emit.call_args.kwargs)
        for obj,k,v in ((origin.position,"z",.2),(origin.orientation,"x",.1),(origin.orientation,"w",1.2),
                        (origin.position,"x",float("inf")),(msg.info,"resolution",float("nan"))):
            with self.subTest(k=k),patch.object(obj,k,v),self.assertRaisesRegex(ValueError,"geometry"):
                s.grid(msg)
        msg.header.stamp.sec=7
        msg.data[0]=-2
        with self.assertRaisesRegex(ValueError,"cell value"): s.grid(msg)

    def map_fixture(self):
        s=Sources.__new__(Sources)
        s.args=SimpleNamespace(all_map_revisions=False,map="/map")
        s.last={}
        s.sink=Mock()
        o=SimpleNamespace(position=SimpleNamespace(x=0.,y=0.,z=0.),orientation=SimpleNamespace(x=0.,y=0.,z=0.,w=1.))
        msg=SimpleNamespace(data=[0,-1],header=SimpleNamespace(frame_id="odom",stamp=SimpleNamespace(sec=3,nanosec=0)),
                            info=SimpleNamespace(width=2,height=1,origin=o,resolution=.1,
                                                 map_load_time=SimpleNamespace(sec=0,nanosec=0)))
        return s,msg

    def test_conflicting_same_stamp_retains_both_raw_windows_despite_rate_limit(self):
        s,msg=self.map_fixture()
        s.grid(msg)
        msg.header.stamp.nanosec=10
        msg.data[0]=1
        s.grid(msg)  # Normal interval would sample out this window.
        self.assertEqual(s.sink.emit.call_count,1)
        msg.data[0]=90
        s.grid(msg)
        s.grid(msg)  # Exact duplicates are still suppressed.
        rows=s.sink.emit.call_args_list
        self.assertEqual([r.args[1] for r in rows],[3000000000,3000000010,3000000010])
        media=[r.kwargs["media"] for r in rows]
        self.assertEqual(len({m["name"] for m in media}),3)
        self.assertEqual([json.loads(base64.b64decode(m["base64"]))["cells"][0] for m in media],[0,1,90])

    def test_new_publisher_topic_and_load_time_bypass_map_downsampling(self):
        s,msg=self.map_fixture()
        for gid in (b"a",b"a",b"b"):
            s.grid(msg,{"publisher_gid":gid})
        s.args.map="/replaced_map"
        s.grid(msg,{"publisher_gid":b"b"})
        msg.info.map_load_time.sec=2
        s.grid(msg,{"publisher_gid":b"b"})
        rows=[r.kwargs for r in s.sink.emit.call_args_list]
        self.assertEqual([r["publisher_id"] for r in rows],["61","62","62","62"])
        self.assertEqual([r["topic"] for r in rows],["/map","/map","/replaced_map","/replaced_map"])
        self.assertEqual([r["map_load_time_ns"] for r in rows],[0,0,0,2000000000])

    def test_map_stamp_rewind_and_all_unknown_reset_are_visible(self):
        s,msg=self.map_fixture()
        s.grid(msg)
        msg.header.stamp.sec=1
        s.grid(msg)
        msg.header.stamp.nanosec=5
        msg.data[0]=-1
        s.grid(msg)
        rows=s.sink.emit.call_args_list
        self.assertEqual([r.args[1] for r in rows],[3000000000,1000000000,1000000005])
        self.assertEqual([r.kwargs["known"] for r in rows],[1,1,0])

    def test_camera_scope_and_independent_media_paths_at_same_timestamp(self):
        args=SimpleNamespace(sim=True,laser_scan=None,imu=None,overview="/external",gz_camera=None,camera="/onboard")
        for deployment in ("hardware","", "admin"):
            with self.subTest(deployment=deployment),patch.dict("os.environ",{"EDGE_RECORDING_DEPLOYMENT":deployment}):
                with self.assertRaises(ValueError): check_signal_scope(args)
        with patch.dict("os.environ",{"EDGE_RECORDING_DEPLOYMENT":"gazebo"}):
            check_signal_scope(args)
            with patch.object(args,"sim",False),self.assertRaises(ValueError): check_signal_scope(args)
            with patch.object(args,"gz_camera",args.overview),self.assertRaises(ValueError): check_signal_scope(args)
        s=Sources.__new__(Sources)
        s.args=args
        s.last={}
        s.sink=Mock()
        camera=SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace(sec=1,nanosec=2,nsec=2)))
        with patch("perception.camera.frames.raw_frame",return_value=(b"on",0,0,0,2,1,0)), \
             patch("scene.campus_large.capture.encode",return_value=(b"off",{"width":2,"height":1})):
            for _ in range(2):
                s.rgb(camera)
                s.gz_rgb(camera,"overview",args.overview)
        rows=s.sink.emit.call_args_list
        self.assertEqual([r.args for r in rows],[("rgb",1000000002),("overview",1000000002)])
        self.assertEqual([r.kwargs["image_role"] for r in rows],["onboard","overview"])
        self.assertEqual([r.kwargs["media"]["name"].split("/")[0] for r in rows],["rgb","overview"])
        self.assertEqual([r.kwargs["topic"] for r in rows],["/onboard","/external"])

    def test_only_this_tasks_bus_events(self):
        self.assertTrue(task_event({"operation_id":"a/step/1"}, "a"))
        self.assertTrue(task_event({"trace_id":"a"}, "a"))
        self.assertFalse(task_event({"operation_id":"aa/step/1"}, "a"))
        self.assertFalse(task_event({"detail":"a"}, "a"))

    def test_partial_marker_and_scope(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"source.jsonl"
            rows=[]
            source=Sources.__new__(Sources)
            source.args=SimpleNamespace(sim=True,task_id="a",markers=path)
            source.clock=2_000_000_000
            source.sink=SimpleNamespace(emit=lambda k,t,**kw:rows.append((k,t,kw)))
            source.mark_offset=0
            path.write_bytes(b'{"task_id":"b","phase":"unrelated"}\n{"task_id":"a","phase":"mapped",')
            source.markers()
            self.assertEqual(rows,[])
            with path.open("ab") as stream:
                stream.write(b'"source_stamp_ns":1000000000,"file":"map.yaml"}\n')
            source.markers()
            source.markers()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0][1],1000000000)
            self.assertEqual(rows[0][2]["artifact"],"map.yaml")
            self.assertNotIn("file",rows[0][2])


if __name__=="__main__":
    unittest.main()
