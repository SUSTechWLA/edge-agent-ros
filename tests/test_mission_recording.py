"""SDD-042 recording contracts, exercised without Docker, ROS or a video encoder."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/system"))
from long_mission_world import finalize, make_world, recording_services, rows_at


class Recording(unittest.TestCase):
    def test_camera_and_private_compose_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            world = make_world(ROOT, out / "trial.sdf")
            with self.assertRaises(FileExistsError):
                make_world(ROOT, world)
            scene = ET.parse(world).getroot().find("world")
            camera = scene.find("./model[@name='commission_recording_camera']/link/sensor")
            self.assertEqual(camera.get("type"), "camera")
            self.assertEqual(camera.findtext("topic"), "/commission/overview")
            for name in ("roof","door_wall_south","workshop_south"):
                link = scene.find(f"./model[@name='{name}']/link")
                self.assertIsNotNone(link.find("collision"), name)
                self.assertNotEqual(link.findtext("visual/visibility_flags"), camera.findtext("camera/visibility_mask"))
            services = recording_services("candidate", 91, "test-recording-partition", world, out)
            for spec in services.values():
                self.assertEqual(spec["environment"]["ROS_DOMAIN_ID"], "91")
                self.assertEqual(spec["environment"]["GZ_PARTITION"], "test-recording-partition")
            recorder = services["recorder"]
            self.assertEqual((recorder["image"], recorder["pull_policy"]), ("candidate","never"))
            self.assertEqual([v["source"] for v in recorder["volumes"] if not v["read_only"]], [str(out)])

    def test_timeline_requires_a_post_terminal_frame_and_intact_rgb(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            frames = out / "media/mission"
            frames.mkdir(parents=True)
            rgb = b"recorded frame bytes"
            (frames / "frame").write_bytes(rgb)
            digest = hashlib.sha256(rgb).hexdigest()
            rows = [dict(kind=kind,stamp_ns=ns) for ns in (1,2) for kind in ("map","fused_pose","twist")]
            rows += [dict(kind="world_pose", stamp_ns=1, xy=[0,0]), dict(kind="world_pose", stamp_ns=2, xy=[3,4]),
                     dict(kind="mission", stamp_ns=1, phase="started"), dict(kind="mission", stamp_ns=2, phase="finished"),
                     dict(kind="rgb", stamp_ns=1, file="frame", sha256=digest)]
            timeline = frames / "timeline.jsonl"
            timeline.write_text("".join(json.dumps(r)+"\n" for r in rows), encoding="utf8")
            with self.assertRaises(TimeoutError):
                finalize(out, timeout=.02)
            with timeline.open("a", encoding="utf8") as stream:
                stream.write(json.dumps(dict(rows[-1],stamp_ns=1_300_000_002))+'\n{"kind":')
            self.assertEqual(len(rows_at(out)), len(rows)+1)
            self.assertEqual(finalize(out, timeout=1)["measured_world_path_m"], 5)
            (frames / "frame").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "invalid RGB artifact"):
                finalize(out, timeout=1)


if __name__ == "__main__":
    unittest.main()
