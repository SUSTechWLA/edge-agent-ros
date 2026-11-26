import unittest
import zlib

from calibration.passive_livox import DenseWindow, metric_xyz
from perception.spatial_facts.livox_capture import CARTESIAN_MM, LIVOX_HEADER


def packet(seq, frame=1):
    pts = CARTESIAN_MM.pack(1000,-2000,50,1,0)*95 + CARTESIAN_MM.pack(0,0,0,0,0)
    ts = (seq + frame*65536)*500000
    crc = zlib.crc32(ts.to_bytes(8,"little")+pts)
    return LIVOX_HEADER.pack(0,LIVOX_HEADER.size+len(pts),0,96,seq,frame,1,0,crc,ts)+pts


class FullDensityCalibration(unittest.TestCase):
    def test_full_returns_and_host_midpoint(self):
        window = DenseWindow()
        for i in range(100):
            self.assertIsNone(window.add(packet(i),1_000_000_000+i*500000))
        stamp, payload = window.add(packet(100),1_050_000_000)
        self.assertEqual(stamp,1_025_000_000)
        xyz = metric_xyz(payload)
        self.assertEqual(xyz.shape,(101*95,3))
        self.assertEqual(xyz.dtype.str,"<f4")
        self.assertAlmostEqual(float(xyz[0,2]),.05,places=6)
        self.assertEqual(xyz[0,:2].tolist(),[1.,-2.])

    def test_discontinuities_discard_partial_window(self):
        for failure in ("lost_packet","bad_crc","host_clock","long_receive_gap"):
            with self.subTest(failure=failure):
                window = DenseWindow()
                for i in range(90):
                    window.add(packet(i),i*500000)
                seq, ts = 90,45_000_000
                if failure == "lost_packet": seq += 1
                if failure == "host_clock": ts = 100
                if failure == "long_receive_gap": ts += 20_000_000
                data = packet(seq)
                if failure == "bad_crc": data = data[:-1]+b"x"
                self.assertIsNone(window.add(data,ts))
                self.assertIn(window.first,(None,ts))
                self.assertLessEqual(len(window.payload),96*CARTESIAN_MM.size)

    def test_frame_reset_keeps_contiguous_samples(self):
        window = DenseWindow()
        for i in range(100):
            frame, seq = (1,i) if i < 50 else (2,i-50)
            self.assertIsNone(window.add(packet(seq,frame),i*500000))
        self.assertIsNotNone(window.add(packet(50,2),50_000_000))
