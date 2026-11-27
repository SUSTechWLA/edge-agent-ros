"""SDD-022/023. Compare a Gazebo image against independent ROS pixels while a second lidar loses TF."""

import argparse
from collections import deque
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
import uuid
import zlib

from map_model_isolation import ROOT, check, publish, wait

SAMPLE_XYZ = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.75), (0.0, -1.5, 1.25)]
TRANSLATION = (0.75, -0.5, 0.375)


def ros_witness():
    """Publish official PointCloud2 field layout and independently observe Gazebo's original pixels."""
    from geometry_msgs.msg import TransformStamped
    import rclpy
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image, PointCloud2, PointField
    from tf2_ros import StaticTransformBroadcaster

    work = Path("/witness")
    rclpy.init()
    node = Node("independent_sensor_witness", parameter_overrides=[Parameter("use_sim_time", value=True)])
    publisher = node.create_publisher(PointCloud2, "/livox/lidar", qos_profile_sensor_data)
    fields = [
        PointField(name=name, offset=offset, datatype=kind, count=1)
        for name, offset, kind in (
            ("x", 0, 7),
            ("y", 4, 7),
            ("z", 8, 7),
            ("intensity", 12, 7),
            ("tag", 16, 2),
            ("line", 17, 2),
            ("timestamp", 18, 8),
        )
    ]
    sent = False
    broadcaster = StaticTransformBroadcaster(node)

    def lidar():
        nonlocal sent
        stamp = node.get_clock().now()
        if not stamp.nanoseconds:
            return
        if not sent and (work / "publish-transform").exists():
            tf = TransformStamped()
            tf.header.stamp = stamp.to_msg()
            tf.header.frame_id, tf.child_frame_id = "odom", "livox_frame"
            tf.transform.translation.x, tf.transform.translation.y, tf.transform.translation.z = TRANSLATION
            tf.transform.rotation.w = 1.0
            broadcaster.sendTransform(tf)
            sent = True
        cloud = PointCloud2()
        cloud.header.frame_id, cloud.header.stamp = "livox_frame", stamp.to_msg()
        cloud.width, cloud.height = len(SAMPLE_XYZ), 1
        cloud.fields = fields
        cloud.point_step = 26
        cloud.row_step = cloud.width * cloud.point_step
        cloud.is_dense = True
        cloud.data = b"".join(
            struct.pack("<ffffBBd", *xyz, 127.0, 16, index, float(stamp.nanoseconds + index))
            for index, xyz in enumerate(SAMPLE_XYZ)
        )
        publisher.publish(cloud)

    history = deque(maxlen=80)

    def observe(msg):
        if msg.encoding.lower() not in ("rgb8", "bgr8") or msg.step < 3 * msg.width:
            return
        src = memoryview(msg.data)
        pixels = bytearray(
            b"".join(src[y * msg.step : y * msg.step + msg.width * 3] for y in range(msg.height))
        )
        if msg.encoding.lower() == "bgr8":
            pixels[0::3], pixels[2::3] = pixels[2::3], pixels[0::3]
        history.append(
            {
                "stamp_ns": msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec,
                "frame_id": msg.header.frame_id,
                "width": msg.width,
                "height": msg.height,
                "pixel_sha256": sha256(pixels).hexdigest(),
            }
        )
        publish(work / "images.json", list(history))

    node.create_subscription(Image, "/edge/cameras/front/image", observe, qos_profile_sensor_data)
    node.create_timer(0.2, lidar)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


def preview_pixels(data):
    """Read an RGB8, non-interlaced PNG with checked chunks, independent of the camera encoder."""
    check(data.startswith(b"\x89PNG\r\n\x1a\n"), "invalid preview signature")
    pos = 8
    compressed = bytearray()
    while pos + 12 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, pos)
        chunk = data[pos + 8 : pos + 8 + size]
        (expected_crc,) = struct.unpack_from(">I", data, pos + 8 + size)
        check(zlib.crc32(kind + chunk) & 0xFFFFFFFF == expected_crc, "PNG checksum")
        if kind == b"IHDR":
            w, h, bits, color, cm, fm, interlace = struct.unpack(">IIBBBBB", chunk)
            check(
                (bits, color, cm, fm, interlace) == (8, 2, 0, 0, 0) and 0 < w <= 1920 and 0 < h <= 1080,
                "unexpected PNG format",
            )
        elif kind == b"IDAT":
            compressed += chunk
        elif kind == b"IEND":
            break
        pos += 12 + size
    packed = zlib.decompress(compressed)
    stride = w * 3
    check(len(packed) == h * (stride + 1), "invalid PNG rows")
    raw = bytearray(stride * h)
    for y in range(h):
        start = y * (stride + 1)
        f = packed[start]
        check(0 <= f <= 4, "unknown PNG filter")
        if f == 0:
            raw[y * stride : (y + 1) * stride] = packed[start + 1 : start + 1 + stride]
            continue
        for x in range(stride):
            i = y * stride + x
            a = raw[i - 3] if x >= 3 else 0
            b = raw[i - stride] if y else 0
            c = raw[i - stride - 3] if y and x >= 3 else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            paeth = a if pa <= pb and pa <= pc else b if pb <= pc else c
            raw[i] = (packed[start + 1 + x] + (a, b, (a + b) // 2, paeth)[f - 1]) % 256
    return w, h, raw


def run(args):
    project = "edge-sensor-audit-" + uuid.uuid4().hex[:10]
    report = {
        "passed": False,
        "project": project,
        "image": args.image,
        "core_image": args.core_image or args.image,
        "scope": "Gazebo RGB camera plus fixed multilayer lidar, synthetic Livox driver2 PointXYZRTLT via real ROS 2 publish/subscribe.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    direct = build_opener(ProxyHandler({}))

    def get(base, path):
        with direct.open(base + path, timeout=5) as response:
            return json.load(response)

    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        mounts = work / "inputs"
        mounts.mkdir()
        witness = work / "witness"
        witness.mkdir()
        probe = "/opt/edge/tests/system/sensor_plugin_isolation.py"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("EDGE_", "COMPOSE_", "DEEPSEEK_"))}
        env.update(
            EDGE_IMAGE=args.image,
            COMPOSE_DISABLE_ENV_FILE="1",
            EDGE_MAP3D_PORT="0",
            EDGE_CAMERA_PORT="0",
            EDGE_ROS_DOMAIN_ID="159",
            EDGE_MAP3D_INPUTS_DIR=str(mounts),
            EDGE_CAMERA_IMAGE=args.image,
        )
        core = ("sim", "bridge", "perception", "scene")
        services = {name: {"image": args.core_image} for name in core} if args.core_image else {}
        services["sensor-witness"] = {
            "image": args.image,
            "environment": {"ROS_DOMAIN_ID": "159", "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"},
            "command": ["python3", "-B", probe, "witness"],
            "volumes": [
                {
                    "type": "bind",
                    "source": str(Path(__file__).parent.resolve()),
                    "target": "/opt/edge/tests/system",
                    "read_only": True,
                },
                {"type": "bind", "source": str(witness), "target": "/witness"},
            ],
        }
        publish(work / "test.json", {"services": services})
        compose = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(ROOT / "compose/camera.yaml"),
            "-f",
            str(work / "test.json"),
        ]

        def dc(*args):
            return subprocess.check_output(
                [*compose, *args], cwd=ROOT, env=env, encoding="utf-8", errors="replace", timeout=180
            ).strip()

        def addr(name, port):
            a = dc("port", name, str(port))
            check(a.startswith("127.0.0.1:"), "expected local loopback")
            return "http://" + a

        def camera():
            s = get(cam, "/api/cameras")["sources"]
            return next(c for c in s if c["id"] == "front")

        def current():
            return get(maps, "/api/map")

        def same_pixels():
            with direct.open(cam + "/api/cameras/front/image", timeout=5) as img:
                data = img.read()
                check(img.headers.get_content_type() == "image/png", "wrong preview type")
                ns = int(img.headers["X-Frame-Stamp-Ns"])
                frame = img.headers["X-Frame-Id"]
            w, h, rgb = preview_pixels(data)
            digest = sha256(rgb).hexdigest()
            rows = json.loads((witness / "images.json").read_bytes())
            matched = next((r for r in rows if r["stamp_ns"] == ns and r["pixel_sha256"] == digest), None)
            if not matched:
                return None
            check(
                (w, h) == (matched["width"], matched["height"]) and frame == matched["frame_id"],
                "wrong camera frame",
            )
            colors = len(set(zip(rgb[0::3], rgb[1::3], rgb[2::3])))
            check(colors >= 8, "camera preview appears blank")
            (args.out.parent / "gazebo-camera-frame.png").write_bytes(data)
            return {**matched, "distinct_rgb": colors, "png_sha256": sha256(data).hexdigest()}

        try:
            dc(
                "up",
                "-d",
                "--no-build",
                *core,
                "map3d",
                "map3d-sensor",
                "camera",
                "camera-sensor",
                "sensor-witness",
            )
            cam, maps = addr("camera", 8771), addr("map3d", 8770)
            wait(lambda: camera()["state"] == "online", "Gazebo RGB camera online", 120)
            before_sensor = current()["cloud"]["stamp_s"] or 0.0
            wait(lambda: current()["cloud"]["status"] == "online", "Gazebo survey lidar online")
            ids = dc("ps", "-q", *core, "map3d", "camera").splitlines()

            def started():
                return subprocess.check_output(
                    ["docker", "inspect", *ids, "--format", "{{.Name}} {{.State.StartedAt}}"],
                    encoding="utf-8",
                ).splitlines()

            starts = started()
            report["camera"] = wait(
                same_pixels, "match decoded preview pixels to ROS camera at exactly the same stamp", 50
            )

            def calibration():
                info = camera().get("camera_info") or {}
                return (
                    info
                    if info.get("calibrated") is True and info.get("frame_id") == report["camera"]["frame_id"]
                    else None
                )

            report["gazebo_camera_info"] = wait(calibration, "matching live camera calibration")
            manifest = {
                "id": "mid360",
                "name": "Livox MID-360",
                "topic": "/livox/lidar",
                "type": "sensor_msgs/msg/PointCloud2",
                "expected_frame": "livox_frame",
                "ttl_s": 3,
            }
            publish(mounts / "mid360.json", manifest)

            def missing_tf():
                m = current()
                l = m.get("cloud_sources", {}).get("mid360", {})
                return (
                    l
                    if l.get("status") == "error" and not l["points"] and m["cloud"]["status"] == "online"
                    else None
                )

            broken = wait(missing_tf, "isolate unlocalized MID-360 points")
            report["missing_tf"] = broken.get("error")
            check(report["missing_tf"] == "transform_unavailable", "failure was unrelated to missing TF")
            check(camera()["state"] == "online", "missing lidar transform poisoned camera")
            (witness / "publish-transform").touch()

            def localized():
                m = current()
                l = m.get("cloud_sources", {}).get("mid360", {})
                return l if l.get("status") == "online" and m["cloud"]["status"] == "online" else None

            layer = wait(localized, "MID-360 uses newly supplied transform")
            actual = sorted(tuple(p) for p in layer["points"])
            expected = sorted(tuple(a + b for a, b in zip(sample, TRANSLATION)) for sample in SAMPLE_XYZ)
            check(
                actual == expected and layer["source_frame"] == "livox_frame",
                "unexpected transformed Livox points",
            )
            report["livox"] = {
                "frame": layer["source_frame"],
                "points_odom": actual,
                "topic": layer["topic"],
                "fields": ["x", "y", "z", "intensity", "tag", "line", "timestamp"],
                "point_step": 26,
            }
            report["camera_before_unplug"] = camera()["stamp_ns"]
            dc("stop", "camera-sensor")
            wait(lambda: camera()["state"] == "stale", "camera unavailability becomes visible")
            try:
                direct.open(cam + "/api/cameras/front/image", timeout=3).close()
            except HTTPError as e:
                check(e.code == 503, "stale camera returned different response")
            else:
                raise AssertionError("stale live view returned a frame")
            check(localized() and current()["cloud"]["stamp_s"] > before_sensor, "lidar stopped with camera")
            dc("start", "camera-sensor")
            report["camera_after_reconnect"] = wait(
                lambda: (
                    row["stamp_ns"]
                    if (row := camera())["state"] == "online"
                    and row["stamp_ns"] > report["camera_before_unplug"]
                    else None
                ),
                "new Gazebo camera frame",
            )
            (mounts / "mid360.json").unlink()
            wait(lambda: "mid360" not in current().get("cloud_sources", {}), "lidar source unplug")
            check(
                current()["cloud"]["status"] == "online" and camera()["state"] == "online",
                "source removal leaked across devices",
            )
            check(starts == started(), "core or observation provider restarted")
            report.update(
                passed=True,
                unchanged_processes=starts,
                isolated_stale_preview_status=503,
                image_id=subprocess.check_output(
                    ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], encoding="utf-8"
                ).strip(),
            )
        except Exception as ex:
            report["error"] = str(ex)[:350]
            report["diagnostic"] = dc(
                "logs", "--no-color", "--tail", "10", "map3d", "camera", "camera-sensor", "sensor-witness"
            )[-4500:]
            raise
        finally:
            try:
                dc("--profile", "*", "down", "--volumes", "--remove-orphans", "--timeout", "4")
            finally:
                args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] == ["witness"]:
        ros_witness()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--image", required=True)
        parser.add_argument("--core-image")
        parser.add_argument("--out", required=True, type=Path)
        run(parser.parse_args())
