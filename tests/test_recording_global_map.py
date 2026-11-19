"""SDD-048. Incremental, identity-bound planar occupancy maps and native map-artifact IO."""
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

from exploration.map_artifact import save_map
from task_recorder.global_map import GlobalMap


ID = {"environment":"hardware","robot_id":"rover-3","map_id":"campus.north","epoch":"localization-7",
      "clock_domain":"ros_system_ns","calibration_id":"factory-1"}


def grid(cells, x=0., y=0., yaw=0., w=2, h=2, frame="map", resolution=1.):
    return {"frame":frame,"origin":[x,y,yaw],"resolution":resolution,"width":w,"height":h,"cells":cells}


def sha(directory):
    return hashlib.sha256((directory/"manifest.json").read_bytes()).hexdigest()


class GlobalMapTests(unittest.TestCase):
    def setUp(self):
        self.state = GlobalMap(ID)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def add(self, data, t=10):
        return self.state.update(data,stamp_ns=t,identity=ID)

    def test_unknown_can_never_erase_but_new_known_evidence_changes_a_previous_obstacle(self):
        first = self.add(grid([-1,100,-1,0]))
        second = self.add(grid([50,-1,0,-1]),20)
        third = self.add(grid([-1,0,-1,-1]),30)
        self.assertEqual(first["changes"],[[1,0,100],[1,1,0]])
        self.assertEqual(second["changes"],[[0,0,50],[0,1,0]])
        self.assertEqual(third["changes"],[[1,0,0]])
        self.assertEqual(self.state.snapshot()["cells"],[50,0,0,0])
        self.assertEqual(third["known_cells"],4)

    def test_shift_on_rotated_lattice_and_negative_origin_without_robot_pose(self):
        yaw = math.pi/2
        first = self.add(grid([0,-1,-1,70],4.,6.,yaw))
        move = self.add(grid([100,-1,-1,35],4.,5.,yaw+2*math.pi),12)
        self.assertEqual(first["origin"],[4.,6.,yaw])
        self.assertEqual(move["bounds"],[-1,0,2,2])
        self.assertEqual(move["changes"],[[-1,0,100],[0,1,35]])
        snapshot = self.state.snapshot()
        self.assertAlmostEqual(snapshot["origin"][0],4.)
        self.assertAlmostEqual(snapshot["origin"][1],5.)
        self.assertEqual(snapshot["cells"],[100,0,-1,-1,35,70])

    def test_retry_and_every_rejected_update_leave_original_revision_unchanged(self):
        initial = grid([1,2,3,4])
        self.add(initial)
        self.assertEqual(self.add(initial)["changes"],[])
        self.assertEqual(self.state.revision,1)
        alternatives = [
            (grid([1,2,3,9]),10,ID,"stamp_conflict"), (initial,9,ID,"clock_rewind"),
            (grid([1,2,3,4],frame="odom"),11,ID,"frame_changed"),
            (grid([1,2,3,4],yaw=.01),11,ID,"orientation_changed"),
            (grid([1,2,3,4],resolution=.9),11,ID,"resolution_changed"),
            (grid([1,2,3,4],x=.25),11,ID,"origin_off_lattice"),
            (grid([1,2,3,4],x=-4098),11,ID,"capacity"),
            (grid([1,2,3,4],x=800,y=800),11,ID,"capacity"),
            ({**initial,"origin_z":.1},11,ID,"invalid_grid"),
            ({**initial,"cells":[255,0,0,0]},11,ID,"invalid_grid"),
            (initial,True,ID,"stamp_invalid"),
        ]
        alternatives.extend((initial,11,{**ID,k:"other"},"identity_mismatch") for k in ID)
        for data, stamp, ident, code in alternatives:
            with self.subTest(code=code,identity=ident),self.assertRaisesRegex(ValueError,code):
                self.state.update(data,stamp_ns=stamp,identity=ident)
            self.assertEqual(self.state.snapshot(),initial)
            self.assertEqual((self.state.revision,self.state.last_stamp_ns),(1,10))

    def test_native_checkpoint_can_resume_offset_lattice_with_idempotent_last_replay(self):
        self.add(grid([0,100,-1,-1]))
        last = grid([40,3,5,-1], x=-1.)
        self.add(last,20)
        dest = self.root/"base"
        first = self.state.save(dest,operation_id="task-one")
        digest = sha(dest)
        restored = GlobalMap.load_base(dest,identity=ID,manifest_sha256="sha256:"+digest)
        self.assertEqual(restored.base_manifest_sha256,digest)
        self.assertEqual(restored.snapshot(),self.state.snapshot())
        self.assertEqual(restored.update(last,stamp_ns=20,identity=ID)["changes"],[])
        self.assertEqual(restored.save(dest,operation_id="task-one"), first)
        new = grid([-1,10,60,20],x=1.)
        self.assertEqual(restored.update(new,stamp_ns=30,identity=ID),self.add(new,30))
        self.assertEqual(restored.save(self.root/"next",operation_id="task-two")["metadata"]["base_manifest_sha256"],digest)
        with self.assertRaisesRegex(ValueError,"conflict"):
            restored.save(dest,operation_id="task-two")

    def test_legacy_mapped_artifact_needs_explicit_approval_and_pinned_provenance(self):
        data = grid([10,80,-1,0])
        dest = self.root/"map"
        save_map(dest,data,metadata={"robot_id":ID["robot_id"],"calibration_id":ID["calibration_id"],
                  "state":"MAPPED","reason":"survey_complete","operation_id":"mission-old","stamp_ns":12})
        digest = sha(dest)
        with self.assertRaisesRegex(ValueError,"legacy_identity_approval"):
            GlobalMap.load_base(dest,identity=ID,manifest_sha256=digest)
        with self.assertRaisesRegex(ValueError,"identity_mismatch"):
            GlobalMap.load_base(dest,identity={**ID,"robot_id":"other"},manifest_sha256=digest,allow_legacy=True)
        restored=GlobalMap.load_base(dest,identity=ID,manifest_sha256=digest,allow_legacy=True)
        self.assertEqual(restored.snapshot(),data)
        self.assertEqual(restored.save(self.root/"registered",operation_id="bind")["metadata"]["base_manifest_sha256"],digest)

    def test_unknown_external_directory_digest_tamper_size_and_false_summary_fail_closed(self):
        path = self.root/"saved"
        self.add(grid([-1,0,50,99]))
        self.state.save(path,operation_id="task")
        digest = sha(path)
        def load(pin=digest):
            return GlobalMap.load_base(path,identity=ID,manifest_sha256=pin)
        with self.assertRaisesRegex(ValueError,"manifest_digest"):
            load("f"*64)
        with self.assertRaisesRegex(ValueError,"identity_mismatch"):
            GlobalMap.load_base(path,identity={**ID,"epoch":"relocalized"},manifest_sha256=digest,allow_legacy=True)
        original = (path/"map.pgm").read_bytes()
        (path/"map.pgm").write_bytes(original+b"corrupt")
        with self.assertRaisesRegex(ValueError,"artifact_digest"):
            load()
        (path/"map.pgm").write_bytes(b"x"*300000)
        with self.assertRaisesRegex(ValueError,"artifact_size"):
            load()
        (path/"map.pgm").write_bytes(original)
        manifest = json.loads((path/"manifest.json").read_text())
        manifest["known_cells"]+=1
        (path/"manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,"manifest_geometry"):
            load(sha(path))
        manifest["sha256"]["../outside"]="0"*64
        (path/"manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,"artifact_set"):
            load(sha(path))

    def test_empty_map_and_standalone_scopes(self):
        with self.assertRaisesRegex(ValueError,"map.empty"):
            self.state.snapshot()
        for bad in [{}, {**ID,"epoch":""}, {**ID,"robot_id":False}]:
            with self.assertRaisesRegex(ValueError,"identity_invalid"):
                GlobalMap(bad)
        for domain in ("hardware","gazebo"):
            ident={**ID,"environment":domain}
            obj=GlobalMap(ident)
            obj.update(grid([-1]*4),identity=ident,stamp_ns=1)
            self.assertEqual(obj.snapshot()["cells"],[-1]*4)
            ident["environment"]="other"
            self.assertEqual(obj.identity["environment"],domain)



class MapHistoryTests(unittest.TestCase):
    def setUp(self):
        from task_recorder.global_map import MapHistory
        self.h = MapHistory("rec-17")

    def event(self,seq,stamp,**fields):
        return {"kind":"map","seq":seq,"stamp_ns":stamp,**fields}

    def test_legacy_all_unknown_splits_prior_map_and_map_frames_stay_reviewable(self):
        before=grid([100,-1,0,0])
        a=self.h.advance(self.event(1,30),before)
        b=self.h.advance(self.event(2,31),grid([-1]*4))
        c=self.h.advance(self.event(3,32),grid([0,-1,-1,-1]))
        self.assertEqual((a["scope"],a["new_cells"],a["changed_cells"]),("recording_local",3,0))
        self.assertEqual((b["segment"],b["reason"],b["known_cells"]),(2,"all_unknown_reset",0))
        self.assertEqual((c["segment"],c["new_cells"],c["observed_known"]),(2,1,1))
        self.assertEqual(self.h.snapshot(1),before)
        self.assertEqual(self.h.snapshot()["cells"],[0,-1,-1,-1])
        summaries=self.h.segments()
        summaries[0]["identity"]["epoch"]="mutated"
        self.assertNotEqual(self.h.segments()[0]["identity"]["epoch"],"mutated")

    def test_fixed_frame_with_matching_explicit_scope_and_publisher_keeps_base(self):
        qualifiers={"map_identity":ID,"fixed_frame":True,"publisher_id":"dds-gid-1","topic":"/map"}
        first=self.event(1,30,**qualifiers)
        self.h.advance(first,grid([10,90,-1,-1]))
        cleared=self.h.advance(self.event(2,31,**qualifiers),grid([-1]*4))
        changed=self.h.advance(self.event(3,32,**qualifiers),grid([-1,0,-1,100]))
        self.assertEqual((cleared["segment"],cleared["observed_known"],cleared["known_cells"]),(1,0,2))
        self.assertEqual((changed["new_cells"],changed["changed_cells"]),(1,1))
        replaced=self.h.advance(self.event(4,33,**{**qualifiers,"publisher_id":"dds-gid-2"}),grid([-1]*4))
        self.assertEqual((replaced["segment"],replaced["reason"],replaced["known_cells"]),(2,"source_changed",0))

    def test_geometry_rewind_reset_and_declared_domain_are_separate_segments(self):
        qualifiers={"map_identity":ID,"publisher_id":"g"}
        self.h.advance(self.event(1,40,**qualifiers),grid([0]*4))
        cases=[(2,41,{**qualifiers},grid([90]*4,x=.25),"origin_off_lattice"),
               (3,15,{**qualifiers},grid([2]*4),"clock_rewind"),
               (4,16,{**qualifiers,"map_reset":True},grid([3]*4),"explicit_reset"),
               (5,17,{"map_identity":{**ID,"clock_domain":"new-clock"},"publisher_id":"g"},grid([4]*4),"identity_changed")]
        for seq,stamp,f,data,why in cases:
            d=self.h.advance(self.event(seq,stamp,**f),data)
            self.assertEqual((d["segment"],d["reason"]),(seq,why))
        with self.assertRaisesRegex(ValueError,"sequence_rewind"):
            self.h.advance(self.event(3,51),grid([0]*4))
        self.assertEqual(self.h.snapshot(2)["cells"],[90]*4)
        self.assertEqual(len(self.h.segments()),5)

    def test_rejections_leave_capacity_bound_history_and_last_good_state_intact(self):
        self.h.MAX_SEGMENTS=2
        data=grid([0]*4)
        self.h.advance(self.event(1,5),data)
        self.h.advance(self.event(2,4),data)
        for event,code in [(self.event(3,3),"history_capacity"),(self.event(2,8),"sequence_conflict"),
                            (self.event(3,9,known=3),"event_count_mismatch"),
                            (self.event(3,9,frame="wrong"),"event_frame_mismatch"),
                            (self.event(3,9,map_identity=ID,clock_domain="wrong"),"clock_domain_mismatch")]:
            with self.assertRaisesRegex(ValueError,code):
                self.h.advance(event,data)
        self.assertEqual([r["last_stamp_ns"] for r in self.h.segments()],[5,4])
        self.assertEqual(self.h.snapshot(),data)
        self.h.MAX_SEGMENTS=99
        self.h.MAX_CLOSED_CELLS=4
        with self.assertRaisesRegex(ValueError,"history_capacity"):
            self.h.advance(self.event(3,3),data)

    def test_static_browsing_and_explicit_external_base_admission_are_distinct(self):
        from task_recorder.global_map import GlobalMap, MapHistory, read_map
        q={"map_identity":ID,"publisher_id":"original","topic":"/map","fixed_frame":True}
        self.h.advance(self.event(1,10,**q),grid([0,0,0,0]))
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"prior"
            self.h.current.save(path,operation_id="before")
            digest=sha(path)
            self.assertEqual(read_map(path,manifest_sha256=digest)["grid"],self.h.snapshot())
            prior=GlobalMap.load_base(path,identity=ID,manifest_sha256=digest)
            resumed=MapHistory("new",base=prior,base_publisher_id="original")
            with self.assertRaisesRegex(ValueError,"base_scope_mismatch"):
                resumed.advance(self.event(1,11,**{**q,"publisher_id":"replacement"}),grid([-1]*4))
            with self.assertRaisesRegex(ValueError,"base_scope_mismatch"):
                resumed.advance(self.event(1,11),grid([-1]*4))
            new=resumed.advance(self.event(1,11,**q),grid([1,-1,-1,-1]))
            self.assertEqual((new["reason"],new["changed_cells"]),("approved_base",1))
            self.assertEqual(prior.snapshot()["cells"],[0]*4)
            self.assertEqual(resumed.snapshot()["cells"],[1,0,0,0])

    def test_load_time_reset_forks_history_and_closed_segment_round_trips_unchanged(self):
        from task_recorder.global_map import read_map
        q={"map_identity":ID,"publisher_id":"original","topic":"/map","fixed_frame":True}
        first=self.event(1,10,map_load_time_ns=1,event_id="r1",**q)
        initial=grid([40,-1,-1,0])
        self.h.advance(first,initial)
        with tempfile.TemporaryDirectory() as folder:
            before=Path(folder)/"prior"
            manifest=self.h.save_segment(1,before,operation_id="snap")
            second=self.event(2,11,map_load_time_ns=9,event_id="r2",**q)
            reset=self.h.advance(second,grid([-1]*4))
            self.assertEqual((reset["reason"],reset["segment"],reset["known_cells"]),("source_changed",2,0))
            retry=self.h.advance(second,grid([-1]*4))
            self.assertEqual((retry["segment_started"],retry["revision"]),(False,1))
            with self.assertRaisesRegex(ValueError,"sequence_conflict"):
                self.h.advance({**second,"event_id":"different"},grid([-1]*4))
            closed=Path(folder)/"closed"
            self.assertEqual(self.h.save_segment(1,closed,operation_id="snap"),manifest)
            self.assertEqual(sha(closed),sha(before))
            self.assertEqual(read_map(closed,manifest_sha256=sha(closed))["grid"],initial)
            reopened=GlobalMap.load_base(closed,identity=ID,manifest_sha256=sha(closed))
            self.assertEqual(reopened.update(initial,stamp_ns=10,identity=ID)["changes"],[])

    def test_missing_provenance_never_retains_an_all_unknown_frame(self):
        from task_recorder.global_map import MapHistory
        for q in ({"map_identity":ID,"fixed_frame":True},{"fixed_frame":True,"publisher_id":"g"},
                  {"map_identity":ID,"publisher_id":"g"}):
            h=MapHistory("recording-a")
            h.advance(self.event(1,1,**q),grid([1]*4))
            delta=h.advance(self.event(2,2,**q),grid([-1]*4))
            self.assertEqual((delta["segment"],delta["known_cells"]),(2,0))


if __name__=="__main__":
    unittest.main()
