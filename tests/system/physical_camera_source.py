"""Test-only ROS publisher and independent bounded driver witness for physical capture acceptance."""
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[2] / "perception" / "camera"))
from physical_io import frame_bytes


def main():
    rclpy.init()
    node = Node("physical_camera_source")
    topic = os.getenv("EDGE_CAMERA_TOPIC","/physical/camera/image")
    source = os.getenv("EDGE_CAMERA_SOURCE","camera.front")
    pub = node.create_publisher(Image,topic,qos_profile_sensor_data)
    root = Path("/witness")
    root.mkdir(exist_ok=True)
    db = sqlite3.connect(root / "source.sqlite")
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        CREATE TABLE IF NOT EXISTS frames(digest TEXT PRIMARY KEY, source TEXT, frame TEXT, stamp INTEGER,
            width INTEGER, height INTEGER, pixel_sha TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS heartbeat(singleton INTEGER PRIMARY KEY,source TEXT,last_seen INTEGER);
        CREATE TABLE IF NOT EXISTS readers(gid TEXT PRIMARY KEY, first_stamp INTEGER, last_stamp INTEGER,
                                            offered_frames INTEGER);
    """)
    seq = 0
    try:
        while rclpy.ok():
            seq += 1
            msg = Image()
            msg.header.stamp = node.get_clock().now().to_msg()
            msg.header.frame_id = "camera_optical"
            msg.width,msg.height,msg.step,msg.encoding = 16,8,16,"mono8"
            msg.data = bytes((seq+i) % 256 for i in range(128))
            digest = "sha256:" + hashlib.sha256(frame_bytes(msg)).hexdigest()
            stamp = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
            endpoints = node.get_subscriptions_info_by_topic(topic)
            with db:
                db.execute("INSERT OR REPLACE INTO heartbeat VALUES(1,?,?)",(source,time.monotonic_ns()))
                # A one-shot subscriber can receive a frame and disappear between graph samples.
                # Offer a sample only after observing the camera; record every reader on this topic.
                if any(endpoint.node_name == "edge_physical_camera" for endpoint in endpoints):
                    db.execute("INSERT OR IGNORE INTO frames VALUES(?,?,?,?,?,?,?)",
                               (digest,source,msg.header.frame_id,stamp,msg.width,msg.height,hashlib.sha256(bytes(msg.data)).hexdigest()))
                    db.execute("DELETE FROM frames WHERE rowid <= (SELECT max(rowid)-1200 FROM frames)")
                    for endpoint in endpoints:
                        gid = bytes(endpoint.endpoint_gid).hex()
                        db.execute("""INSERT INTO readers VALUES(?,?,?,1) ON CONFLICT(gid)
                            DO UPDATE SET last_stamp=excluded.last_stamp,offered_frames=offered_frames+1""",(gid,stamp,stamp))
                    pub.publish(msg)
            rclpy.spin_once(node,timeout_sec=.10)
    finally:
        db.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
