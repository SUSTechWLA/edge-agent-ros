"""Read-only, bounded projection of Gazebo instrument samples into the odometry frame."""
from collections import deque
import math
from xml.etree import ElementTree

from edge_contracts.spatial_facts import rotation
from perception.rgbd.pairs import depth_reader, stamp, validate


def transform(matrix, point):
    x, y, z = point
    return tuple(a*x + b*y + c*z + d for a, b, c, d in matrix)


def mounting(sensor):
    if sensor is None or sensor.find("pose") is None or sensor.find("pose").get("relative_to"):
        raise ValueError("view_mount_unregistered")
    x, y, z, roll, pitch, yaw = map(float, sensor.findtext("pose").split())
    cr, cp, cy = (math.cos(a/2) for a in (roll, pitch, yaw))
    sr, sp, sy = (math.sin(a/2) for a in (roll, pitch, yaw))
    return rotation((x,y,z), (sr*cp*cy-cr*sp*sy, cr*sp*cy+sr*cp*sy, cr*cp*sy-sr*sp*cy, cr*cp*cy+sr*sp*sy))


class LocalObservations:
    def __init__(self, world):
        link = ElementTree.parse(world).find("world/model[@name='edge_rover']/link[@name='base_link']")
        if link is None:
            raise ValueError("view_reference_link_unavailable")
        self.camera_mount, self.laser_mount = None, None
        self.optical_frame = self.camera_topic = self.laser_frame = None
        camera, laser = link.find("sensor[@type='rgbd_camera']"), link.find("sensor[topic='/scan']")
        if camera is not None and camera.findtext("camera/optical_frame_id") and camera.findtext("topic"):
            try:
                self.camera_mount = mounting(camera)
                self.optical_frame = camera.findtext("camera/optical_frame_id")
                self.camera_topic = camera.findtext("topic")
            except ValueError:
                pass  # A sensor without a registered mount contributes no spatial points.
        if laser is not None and laser.findtext("gz_frame_id"):
            try:
                self.laser_mount = mounting(laser)
                self.laser_frame = laser.findtext("gz_frame_id")
            except ValueError:
                pass
        self.poses = deque(maxlen=96)
        self.images = deque(maxlen=4)
        self.infos = deque(maxlen=4)
        self.cache = {}

    def pose(self, msg):
        if msg.header.frame_id != "odom" or msg.child_frame_id != "base_link":
            return
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        try:
            matrix = rotation((p.x, p.y, p.z), (q.x, q.y, q.z, q.w))
        except ValueError:
            return
        self.poses.append((stamp(msg), matrix))

    def camera(self, kind, msg):
        if not self.optical_frame or msg.header.frame_id != self.optical_frame:
            return
        try:
            validate(kind, msg)
        except ValueError:
            return
        (self.infos if kind == "info" else self.images).append(msg)

    def register(self, kind, timestamp, source, mount, samples, now):
        if not self.poses or not 0 <= now - timestamp <= 650_000_000:
            return None
        pose_time, pose = min(self.poses, key=lambda item: abs(item[0]-timestamp))
        if abs(pose_time-timestamp) > 60_000_000:
            return None
        cached = self.cache.get(kind)
        if cached and cached["stamp_ns"] == timestamp and cached["pose_stamp_ns"] == pose_time:
            return cached
        points = [[round(n, 3) for n in transform(pose, transform(mount, p))] for p in samples()]
        row = dict(source_frame=source, stamp_ns=timestamp, pose_stamp_ns=pose_time, points=points)
        self.cache[kind] = row
        return row

    def snapshot(self, now, scans):
        scene = dict(frame="odom", as_of_ns=now)
        infos = {stamp(info): info for info in self.infos}
        for msg in reversed(self.images):
            info = infos.get(stamp(msg))
            if info is None or (msg.width, msg.height) != (info.width, info.height):
                continue
            def pixels():
                read = depth_reader(msg)
                stride = max(1, math.ceil(math.sqrt(msg.width * msg.height / 900)))
                for v in range(stride//2, msg.height, stride):
                    for u in range(stride//2, msg.width, stride):
                        d = read(u, v)
                        if d is not None and d <= 7:
                            # An optical ray is right/down/forward; SDF mounts are forward/left/up.
                            yield (d, -(u-info.k[2])*d/info.k[0], -(v-info.k[5])*d/info.k[4])
            registered = self.register("depth", stamp(msg), self.optical_frame, self.camera_mount, pixels, now)
            if registered is not None:
                scene["depth"] = registered
                break
        for scan in reversed(scans):
            if scan["frame"] != self.laser_frame:
                continue
            def rays():
                step = max(1, math.ceil(len(scan["ranges"])/240))
                for index in range(0, len(scan["ranges"]), step):
                    distance = scan["ranges"][index]
                    if distance is not None and scan["range_min"] <= distance < scan["range_max"]:
                        az = scan["angle_min"] + index*scan["angle_increment"]
                        yield (distance*math.cos(az), distance*math.sin(az), 0)
            registered = self.register("laser", scan["stamp_ns"], self.laser_frame, self.laser_mount, rays, now)
            if registered is not None:
                scene["laser"] = registered
                break
        return scene
