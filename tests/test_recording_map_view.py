"""Offline map export and saved-map reader contracts. No ROS, browser or Pillow required."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_recorder import map_view


def payload(path):
    return json.loads(path.read_text("utf8").split('<script type="application/json" id="data">',1)[1].split("</script>",1)[0])


class MapViews(unittest.TestCase):
    def build(self, folder, grids, request=None):
        rows=[]
        for i,(frame,ox,cells) in enumerate(grids,1):
            grid=dict(frame=frame,width=len(cells),height=1,resolution=1,origin=[ox,0,0],cells=cells)
            f=folder/f"grid-{i}.json"
            f.write_text(json.dumps(grid),"utf8")
            rows.append(dict(kind="map",event_id=f"event-{i}",frame=frame,stamp_ns=i*10**9,received_ns=i*10**9,
                             seq=i,_at=i*10**9,_path=f,sha256=hashlib.sha256(f.read_bytes()).hexdigest(),
                             clock_domain="sim",task_id="A"))
        record=SimpleNamespace(header=dict(recording_id="rec-A",task_id="A"),first=0,end=max(1,len(rows))*10**9,
                               all=rows,by={"map":rows})
        events=[dict(_at=0,kind="mission",title=request,seq=1,event_id="submitted")] if request else []
        view=SimpleNamespace(events=events,signals=[])
        return map_view.MapPlayback(record,{},"received_ns",view,captioner=lambda r,g:(r["title"],""))

    def test_recorded_delta_saved_directory_and_no_preloaded_future_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);output=root/"out";output.mkdir()
            view=self.build(root,[("odom",-1,[-1,0]),("odom",0,[80,0])])
            refs=view.export(output)
            model=payload(output/"map.html")
            first,last=model["revisions"]
            self.assertGreater(first["t"],0)
            self.assertEqual(first["changes"],[[1,0,0]])
            self.assertEqual((last["added"],last["updated"],last["known_cells"]),(1,1,2))
            self.assertEqual(model["cameras"],{})
            self.assertEqual(model["saved_maps"],refs)
            target=output/refs[0]["folder"]
            static=payload(target/"index.html")
            self.assertTrue(static["saved"])
            self.assertEqual(static["revisions"][0]["known_cells"],2)
            self.assertEqual(static["revisions"][0]["sha256"],refs[0]["manifest_sha256"])
            with self.assertRaisesRegex(ValueError,"digest"):
                map_view.saved_view(target,"0"*64,root/"bad.html")
            f=target/"occupancy.json";f.write_bytes(f.read_bytes()+b" ")
            with self.assertRaisesRegex(ValueError,"digest"):
                map_view.saved_view(target,refs[0]["manifest_sha256"],root/"bad.html")

    def test_unaligned_frames_remain_separate_and_each_saved_map_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);output=root/"out";output.mkdir()
            v=self.build(root,[("odom",0,[0,100]),("garage",0,[-1,100])])
            refs=v.export(output)
            self.assertEqual([s["segment"] for s in refs],[1,2])
            self.assertEqual([r["known_cells"] for r in v.model["revisions"]],[2,1])
            for ref in refs:
                p=output/ref["folder"]
                map_view.saved_view(p,ref["manifest_sha256"],root/"reopen.html")
                self.assertEqual(payload(root/"reopen.html")["revisions"][0]["frame"],"odom" if ref["segment"]==1 else "garage")

    def test_software_recording_exports_escaped_events_without_map_or_camera(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            v=self.build(root,[],request="</script><script>bad()</script>")
            self.assertEqual(v.export(root),[])
            model=payload(root/"map.html")
            self.assertEqual(model["events"][0]["title"],"</script><script>bad()</script>")
            self.assertFalse(model["revisions"] or model["cameras"])
            self.assertFalse((root/"maps").exists())

    def test_camera_roles_require_real_source_and_topic(self):
        role=map_view.display_image_role
        self.assertEqual(role({"kind":"overview"}),"unknown")
        self.assertEqual(role({"kind":"overview","image_role":"overview","source":"ROS sensor_msgs/msg/Image","topic":"/ceiling/camera"}),"overview")
        self.assertEqual(role({"kind":"rgb","image_role":"onboard","source":"ROS sensor_msgs/msg/Image","topic":"/head/color"}),"onboard")
        self.assertEqual(role({"kind":"rgb","source":"gz.msgs.Image","topic":"/commission/overview"}),"overview_sim")
        self.assertEqual(role({"kind":"rgb","image_role":"onboard","topic":"/head/color"}),"unknown")
        self.assertEqual(role({"kind":"rgb","source":"gz.msgs.Image","topic":"../../raw"}),"unknown")

    def test_map_output_preserves_existing_assets_and_publishes_only_complete_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/"out"
            out.mkdir()
            old=out/"task.mp4"
            old.write_bytes(b"old")
            with self.assertRaises(FileExistsError):
                with map_view.map_output(out):
                    self.fail("started rendering over an existing package")
            self.assertEqual(old.read_bytes(),b"old")
            fresh=Path(tmp)/"fresh"
            with self.assertRaises(RuntimeError):
                with map_view.map_output(fresh) as stage:
                    (stage/"task.mp4").write_bytes(b"partial")
                    raise RuntimeError("encoder failed")
            self.assertFalse(fresh.exists())
            fresh.mkdir()
            with map_view.map_output(fresh) as stage:
                (stage/"index.html").write_bytes(b"new")
                self.assertEqual(list(fresh.iterdir()),[])
            self.assertEqual((fresh/"index.html").read_bytes(),b"new")
            race=Path(tmp)/"concurrent"
            with self.assertRaises(FileExistsError):
                with map_view.map_output(race) as stage:
                    (stage/"index.html").write_bytes(b"new")
                    race.mkdir()
                    (race/"keep").write_bytes(b"other writer")
            self.assertEqual((race/"keep").read_bytes(),b"other writer")
            self.assertFalse((race/"index.html").exists())


if __name__=="__main__":
    unittest.main()
