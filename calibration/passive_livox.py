"""Passively receive dense MID-360 clouds for stationary spatial calibration.

Each scan is timestamped at the midpoint of its kernel host receive times. These
are not point acquisition times, and this source cannot supply time calibration.
"""
import argparse
from array import array
from ipaddress import IPv4Address
import re
import socket

from perception.spatial_facts.livox_capture import (
    CARTESIAN_MM, LIVOX_HEADER, checked_header, open_receiver, receive, release_process_capabilities,
)


class DenseWindow:
    """Collect every validated return across a short source-local receive window."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.payload = bytearray()
        self.first = self.last = self.last_device = self.clock = self.sequence = self.frame = None

    def add(self, data, received):
        header, error = checked_header(data)
        if error:
            self.reset()
            return None
        clock, _, device_stamp, sequence, frame = header
        # Discard mixed clock epochs and long network gaps. Bound memory even if
        # receive timestamps stall or a source sends an excessive packet rate.
        if self.first is not None and (clock != self.clock or device_stamp < self.last_device
                or (sequence != (self.sequence + 1) % 65536 and not (sequence == 0 and frame == (self.frame + 1) % 256))
                or received < self.last or received-self.last > 5_000_000
                or len(self.payload) + len(data) > 2_000_000):
            self.reset()
        if self.first is None:
            self.first, self.clock = received, clock
        self.last, self.last_device, self.sequence, self.frame = received, device_stamp, sequence, frame
        self.payload.extend(memoryview(data)[LIVOX_HEADER.size:])
        if received - self.first < 50_000_000:
            return None
        result = (self.first + (received-self.first)//2, self.payload)
        self.reset()
        return result


def metric_xyz(payload):
    import numpy as np
    record = np.dtype({"names":["xyz"], "formats":[("<i4",(3,))], "offsets":[0], "itemsize":CARTESIAN_MM.size})
    mm = np.frombuffer(payload, dtype=record)["xyz"]
    xyz = mm[mm.any(axis=1)].astype("<f4")
    xyz *= .001
    return xyz


def run(interface, destination, sources):
    with open_receiver(interface, destination, [ip for ip, _, _ in sources]) as receiver:
        # No ROS/DDS or numerical library worker threads may inherit capabilities.
        release_process_capabilities()
        import rclpy
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import PointCloud2, PointField
        windows = {ip.packed:DenseWindow() for ip, _, _ in sources}
        rclpy.init(args=[])
        # Independent single-sensor receivers on one DDS domain need distinct ROS names.
        name = f"passive_calibration_clouds_{sources[0][1]}" if len(sources) == 1 else "passive_calibration_clouds"
        node = rclpy.create_node(name, use_global_arguments=False)
        try:
            outputs = {}
            for ip, sensor, frame in sources:
                topic = "/edge/calibration/passive/" + sensor + "/points"
                pub = node.create_publisher(PointCloud2, topic, qos_profile_sensor_data)
                outputs[ip.packed] = (pub, frame)
                node.get_logger().info(f"{ip} -> {topic} [{frame}], host receive midpoint; stationary spatial calibration only")
            fields = [PointField(name=name, offset=i*4, datatype=PointField.FLOAT32, count=1)
                      for i,name in enumerate(("x","y","z"))]
            while rclpy.ok():
                try:
                    packet = receive(receiver, destination.packed, windows)
                except socket.timeout:
                    continue
                if packet is None:
                    continue
                ip, received, data = packet
                completed = windows[ip].add(data,received)
                if completed is None:
                    continue
                stamp, payload = completed
                xyz = metric_xyz(payload)
                if not len(xyz):
                    continue
                pub, frame = outputs[ip]
                cloud = PointCloud2()
                cloud.header.frame_id = frame
                cloud.header.stamp.sec, cloud.header.stamp.nanosec = divmod(stamp,10**9)
                cloud.fields, cloud.width, cloud.height = fields, len(xyz), 1
                cloud.point_step, cloud.row_step = 12, 12*len(xyz)
                cloud.is_dense = True
                cloud.data = array("B", xyz.tobytes())
                pub.publish(cloud)
        finally:
            node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--interface", required=True)
    p.add_argument("--destination", type=IPv4Address, required=True)
    p.add_argument("--source", action="append", required=True, nargs=3, metavar=("IP","ID","FRAME"))
    args = p.parse_args()
    try:
        sources = [(IPv4Address(ip), sid, frame) for ip, sid, frame in args.source]
    except ValueError as exc:
        p.error(str(exc))
    if (not 1 <= len(sources) <= 8 or any(len(set(values)) != len(values) for values in zip(*sources))
            or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}",sid) or
                   not re.fullmatch(r"[A-Za-z][A-Za-z0-9_./-]{0,127}",frame) for _,sid,frame in sources)):
        p.error("provide 1–8 unique sensor IPs, ROS names and native frames")
    run(args.interface,args.destination,sources)


if __name__ == "__main__":
    main()
