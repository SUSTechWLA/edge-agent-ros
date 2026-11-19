"""Independent ROS camera verifier. Only reads the driver's observations and capture artifacts."""
import hashlib
import os
from pathlib import Path
import sqlite3
import time

import rclpy
from rclpy.node import Node
from rclpy.serialization import deserialize_message
from sensor_msgs.msg import Image

from frames import MAX_RAW, raw_frame
from physical_io import ControlError, artifact_ref, request


def frame_proof(job, captures, witness):
    lease, candidate = job["lease"], job["candidate"]
    op = lease["operation_id"]
    ref = artifact_ref(op,lease["epoch"])
    proof = {"operation_id":op,"resource_id":lease["resource_id"],"epoch":lease["epoch"],
             **{k:candidate[k] for k in ("frame_id","stamp_ns","source_id","artifact_sha256")},
             "evidence_ref":ref,"verdict":"fail"}
    if candidate["artifact_ref"] != ref:
        return proof
    path = Path(captures) / ref
    if path.resolve() != path:
        return proof
    if not path.exists():
        return None
    if not path.is_file():
        return proof
    with path.open("rb") as stream:
        raw = stream.read(MAX_RAW + 4097)
    if (len(raw) > MAX_RAW + 4096 or "sha256:" + hashlib.sha256(raw).hexdigest() != candidate["artifact_sha256"]):
        return proof
    # Look up independent sensor-side evidence before decoding any untrusted CDR.
    observed = witness.execute("""SELECT source,frame,stamp,width,height,pixel_sha FROM frames WHERE digest=?""",
                               (candidate["artifact_sha256"],)).fetchone()
    if not observed:
        return None
    if observed[:3] != (candidate["source_id"],candidate["frame_id"],candidate["stamp_ns"]):
        return proof
    msg = deserialize_message(raw,Image)
    stamp = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
    if (observed[3:5] == (msg.width,msg.height) and hashlib.sha256(bytes(msg.data)).hexdigest() == observed[5]
            and msg.header.frame_id == candidate["frame_id"]
            and stamp == candidate["stamp_ns"]):
        raw_frame(msg)
        proof["verdict"] = "pass"
    return proof


class PhysicalVerifier(Node):
    def __init__(self):
        super().__init__("edge_physical_verifier")
        self.topic = self.resolve_topic_name(os.getenv("EDGE_CAMERA_TOPIC","/physical/camera/image"))
        self.source = os.getenv("EDGE_CAMERA_SOURCE","camera.front")
        self.quiet = None
        self.last_error = None

    def stopped(self, job, witness):
        now = time.monotonic_ns()
        beat = witness.execute("SELECT source,last_seen FROM heartbeat WHERE singleton=1").fetchone()
        present = any(sub.node_name == "edge_physical_camera" for sub in self.get_subscriptions_info_by_topic(self.topic))
        source_live = (beat is not None and beat[0] == self.source and 0 <= now-beat[1] < 1_000_000_000
                       and self.count_publishers(self.topic) > 0)
        identity = ("resource_id","plugin_id","epoch","boot_id","stop_id","camera_instance","verifier_instance")
        key = tuple(job[k] for k in identity)
        if present or not source_live:
            self.quiet = None
            return
        if self.quiet is None or self.quiet[0] != key:
            self.quiet = [key,now,beat[1],1]
            return
        if beat[1] <= self.quiet[2]:
            return
        self.quiet[2] = beat[1]
        self.quiet[3] += 1
        duration = now - self.quiet[1]
        if duration >= 600_000_000 and self.quiet[3] >= 3:
            proof = {k:job[k] for k in identity}
            proof.update(verdict="stopped",subscription_count=0, source_alive=True, observations=self.quiet[3],
                         window_ns=duration,evidence_ref=f"witness/quiescence/{job['epoch']}/{now}")
            request("verifier","stopped",proof=proof)
            self.quiet = None

    def tick(self):
        try:
            job = request("verifier","next",source_id=self.source,topic=self.topic,
                          ros_domain_id=self.context.get_domain_id())
            if job:
                # read-only mount, no shared writable artifact or registry path.
                witness = sqlite3.connect("file:/witness/source.sqlite?mode=ro",uri=True,timeout=.25)
                try:
                    if job["kind"] == "candidate":
                        proof = frame_proof(job,Path("/captures"),witness)
                        if proof is not None:
                            request("verifier","verify",proof=proof)
                    elif job["kind"] == "stop":
                        self.stopped(job,witness)
                finally:
                    witness.close()
            self.last_error = None
        except (OSError,sqlite3.Error,ControlError,ValueError,RuntimeError,KeyError) as error:
            self.quiet = None
            code = error.code if isinstance(error,ControlError) else type(error).__name__
            if self.last_error != code:
                self.get_logger().warning(str(code))
                self.last_error = code


def main():
    rclpy.init()
    node = PhysicalVerifier()
    try:
        while rclpy.ok():
            node.tick()
            rclpy.spin_once(node,timeout_sec=.12)
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
