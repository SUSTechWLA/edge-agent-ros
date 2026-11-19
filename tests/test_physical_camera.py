"""SDD-045 P2 camera adapter boundary in the ROS image; no external device needed."""
import hashlib
import sqlite3
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "perception" / "camera"))
from physical import DeviceGate, PhysicalCamera
from physical_io import ControlError, artifact_ref, frame_bytes, write_once
from physical_verifier import PhysicalVerifier, frame_proof
from rclpy.serialization import deserialize_message
from sensor_msgs.msg import Image


class CameraContracts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.lease = {"operation_id":"task/step/1","resource_id":"camera.front","provider_id":"provider","plugin_id":"camera",
                      "boot_id":"first-boot","content_digest":"sha256:"+("a"*64),
                      "epoch":1,"valid_until_ns":time.monotonic_ns()+2_000_000_000}

    def test_pending_device_intent_and_highest_fence_survive_restart(self):
        first = DeviceGate(self.root)
        self.assertTrue(first.claim(self.lease))
        self.assertFalse(first.claim(self.lease))
        with self.assertRaises(BlockingIOError):
            DeviceGate(self.root)
        first.close()
        new = DeviceGate(self.root)
        self.addCleanup(new.close)
        self.assertFalse(new.claim(self.lease))
        with self.assertRaises(ControlError):
            new.claim({**self.lease,"operation_id":"different"})
        with new.db:
            new.db.execute("UPDATE captures SET state='candidate_reported'")
        self.assertTrue(new.claim({**self.lease,"operation_id":"different"}))
        with self.assertRaises(ControlError):
            new.claim({**self.lease,"operation_id":"impostor","provider_id":"another"})
        next_lease = {**self.lease, "operation_id":"next","epoch":2}
        self.assertTrue(new.claim(next_lease))
        with self.assertRaises(ControlError):
            new.fence(self.lease)
        with self.assertRaises(ControlError):
            new.fence({**next_lease,"valid_until_ns":time.monotonic_ns()-1})

    def test_independent_original_frame_and_artifact_needed_for_pass(self):
        msg = Image()
        msg.header.frame_id = "front"
        msg.header.stamp.sec = 23
        msg.width,msg.height,msg.step,msg.encoding = 4,2,4,"mono8"
        msg.data = bytes(range(8))
        raw = frame_bytes(msg)
        self.assertEqual(deserialize_message(raw,Image),msg)
        digest = "sha256:"+hashlib.sha256(raw).hexdigest()
        ref = artifact_ref("task/step/1",1)
        job = {"lease":self.lease,"candidate":{"artifact_ref":ref,"artifact_sha256":digest,"source_id":"front",
                                              "frame_id":"front","stamp_ns":23_000_000_000}}
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE frames(digest,source,frame,stamp,width,height,pixel_sha)")
        self.assertIsNone(frame_proof(job,self.root,db))
        write_once(self.root/ref,raw)
        with self.assertRaises(FileExistsError):
            write_once(self.root/ref,b"new frame")
        self.assertIsNone(frame_proof(job,self.root,db))
        db.execute("INSERT INTO frames VALUES(?,?,?,?,?,?,?)",(digest,"front","front",23_000_000_000,4,2,
                                                             hashlib.sha256(bytes(msg.data)).hexdigest()))
        self.assertEqual(frame_proof(job,self.root,db)["verdict"],"pass")
        for change in ({"stamp_ns":24_000_000_000},{"artifact_ref":"../outside"},{"frame_id":"rear"}):
            wrong = {**job,"candidate":{**job["candidate"],**change}}
            self.assertEqual(frame_proof(wrong,self.root,db)["verdict"],"fail")
        (self.root/ref).write_bytes(raw[:-1])
        self.assertEqual(frame_proof(job,self.root,db)["verdict"],"fail")

    def test_real_ros_node_acquires_one_subscription_and_drops_it_after_candidate(self):
        import rclpy
        rclpy.init()
        self.addCleanup(rclpy.shutdown)
        gate = DeviceGate(self.root)
        self.addCleanup(gate.close)
        node = PhysicalCamera(gate)
        self.addCleanup(node.destroy_node)
        claims = [{"kind":"capture","lease":self.lease}]
        delivered = []

        def controller(role,action,**args):
            if action == "next":
                return claims.pop(0) if claims else None
            if action == "candidate":
                delivered.append(args["measurement"])
            return True

        with patch("physical.request",side_effect=controller):
            node.tick()
            self.assertIsNotNone(node.frame_subscription)
            msg = Image()
            msg.header.stamp = node.get_clock().now().to_msg()
            msg.header.frame_id = "front"
            msg.width,msg.height,msg.step,msg.encoding,msg.data = 2,1,2,"mono8",bytes([10,20])
            node.receive(msg)
            node.receive(msg)
            node.tick()
        self.assertEqual(len(delivered),1)
        self.assertIsNone(node.frame_subscription)
        self.assertEqual(deserialize_message((self.root/delivered[0]["artifact_ref"]).read_bytes(),Image),msg)

    def test_stop_window_requires_consecutive_new_source_beats_for_this_challenge(self):
        witness = sqlite3.connect(":memory:")
        self.addCleanup(witness.close)
        witness.execute("CREATE TABLE heartbeat(singleton INTEGER PRIMARY KEY,source,last_seen)")
        readers = []
        verifier = SimpleNamespace(source="front",topic="/raw",quiet=None,
                                   get_subscriptions_info_by_topic=lambda _: readers,count_publishers=lambda _: 1)
        job = dict(resource_id="front",plugin_id="capture",epoch=7,boot_id="boot",stop_id="old",
                   camera_instance="camera",verifier_instance="verifier")
        with patch("physical_verifier.request") as publish, patch("physical_verifier.time.monotonic_ns") as clock:
            def poll(ms):
                clock.return_value = ms * 1_000_000
                witness.execute("INSERT OR REPLACE INTO heartbeat VALUES(1,'front',?)",(clock.return_value,))
                PhysicalVerifier.stopped(verifier,job,witness)
            poll(100)
            poll(350)
            job["stop_id"] = "after-drain"
            poll(750)
            readers.append(SimpleNamespace(node_name="edge_physical_camera"))
            poll(1000)
            readers.clear()
            poll(1100)
            poll(1300)
            publish.assert_not_called()
            poll(1800)
            proof = publish.call_args.kwargs["proof"]
            self.assertEqual(proof["stop_id"],"after-drain")
            self.assertEqual(proof["window_ns"],700_000_000)
            self.assertEqual(proof["observations"],3)


if __name__ == "__main__":
    unittest.main()
