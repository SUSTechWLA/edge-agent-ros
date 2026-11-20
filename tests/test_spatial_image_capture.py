"""Actual PNG/ROS color order, published calibration identity, bounded history and digest retrieval."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace as Row
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_contracts.spatial_facts import SpatialFactError
from perception.spatial_facts.artifact_ring import ArtifactRing
from perception.spatial_facts.camera_info_capture import camera_info, publish
from perception.spatial_facts.gateway import read_source
from perception.spatial_facts.image_capture import depth_png, frame_sample


class ImageCapture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cal = self.root / "camera"
        self.cal.mkdir()
        self.stamp = 1_795_155_690_741_852_963
        header = Row(stamp=Row(sec=self.stamp // 10**9, nanosec=self.stamp % 10**9), frame_id="optical")
        self.msg = Row(header=header, width=2, height=1, encoding="bgr8", step=8,
                       data=bytes([90, 20, 5, 2, 240, 80, 99, 99]))
        msg = Row(header=header, width=2, height=1, distortion_model="plumb_bob", k=[10.]*9, d=[0.]*5,
                  r=[1.]*9, p=[1.]*12, roi=Row(x_offset=0, y_offset=0, width=0, height=0, do_rectify=False),
                  binning_x=0, binning_y=0)
        self.calibration, raw = camera_info(msg, "rover", "d435.color.info", "optical")
        publish(self.cal, self.calibration, raw)

    def capture(self, msg=None, now=None):
        return frame_sample(msg or self.msg, "rover", "d435.color.image", "optical", self.cal, "d435.color.info",
                            self.stamp + 1 if now is None else now)

    def test_color_pixels_padding_digest_and_original_ros_stamp(self):
        sample, png = self.capture()
        self.assertEqual(sample["stamp_ns"], self.stamp)
        self.assertEqual(sample["artifact_ref"], "sha256:" + hashlib.sha256(png).hexdigest())
        self.assertEqual(sample["calibration_ref"], self.calibration["artifact_ref"])
        self.assertEqual(struct.unpack(">II", png[16:24]), (2, 1))
        chunk = png.index(b"IDAT")
        length, = struct.unpack(">I", png[chunk-4:chunk])
        self.assertEqual(zlib.decompress(png[chunk+4:chunk+4+length]), bytes([0, 5, 20, 90, 80, 240, 2]))

    def test_declines_stale_or_future_image_wrong_calibration_and_raw_frame(self):
        for now in (self.stamp - 1, self.stamp + 1_000_000_001):
            with self.subTest(now=now), self.assertRaisesRegex(SpatialFactError, "image_stamp_invalid"):
                self.capture(now=now)
        self.msg.width = 1
        with self.assertRaisesRegex(SpatialFactError, "calibration_mismatch"):
            self.capture()
        self.msg.width = 2
        self.msg.encoding = "16UC1"
        with self.assertRaisesRegex(SpatialFactError, "color_encoding"):
            self.capture()
        self.msg.encoding = "rgb8"
        path = self.cal / "artifacts" / (self.calibration["artifact_ref"][7:] + ".json")
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(SpatialFactError, "calibration_digest"):
            self.capture()

    def test_lossless_depth_both_byte_orders_zero_and_row_padding(self):
        original = [0, 256, 1157, 65535]
        for big_endian in (0, 1):
            endian = ">" if big_endian else "<"
            payload = b"".join(struct.pack(endian+"HH", *original[y:y+2]) + b"\xaa\xbb" for y in (0, 2))
            msg = Row(encoding="16UC1", width=2, height=2, step=6, is_bigendian=big_endian, data=payload)
            png = depth_png(msg)
            self.assertEqual(struct.unpack(">IIBB", png[16:26]), (2, 2, 16, 0))
            start = png.index(b"IDAT")
            length, = struct.unpack(">I", png[start-4:start])
            rows = zlib.decompress(png[start+4:start+4+length])
            self.assertEqual(rows, b"\0" + struct.pack(">HH", *original[:2]) +
                             b"\0" + struct.pack(">HH", *original[2:]))
        msg.step = 3
        with self.assertRaisesRegex(SpatialFactError, "depth_layout"):
            depth_png(msg)
        msg.step, msg.encoding = 6, "32FC1"
        with self.assertRaisesRegex(SpatialFactError, "depth_encoding"):
            depth_png(msg)

    def test_depth_sample_links_full_grid_to_same_calibration(self):
        self.msg.encoding, self.msg.step = "16UC1", 4
        self.msg.is_bigendian, self.msg.data = 0, b"\0\0\x01\xfa"
        sample, png = frame_sample(self.msg, "rover", "d435.aligned.depth", "optical",
                                   self.cal, "d435.color.info", self.stamp+1, stream="depth16")
        self.assertEqual((sample["source_id"], sample["calibration_ref"]),
                         ("d435.aligned.depth", self.calibration["artifact_ref"]))
        self.assertEqual(sample["artifact_ref"], "sha256:" + hashlib.sha256(png).hexdigest())

    def test_content_ring_retains_repeated_frames_evicts_and_reopens(self):
        output = self.root / "rgb"
        output.mkdir()

        def open_ring():
            return ArtifactRing(output, "rover", "d435.color.image", media_type="image/png", suffix=".png", retain=3)

        ring = open_ring()
        first, png = self.capture()
        ring.publish(first, png)
        ring.publish({**first, "stamp_ns": self.stamp + 1}, png)
        self.assertEqual(1, len(list(ring.store.iterdir())))
        original = ring.store / (first["artifact_ref"][7:] + ".png")
        self.assertEqual(original.read_bytes(), png)
        self.assertEqual(2, len(json.loads(ring.index.read_bytes())["frames"]))
        ring = open_ring()
        ring.publish(first, png)
        self.assertEqual(2, len(ring.frames))
        for i in range(2, 5):
            self.msg.data = bytes([i]) * 8
            sample, png = self.capture()
            ring.publish({**sample, "stamp_ns": self.stamp + i}, png)
        self.assertFalse(original.exists())
        self.assertEqual(3, len(list(ring.store.iterdir())))
        batch = read_source(output / "observations.json", "rover", self.stamp + 5)
        last = batch["samples"][0]
        self.assertEqual(last["stamp_ns"], self.stamp + 4)
        self.assertEqual(png, (ring.store / (last["artifact_ref"][7:] + ".png")).read_bytes())
        self.assertEqual([str(self.stamp + i) for i in range(2, 5)], [f["stamp_ns"] for f in ring.frames])
        with self.assertRaisesRegex(SpatialFactError, "invalid_artifact"):
            ring.publish({**first, "stamp_ns": self.stamp + 5}, png)


if __name__ == "__main__":
    unittest.main()
