"""Dual MID-360 preview capture and isolated Linux UDP-consumer coexistence."""
import hashlib
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from perception.spatial_facts.artifact_ring import ArtifactRing
from perception.spatial_facts.livox_capture import (CARTESIAN_MM, DEST_PORT, LIVOX_HEADER, PreviewWindow,
                                                   SOURCE_PORT, open_receiver, publish, receive)


def packet(stamp=1234, *, crc=True):
    points = b"".join(CARTESIAN_MM.pack(1000 + i, -2000, 123, 75, 0) for i in range(96))
    checksum = zlib.crc32(stamp.to_bytes(8, "little") + points) if crc else 0
    return LIVOX_HEADER.pack(0, len(points) + LIVOX_HEADER.size, 0, 96, 1, 2, 1, 0, checksum, stamp) + points


class Capture(unittest.TestCase):
    def test_preserves_clocks_and_views_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            for sensor in ("front", "rear"):
                source, frame = "mid360." + sensor, sensor + "_mid360"
                window = PreviewWindow(source, frame)
                for index in range(112):
                    self.assertIsNone(window.add(packet(stamp=1000 + index, crc=index != 0), 100000 + index))
                directory = Path(tmp) / source
                directory.mkdir()
                ring = ArtifactRing(directory, "rover", source, media_type="application/json", suffix=".json")
                publish(ring, "rover", window)
                sample = json.loads((directory / "observations.json").read_bytes())["samples"][0]
                digest = sample["artifact_ref"][7:]
                data = (directory / "artifacts" / (digest + ".json")).read_bytes()
                self.assertEqual(digest, hashlib.sha256(data).hexdigest())
                row = json.loads(data)
                self.assertEqual((frame, "1111", "100111", 0), (row["frame_id"], row["last_device_stamp_ns"],
                                 row["last_receive_ns"], row["device_time_type"]))
                self.assertEqual((100111, "unix", "host_packet_receive"), (sample["stamp_ns"], sample["clock_id"],
                                 row["stamp_basis"]))
                self.assertEqual({"device_field_zero": 1, "verified": 111}, row["crc_counts"])
                self.assertEqual(112, row["packet_count"])
                self.assertEqual(23 * 96, row["valid_point_count"])
                self.assertGreater(len(row["points"]), 0)
                self.assertTrue(all(1 <= p[0] <= 1.096 and p[1:] == [-2, .123, 75] for p in row["points"]))

    def test_rejects_corrupt_wrong_type_time_reset_and_truncation(self):
        window = PreviewWindow("mid360.front", "front_mid360")
        good = packet()
        self.assertEqual("invalid_crc", window.add(good[:-1] + b"\xff", 50))
        self.assertEqual("invalid_size", window.add(good[:-1], 50))
        self.assertEqual("unsupported_packet", window.add(good[:10] + b"\x03" + good[11:], 50))
        self.assertIsNone(window.add(good, 50))
        self.assertEqual("time_discontinuity", window.add(packet(stamp=1), 60))
        self.assertEqual(1, window.packets)

    @unittest.skipUnless(os.environ.get("EDGE_LIVOX_ISOLATED_LOOPBACK") == "1" and hasattr(socket, "AF_PACKET"),
                         "run on Linux in a private network namespace with CAP_NET_RAW")
    def test_reader_relinquishes_raw_socket_authority_after_setup(self):
        run = subprocess.run([sys.executable, "-c", """
from pathlib import Path
import socket
from ipaddress import IPv4Address
from perception.spatial_facts.livox_capture import open_receiver, release_process_capabilities
with open_receiver('lo', IPv4Address('127.0.0.1'), [IPv4Address('127.0.0.2')]) as rx:
    release_process_capabilities()
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    assert all(int(status[k], 16) == 0 for k in ('CapEff', 'CapPrm', 'CapInh'))
    assert status['NoNewPrivs'].strip() == '1'
    try:
        socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, 0)
    except PermissionError:
        pass
    else:
        raise AssertionError('unexpected authority to create another packet socket')
"""], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)

    @unittest.skipUnless(os.environ.get("EDGE_LIVOX_ISOLATED_LOOPBACK") == "1" and hasattr(socket, "AF_PACKET"),
                         "run on Linux in a private network namespace with CAP_NET_RAW")
    def test_exact_passive_copy_coexists_with_driver_receiver(self):
        dest = IPv4Address("127.0.0.1")
        sensors = [IPv4Address("127.0.0.2"), IPv4Address("127.0.0.3")]
        with open_receiver("lo", dest, sensors) as rx, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as driver:
            driver.bind((str(dest), DEST_PORT))
            driver.settimeout(1)
            for bad in (("127.0.0.4", SOURCE_PORT, DEST_PORT), (str(sensors[0]), SOURCE_PORT + 2, DEST_PORT),
                        (str(sensors[0]), SOURCE_PORT, DEST_PORT + 2)):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    sender.bind(bad[:2])
                    sender.sendto(b"not-allowed", (str(dest), bad[2]))
                with self.assertRaises(TimeoutError):
                    receive(rx, dest.packed, {s.packed for s in sensors})
                if bad[2] == DEST_PORT:
                    self.assertEqual(b"not-allowed", driver.recv(1500))
            for index, source in enumerate(sensors):
                data = packet(stamp=2000 + index)
                before = time.time_ns()
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sensor:
                    sensor.bind((str(source), SOURCE_PORT))
                    sensor.sendto(data, (str(dest), DEST_PORT))
                src_ip, stamp, preview = receive(rx, dest.packed, {s.packed for s in sensors})
                self.assertEqual((source.packed, data), (src_ip, preview))
                self.assertTrue(before <= stamp <= time.time_ns())
                self.assertEqual(data, driver.recv(1500))


if __name__ == "__main__":
    unittest.main()
