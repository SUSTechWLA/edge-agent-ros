"""Deployment-only ROS Image capture. A durable original operation can consume at most one frame."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from frames import raw_frame
from physical_io import ControlError, artifact_ref, frame_bytes, request, write_once


class DeviceGate:
    """Single device adapter + durable highest fencing epoch, even after a controller/adapter restart."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.lock = (self.directory / "device.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise
        self.db = sqlite3.connect(self.directory / "device.sqlite")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS fences(resource TEXT PRIMARY KEY,epoch INTEGER NOT NULL,owner TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS captures(op TEXT PRIMARY KEY,lease TEXT NOT NULL,state TEXT NOT NULL,
                candidate TEXT,code TEXT);
        """)

    def claim(self, lease):
        op,resource,epoch = lease["operation_id"],lease["resource_id"],lease["epoch"]
        owner = json.dumps([lease[k] for k in ("plugin_id","provider_id","content_digest","boot_id")])
        if type(epoch) is not int or epoch <= 0:
            raise ControlError("plugin.fence_rejected")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM captures WHERE op=?", (op,)).fetchone():
                return False
            prior = self.db.execute("SELECT epoch,owner FROM fences WHERE resource=?", (resource,)).fetchone()
            if prior and (epoch < prior[0] or epoch == prior[0] and owner != prior[1]):
                raise ControlError("plugin.fence_rejected")
            if prior and epoch == prior[0]:
                active = self.db.execute("""SELECT 1 FROM captures WHERE json_extract(lease,'$.resource_id')=?
                    AND json_extract(lease,'$.epoch')=? AND state<>'candidate_reported' LIMIT 1""",
                                         (resource,epoch)).fetchone()
                if active:
                    raise ControlError("plugin.lease_conflict")
            self.db.execute("INSERT OR REPLACE INTO fences VALUES(?,?,?)",(resource,epoch,owner))
            self.db.execute("INSERT INTO captures VALUES(?,?,'armed',NULL,NULL)",(op,json.dumps(lease)))
        return True

    def fence(self, lease):
        row = self.db.execute("SELECT epoch FROM fences WHERE resource=?",(lease["resource_id"],)).fetchone()
        if not row or row[0] != lease["epoch"] or time.monotonic_ns() >= lease["valid_until_ns"]:
            raise ControlError("plugin.fence_rejected")

    def close(self):
        self.db.close()
        self.lock.close()


class PhysicalCamera(Node):
    def __init__(self, gate):
        super().__init__("edge_physical_camera")
        self.gate = gate
        self.frame_subscription = None
        self.lease = None
        self.topic = self.resolve_topic_name(os.getenv("EDGE_CAMERA_TOPIC","/physical/camera/image"))
        self.source = os.getenv("EDGE_CAMERA_SOURCE","camera.front")
        self.last_error = None
        # A previous process may have observed a frame while crashing. Never subscribe again for that operation.
        with gate.db:
            gate.db.execute("UPDATE captures SET state='unknown',code='camera.process_lost' WHERE state='armed'")

    def drop(self):
        if self.frame_subscription is not None:
            self.destroy_subscription(self.frame_subscription)
            self.frame_subscription = None
        self.lease = None

    def reject(self, code):
        if self.lease:
            with self.gate.db:
                self.gate.db.execute("UPDATE captures SET state='unknown',code=? WHERE op=? AND state='armed'",
                                     (code,self.lease["operation_id"]))
        self.drop()

    def check(self, lease):
        self.gate.fence(lease)
        if request("camera","check",lease=lease) is not True:
            raise ControlError("plugin.fence_rejected")

    def receive(self, msg):
        lease = self.lease
        if lease is None:
            return
        try:
            self.check(lease)
            stamp = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
            if not msg.header.frame_id or not 0 <= self.get_clock().now().nanoseconds - stamp <= 2_000_000_000:
                raise ControlError("camera.frame_stale")
            raw_frame(msg)  # Validate supported dimensions, encoding, padding and raw data length.
            raw = frame_bytes(msg)
            op = lease["operation_id"]
            ref = artifact_ref(op,lease["epoch"])
            self.check(lease)
            write_once(self.gate.directory / ref,raw)
            measurement = {"artifact_ref":ref,"artifact_sha256":"sha256:"+hashlib.sha256(raw).hexdigest(),
                           "source_id":self.source,"frame_id":msg.header.frame_id,"stamp_ns":stamp}
            with self.gate.db:
                self.gate.db.execute("UPDATE captures SET state='candidate',candidate=? WHERE op=?",
                                     (json.dumps(measurement),op))
            self.drop()
        except (ControlError, ValueError, OSError, sqlite3.Error) as error:
            self.reject(error.code if isinstance(error,ControlError) else "camera.capture_failed")

    def tick(self):
        try:
            self.deliver()
            if self.lease:
                self.check(self.lease)
            else:
                item = request("camera","next",source_id=self.source,topic=self.topic,
                               ros_domain_id=self.context.get_domain_id())
                if item and item.get("kind") not in ("stop","released"):
                    lease = item["lease"]
                    if request("camera","check",lease=lease) is True and self.gate.claim(lease):
                        self.lease = lease
                        self.check(lease)
                        self.frame_subscription = self.create_subscription(Image,self.topic,self.receive,qos_profile_sensor_data)
            self.last_error = None
        except (OSError,ControlError,ValueError,KeyError) as error:
            code = error.code if isinstance(error,ControlError) else "camera.controller_unavailable"
            self.reject(code)
            if self.last_error != code:
                self.get_logger().warning(str(code))
                self.last_error = code

    def deliver(self):
        rows = self.gate.db.execute("SELECT op,lease,state,candidate,code FROM captures WHERE state IN ('candidate','unknown') LIMIT 4").fetchall()
        for op,raw,state,measurement,code in rows:
            lease = json.loads(raw)
            try:
                if state == "candidate":
                    request("camera","candidate",lease=lease,measurement=json.loads(measurement))
                else:
                    request("camera","unknown",lease=lease,code=code)
            except ControlError as error:
                # The controller may have expired or already reconciled this operation.
                current = request("camera","status",operation_id=op)
                if current["state"] not in ("RECOVERY_REQUIRED","SUCCEEDED","FAILED","RELEASED"):
                    raise error
            with self.gate.db:
                self.gate.db.execute("UPDATE captures SET state=? WHERE op=?",(state + "_reported",op))


def main():
    rclpy.init()
    gate = DeviceGate("/captures")
    node = PhysicalCamera(gate)
    try:
        while rclpy.ok():
            node.tick()
            rclpy.spin_once(node,timeout_sec=.08)
    finally:
        node.drop()
        node.destroy_node()
        gate.close()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
