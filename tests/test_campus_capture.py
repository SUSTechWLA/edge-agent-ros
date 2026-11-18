"""SDD-025. Campus images carry their original pixels, timestamp and world revision."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
from types import SimpleNamespace as Obj
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scene/campus_large"))
from capture import VIEWS, capture, encode, identity, prepare, record


def sample():
    return Obj(width=2, height=1, step=8, data=b"\xff\x00\x00\x00\x80\x01xx", pixel_format_type=3,
               header=Obj(stamp=Obj(sec=2, nsec=123)))


class CaptureChecks(unittest.TestCase):
    def test_rgb8_pixels_without_padding_and_original_stamp(self):
        png, meta = encode(sample())
        offset = png.index(b"IDAT")
        size, = struct.unpack_from(">I", png, offset-4)
        self.assertEqual(zlib.decompress(png[offset+4:offset+4+size]), b"\0\xff\x00\x00\x00\x80\x01")
        self.assertEqual((meta["width"], meta["height"], meta["stamp_ns"]), (2,1,2_000_000_123))
        for key, value in (("pixel_format_type",8), ("step",1), ("width",5000), ("data",b"short")):
            msg = sample()
            setattr(msg, key, value)
            with self.assertRaises(ValueError):
                encode(msg)

    def test_subscription_always_removed(self):
        class Fixture:
            def __init__(self, frame):
                self.frame, self.removed = frame, []
            def subscribe(self, message_type, topic, callback):
                callback(self.frame)
                return True
            def unsubscribe(self, topic):
                self.removed.append(topic)
        ok = Fixture(sample())
        self.assertEqual(capture(ok, object, "/view", time.monotonic()+.2)[1]["width"], 2)
        self.assertEqual(ok.removed, ["/view"])
        invalid = sample()
        invalid.step = 0
        bad = Fixture(invalid)
        with self.assertRaisesRegex(TimeoutError, "RGB8"):
            capture(bad, object, "/view", time.monotonic()+.05)
        self.assertEqual(bad.removed, ["/view"])

    def test_corrupt_image_requeues_only_it_world_change_revokes_all(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            views = root / "overview"
            views.mkdir()
            (root / "world.sdf").write_bytes(b"world one")
            (root / "layout.json").write_bytes(b"{}")
            original = identity(root)
            png, meta = encode(sample())
            for name in VIEWS:
                (views / (name+".png")).write_bytes(png)
            record(views / "capture.json", {"world": original, "views": dict.fromkeys(VIEWS, meta)})
            self.assertTrue(prepare(root, original)["complete"])
            (views / "road.png").write_bytes(b"incomplete")
            same = prepare(root, original)
            self.assertEqual(set(same["views"]), set(VIEWS)-{"road"})
            self.assertFalse((views / "road.png").exists())
            (root / "layout.json").write_bytes(b'{"revision":2}')
            changed = prepare(root, identity(root))
            self.assertFalse(changed["complete"])
            self.assertEqual((changed["views"], list(views.glob("*.png"))), ({}, []))
            self.assertEqual(changed, json.loads((views/"capture.json").read_text()))


if __name__ == "__main__":
    unittest.main()
