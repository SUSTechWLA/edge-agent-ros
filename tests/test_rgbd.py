"""SDD-024 frame/value contracts with small independently encoded RGB-D examples."""
from pathlib import Path
import json
import math
import struct
import sys
from types import SimpleNamespace as NS
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "perception" / "rgbd"))
from pairs import configuration, KINDS, Synchronizer, validate

SECOND = 10**9


def header(nsec, frame="camera_color_optical_frame"):
    sec, ns = divmod(nsec, SECOND)
    return NS(stamp=NS(sec=sec, nanosec=ns), frame_id=frame)


def images(t=SECOND, depth_enc="16UC1", big=0, values=None):
    color = NS(width=3, height=2, header=header(t), encoding="bgr8", is_bigendian=0, step=11,
               data=bytes([3, 2, 1, 6, 5, 4, 9, 8, 7, 89, 89, 12, 11, 10, 15, 14, 13, 18, 17, 16, 89, 89]))
    integer = depth_enc == "16UC1"
    fmt = (">" if big else "<") + ("HHH" if integer else "fff")
    if values is None:
        values = (0, 1005, 2500, 3300, 7001, 6100) if integer else (float("nan"), 1.005, 2.5, 3.3, 7.001, 6.1)
    data = struct.pack(fmt, *values[:3]) + b"\x66\x77" + struct.pack(fmt, *values[3:]) + b"\x66\x77"
    depth = NS(width=3, height=2, header=header(t), encoding=depth_enc, is_bigendian=big, step=len(data)//2, data=data)
    info = NS(width=3, height=2, header=header(t), k=[2., 0., 1., 0., 2., .5, 0., 0., 1.])
    return [color, depth, info]


def png_data(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack_from(">II", data, 16)
    start = data.index(b"IDAT")
    length = struct.unpack_from(">I", data, start - 4)[0]
    rows = zlib.decompress(data[start + 4:start + 4 + length])
    return width, height, rows


class RGBDContracts(unittest.TestCase):
    def setUp(self):
        self.sync = Synchronizer("instance")

    def feed(self, msgs, now=1., ros=SECOND, order=KINDS):
        for kind in order:
            self.sync.accept(kind, msgs[KINDS.index(kind)], now, ros)
        return self.sync.find(None)

    def test_endian_units_padding_and_independent_png(self):
        for encoding in ("16UC1", "32FC1"):
            for big in (0, 1):
                with self.subTest(encoding=encoding, big=big):
                    self.setUp()
                    status, pair = self.feed(images(depth_enc=encoding, big=big))
                    self.assertEqual(status, 200)
                    status, sample = pair.sample(1, 1)
                    self.assertEqual(status, 200)
                    self.assertAlmostEqual(sample["depth_m"], 7.001, places=5)
                    self.assertEqual(sample["rgb"], [13, 14, 15])
                    self.assertEqual(pair.sample(3, 0)[0], 400)
                    status, invalid = pair.sample(0, 0)
                    self.assertEqual(status, 422)
                    self.assertNotIn("depth_m", invalid)
                    self.assertEqual(png_data(pair.preview("color")), (3, 2, b"\0"+bytes(range(1,10))+b"\0"+bytes(range(10,19))))
                    w, h, rows = png_data(pair.preview("depth"))
                    self.assertEqual((w, h), (3, 2))
                    self.assertEqual(rows[:4], bytes(4))
                    self.assertNotEqual(rows[4:7], bytes(3))
                    self.assertEqual(rows[14:17], rows[17:20])  # both beyond display scale

    def test_invalid_floats(self):
        msgs = images(depth_enc="32FC1", values=[0, -1, math.inf, -math.inf, math.nan, .125])
        _, pair = self.feed(msgs)
        for u,v in [(0,0), (1,0), (2,0), (0,1), (1,1)]:
            self.assertEqual(pair.sample(u,v)[0], 422)
        self.assertEqual(pair.sample(2,1)[1]["depth_m"], .125)

    def test_timestamp_matching_clock_reset_expiration_and_history(self):
        msgs = images()
        msgs[1].header = header(SECOND + 20_000_000)
        _, first = self.feed(msgs, ros=SECOND+20_000_000, order=("depth", "info", "color"))
        self.assertEqual(first.details()["skew_ms"], 20)
        self.assertEqual(first.details()["stamp_ns_text"]["color"], str(SECOND))
        for i in range(1, 5):
            self.feed(images((i+1)*SECOND), i+1, (i+1)*SECOND)
        self.assertEqual(self.sync.find(first.id)[0], 409)
        self.sync.refresh(9., 5*SECOND)
        self.assertEqual((self.sync.status,self.sync.reason,self.sync.find(None)[0]), ("stale","stream_timeout",503))
        self.sync.refresh(9.1, SECOND)
        self.assertEqual(self.sync.reason, "clock_reset")
        self.assertEqual(self.feed(images(SECOND),10.,SECOND)[0], 200)

    def test_paused_ros_clock_expires_pending_inputs_and_pair_by_oldest_arrival(self):
        msgs = images()
        self.feed(msgs, 1., SECOND, ("color", "depth"))
        self.sync.refresh(6., SECOND)
        self.feed(msgs, 6., SECOND, ("info",))
        self.assertEqual(self.sync.find(None)[0], 503)
        self.feed(msgs, 6.1, SECOND, ("color", "depth"))
        self.assertEqual(self.sync.find(None)[0], 503)
        fresh = images(2 * SECOND)
        self.feed(fresh, 7., 2 * SECOND, ("color",))
        self.feed(fresh, 8.2, 2 * SECOND, ("depth","info"))
        self.assertEqual(self.sync.find(None)[0], 200)
        self.sync.refresh(9.05, 2 * SECOND)
        self.assertEqual(self.sync.find(None)[0], 503)
        self.assertEqual(self.sync.reason, "stream_timeout")

    def test_bad_triples_revoke_old_frame_then_recover(self):
        def wrong_size(ms):
            ms[2].width = 4
        def skew(ms):
            ms[2].header = header(2*SECOND+20_000_001)
        def bad_k(ms):
            ms[2].k[0] = 0
        def bad_frame(ms):
            ms[1].header.frame_id = "depth_optical_frame"
        def short(ms):
            ms[1].data = ms[1].data[:-1]
        for alter, error in [(wrong_size,"size_mismatch"),(skew,"timestamp_skew"),(bad_k,"invalid_intrinsics"),
                             (bad_frame,"frame_mismatch"),(short,"depth_layout")]:
            with self.subTest(error=error):
                self.setUp()
                self.feed(images())
                broken = images(2*SECOND)
                alter(broken)
                st, obj = self.feed(broken, 2., 2*SECOND+30_000_000)
                self.assertEqual((st,obj["reason"]), (503,error))
                st, _ = self.feed(images(3*SECOND), 3., 3*SECOND)
                self.assertEqual(st, 200)

    def test_config_clock_and_dimensions(self):
        for filename in ("simulation","realsense"):
            raw = (ROOT/"perception"/"rgbd"/"config"/(filename+".json")).read_bytes()
            spec = configuration(raw)
            spec["alignment"] = "unknown"
            with self.assertRaisesRegex(ValueError,"alignment_undeclared"):
                configuration(json.dumps(spec))
        self.assertEqual(self.feed(images(), 1., 0)[1]["reason"], "clock_unavailable")
        self.assertEqual(self.feed(images(), 2., 4*SECOND)[1]["reason"], "stamp_outside_clock")
        for kind in KINDS:
            msg = images()[KINDS.index(kind)]
            msg.width = 99999
            with self.assertRaises(ValueError):
                validate(kind, msg)

    def test_oversize_preview_matches_decimated_pixels(self):
        msgs = images()
        w = 641
        for msg in msgs:
            msg.width, msg.height = w, 1
        msgs[0].step = w * 3
        msgs[0].data = bytes([3, 2, 1]) * w
        msgs[1].step = w * 2
        msgs[1].data = struct.pack("<" + "H"*w, *([0]*(w-1)+[2345]))
        _, pair = self.feed(msgs)
        for kind in ("color","depth"):
            pw, ph, rows = png_data(pair.preview(kind))
            self.assertEqual((pw,ph), (321,1))
            self.assertNotEqual(rows[-3:], bytes(3))
        self.assertEqual(pair.sample(640,0)[1]["depth_m"], 2.345)


if __name__ == "__main__":
    unittest.main()
