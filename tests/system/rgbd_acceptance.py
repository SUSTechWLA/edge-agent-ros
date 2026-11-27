"""Read real Gazebo depth/color through ROS and compare the same calibrated pixel through the RGB-D API."""

import argparse
from collections import deque
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener
import uuid

from map_model_isolation import ROOT, check, copy_shipped_contracts, publish, wait

TOPIC = "/edge/rgbd/"
CLOUD = "/camera/camera/depth/color/points"
WORLD_POINTS = ((1.0, 0.5, 1.375), (2.0, -0.25, 2.625))
PACKED_COLORS = (0xFF4010, 0x4080EF)


def sensor_witness():
    from geometry_msgs.msg import TransformStamped
    import rclpy
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.time import Time
    from sensor_msgs.msg import Image, CameraInfo, PointCloud2, PointField
    from tf2_ros import StaticTransformBroadcaster

    root = Path("/witness")
    rclpy.init()
    node = Node("independent_rgbd_witness", parameter_overrides=[Parameter("use_sim_time", value=True)])
    node.create_timer(0.5, lambda: publish(root / "clock.json", node.get_clock().now().nanoseconds))
    history = {key: deque(maxlen=24) for key in ("color", "depth")}
    sensor_frame = ""

    def collect(msg, kind):
        nonlocal sensor_frame
        if (root / "mode").exists():
            return
        enc = msg.encoding.lower()
        if kind == "color" and enc not in ("rgb8", "bgr8") or kind == "depth" and enc != "32fc1":
            return
        sensor_frame = msg.header.frame_id
        samples = {}
        buf = memoryview(msg.data)
        order = ">" if msg.is_bigendian else "<"
        for nx in (1, 2, 4, 5, 7):
            for ny in (1, 3, 4, 5, 7):
                x, y = msg.width * nx // 8, msg.height * ny // 8
                offset = y * msg.step + x * (4 if kind == "depth" else 3)
                if kind == "depth":
                    v = struct.unpack_from(order + "f", buf, offset)[0]
                    if not math.isfinite(v) or v <= 0:
                        continue
                else:
                    v = list(buf[offset : offset + 3])
                    if enc == "bgr8":
                        v.reverse()
                samples[f"{x},{y}"] = v
        history[kind].append(
            {
                "stamp": msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec,
                "frame": msg.header.frame_id,
                "samples": samples,
            }
        )
        publish(root / (kind + ".json"), list(history[kind]))

    paths = ("color/image_raw", "aligned_depth_to_color/image_raw", "color/camera_info")
    node.create_subscription(Image, TOPIC + paths[0], lambda m: collect(m, "color"), qos_profile_sensor_data)
    node.create_subscription(Image, TOPIC + paths[1], lambda m: collect(m, "depth"), qos_profile_sensor_data)

    def observe_cloud(msg):
        fields = {field.name: field.offset for field in msg.fields}
        if not {"x", "y", "z", "rgb"} <= fields.keys() or (root / "mode").exists():
            return
        u, v = msg.width // 2, msg.height // 2
        order = ">" if msg.is_bigendian else "<"

        def pixel(x, y):
            offset = y * msg.row_step + x * msg.point_step
            coords = [struct.unpack_from(order + "f", msg.data, offset + fields[axis])[0] for axis in "xyz"]
            rgb = struct.unpack_from(order + "I", msg.data, offset + fields["rgb"])[0] & 0xFFFFFF
            return coords, rgb

        xyz, rgb = pixel(u, v)
        if not all(map(math.isfinite, xyz)):
            return
        colors = {pixel(x, y)[1] for y in range(0, msg.height, 16) for x in range(0, msg.width, 16)}
        publish(
            root / "cloud.json",
            {
                "frame": msg.header.frame_id,
                "stamp_ns": msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec,
                "size": [msg.width, msg.height],
                "center_xyz": xyz,
                "center_rgb": rgb,
                "color_count": len(colors),
            },
        )

    node.create_subscription(PointCloud2, TOPIC + "points", observe_cloud, qos_profile_sensor_data)
    rgb_pub, depth_pub, info_pub = [
        node.create_publisher(cls, TOPIC + path, qos_profile_sensor_data)
        for cls, path in zip((Image, Image, CameraInfo), paths)
    ]
    cloud_pub = node.create_publisher(PointCloud2, CLOUD, qos_profile_sensor_data)
    tf_pub = StaticTransformBroadcaster(node)

    def synthetic():
        if not (root / "mode").exists() or not sensor_frame:
            return
        mode = (root / "mode").read_text().strip()
        if not mode:
            return
        now = node.get_clock().now()
        color = Image()
        color.header.stamp = now.to_msg()
        color.header.frame_id = sensor_frame
        color.width, color.height, color.encoding, color.step = 4, 3, "rgb8", 12
        color.data = bytes((64, 128, 192)) * 12
        depth = Image()
        depth.header.frame_id = sensor_frame if mode != "wrong-frame" else "unregistered_optical"
        depth.header.stamp = Time(
            nanoseconds=now.nanoseconds - (100_000_000 if mode == "skew" else 0)
        ).to_msg()
        depth.width, depth.height, depth.encoding, depth.step = 4, 3, "16UC1", 8
        depth.data = struct.pack("<12H", 0, 777, 1250, 2333, 4096, 5024, 6000, 1200, 1200, 1200, 1200, 1200)
        if mode == "malformed":
            depth.data = depth.data[:-2]
        info = CameraInfo()
        info.header = color.header
        info.width, info.height = 4, 3
        info.k = [4.0, 0.0, 2.0, 0.0, 4.0, 1.5, 0.0, 0.0, 1.0]
        info.p = [4.0, 0.0, 2.0, 0.0, 0.0, 4.0, 1.5, 0.0, 0.0, 0.0, 1.0, 0.0]
        info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        info.distortion_model = "plumb_bob"
        info.d = [0.0] * 5
        rgb_pub.publish(color)
        depth_pub.publish(depth)
        info_pub.publish(info)
        # A separately published textured cloud from a simulated driver, not RGB-D app inference.
        tf = TransformStamped()
        tf.header.stamp = now.to_msg()
        tf.header.frame_id = "odom"
        tf.child_frame_id = "fixture_cloud_frame"
        tf.transform.rotation.w = 1.0
        tf_pub.sendTransform(tf)
        cloud = PointCloud2()
        cloud.header.frame_id = tf.child_frame_id
        cloud.header.stamp = now.to_msg()
        cloud.width, cloud.height, cloud.point_step, cloud.row_step = 2, 1, 16, 32
        cloud.fields = [
            PointField(name=k, offset=n * 4, datatype=PointField.FLOAT32, count=1)
            for n, k in enumerate(("x", "y", "z", "rgb"))
        ]
        cloud.data = b"".join(
            struct.pack("<fffI", *xyz, packed) for xyz, packed in zip(WORLD_POINTS, PACKED_COLORS)
        )
        cloud_pub.publish(cloud)

    node.create_timer(0.35, synthetic)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


def run(args):
    work_dir = args.out.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    project = "edge-rgbd-audit-" + uuid.uuid4().hex[:10]
    direct = build_opener(ProxyHandler({}))
    report = {
        "passed": False,
        "project": project,
        "image": args.image,
        "core_image": args.core_image or args.image,
    }

    def get(base, path, raw=False):
        with direct.open(base + path, timeout=5) as response:
            return response.read() if raw else json.load(response)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        inputs = tmp / "inputs"
        inputs.mkdir()
        witness = tmp / "witness"
        witness.mkdir()
        copy_shipped_contracts(tmp / "contracts")
        (tmp / "plugins").mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("EDGE_", "COMPOSE_", "DEEPSEEK_"))}
        env.update(
            COMPOSE_DISABLE_ENV_FILE="1",
            EDGE_IMAGE=args.image,
            EDGE_ROS_DOMAIN_ID="157",
            EDGE_CAMERA_PORT="0",
            EDGE_RGBD_PORT="0",
            EDGE_CONSOLE_PORT="0",
            EDGE_MAP3D_PORT="0",
            EDGE_MAP3D_INPUTS_DIR=str(inputs),
            EDGE_CONTRACTS_DIR=str(tmp / "contracts"),
            EDGE_PLUGINS_DIR=str(tmp / "plugins"),
            EDGE_EXTRA_PLUGIN_DIR="",
        )
        core = ("sim", "bridge", "core", "perception", "scene", "agent", "console", "gateway")
        services = {name: {"image": args.core_image} for name in core} if args.core_image else {}
        services["witness-rgbd"] = {
            "image": args.image,
            "environment": {"ROS_DOMAIN_ID": "157", "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"},
            "command": ["python3", "-B", "/opt/edge/tests/system/rgbd_acceptance.py", "witness"],
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
        publish(tmp / "test.json", {"services": services})
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
            str(ROOT / "compose/rgbd.yaml"),
            "-f",
            str(ROOT / "compose/rgbd-sensor.yaml"),
            "-f",
            str(tmp / "test.json"),
        ]

        def dc(*args):
            return subprocess.check_output(
                [*compose, *args], cwd=ROOT, env=env, encoding="utf-8", errors="replace", timeout=180
            ).strip()

        def addr(name, port):
            result = dc("port", name, str(port))
            check(result.startswith("127.0.0.1:"), "expected local port")
            return "http://" + result

        def meta():
            return get(base, "/api/rgbd")

        def sample(key, x, y):
            return get(base, "/api/sample?" + urlencode({"pair": key, "u": x, "v": y}))

        def error_sample(key, x, y):
            try:
                sample(key, x, y)
                return None
            except HTTPError as ex:
                row = json.load(ex)
                check("depth_m" not in row, "bad request returned a depth")
                return ex.code, row.get("reason", row.get("error"))

        def compare():
            data = meta()
            if data["status"] != "online":
                return None
            pair = data["pair_id"]
            depth = next(
                (
                    r
                    for r in json.loads((witness / "depth.json").read_bytes())
                    if r["stamp"] == data["depth_stamp_ns"]
                ),
                None,
            )
            color = next(
                (
                    r
                    for r in json.loads((witness / "color.json").read_bytes())
                    if r["stamp"] == data["color_stamp_ns"]
                ),
                None,
            )
            if not depth or not color:
                return None
            # Gazebo initializes render targets before it has drawn world materials.
            if len({tuple(rgb) for rgb in color["samples"].values()}) < 3:
                return None
            measurements = sorted(
                (z, *map(int, xy.split(",")), color["samples"][xy])
                for xy, z in depth["samples"].items()
                if xy in color["samples"]
            )
            if len(measurements) < 2 or measurements[-1][0] - measurements[0][0] < 0.05:
                return None
            results = []
            try:
                center = next(
                    (m for m in measurements if m[1:3] == (data["width"] // 2, data["height"] // 2)), None
                )
                for z, x, y, rgb in (
                    measurements[0],
                    measurements[-1],
                    *([center] if center and center not in (measurements[0], measurements[-1]) else []),
                ):
                    obs = sample(pair, x, y)
                    check(
                        abs(obs["depth_m"] - z) < 0.001 and obs["rgb"] == rgb and obs["pair_id"] == pair,
                        "wrong pixel value, depth units or identity",
                    )
                    results.append({"xy": [x, y], "ROS_Z_m": z, "API_Z_m": obs["depth_m"], "rgb": rgb})
                pictures = {}
                for name, ext in (("color", "png"), ("depth", "png")):
                    raw = get(base, f"/frame/{name}.{ext}?" + urlencode({"pair": pair}), raw=True)
                    check(raw.startswith(b"\x89PNG\r\n\x1a\n"), "invalid published image")
                    path = work_dir / ("gazebo-rgbd-" + name + "." + ext)
                    path.write_bytes(raw)
                    pictures[name] = sha256(raw).hexdigest()
            except HTTPError as ex:
                if ex.code == 409:
                    return None
                raise
            return {
                "pair_id": pair,
                "color_stamp_ns": color["stamp"],
                "depth_stamp_ns": depth["stamp"],
                "depth_frame": depth["frame"],
                "image_size": [data["width"], data["height"]],
                "sampled": results,
                "image_sha256": pictures,
            }

        def set_mode(mode):
            stage = witness / "mode.tmp"
            stage.write_text(mode, encoding="utf-8")
            stage.replace(witness / "mode")

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
                "rgbd",
                "rgbd-sensor",
                "witness-rgbd",
            )
            base, cam, mp, console = (
                addr("rgbd", 8772),
                addr("camera", 8771),
                addr("map3d", 8770),
                addr("console", 8080),
            )
            first = wait(compare, "same actual Gazebo optical image, valid aligned depth and colors", 120)
            report["gazebo"] = first
            center = next(s for s in first["sampled"] if s["xy"] == [160, 120])
            # Station (-1.4, -1, 1.1) faces +X at the near face of the south door wall (x=1.8-0.18/2).
            expected = 1.8 - 0.18 / 2 - (-1.4)
            check(abs(center["ROS_Z_m"] - expected) < 0.035, "center ray misses known south wall geometry")

            def textured():
                raw = json.loads((witness / "cloud.json").read_bytes())
                return raw if raw["color_count"] > 2 else None

            raw_cloud = wait(textured, "real Gazebo colored cloud after the renderer warms up")
            check(
                raw_cloud["size"] == [320, 240]
                and raw_cloud["frame"] == "edge_rgbd_link"
                and raw_cloud["color_count"] > 2,
                "simulation cloud must be organized, textured, and use Gazebo body axes",
            )
            check(
                abs(raw_cloud["center_xyz"][0] - center["ROS_Z_m"]) < 0.035,
                "cloud forward range disagrees with optical Z",
            )
            target = (
                raw_cloud["center_xyz"][0] - 1.4,
                raw_cloud["center_xyz"][1] - 1.0,
                raw_cloud["center_xyz"][2] + 1.1,
            )
            profile = ROOT / "perception/rgbd/profiles/gazebo-rgbd-points.json"
            publish(inputs / profile.name, json.loads(profile.read_bytes()))

            def mapped_center():
                m = get(mp, "/api/map")
                s = m.get("cloud_sources", {}).get("gazebo-rgbd-points", {})
                if s.get("status") != "online":
                    return None
                nearest = min(s["points"], key=lambda p: math.dist(target, p))
                return (
                    {"target": target, "nearest_voxel": nearest, "distance_m": math.dist(target, nearest)}
                    if math.dist(target, nearest) < 0.2
                    else None
                )

            mapped = wait(mapped_center, "genuine colored Gazebo cloud transformed through TF into map")
            report["gazebo"]["center_expected_m"] = expected
            report["gazebo"]["organized_cloud"] = {**raw_cloud, "map_alignment": mapped}
            browser = work_dir / "rgbd-browser.json"
            subprocess.run(
                ["node", str(ROOT / "tests/system/console_browser.mjs"), base, str(browser), "--rgbd"],
                cwd=ROOT,
                env=env,
                timeout=90,
                check=True,
            )
            report["browser"] = json.loads(browser.read_bytes())
            (inputs / profile.name).unlink()
            wait(
                lambda: "gazebo-rgbd-points" not in get(mp, "/api/map").get("cloud_sources", {}),
                "remove only Gazebo RGB-D cloud",
            )
            selected = dc("ps", "-q", *core, "map3d", "camera").splitlines()

            def unchanged():
                return subprocess.check_output(
                    ["docker", "inspect", *selected, "--format", "{{.Name}} {{.State.StartedAt}}"],
                    encoding="utf-8",
                ).splitlines()

            start = unchanged()

            def observations():
                camera = next(s for s in get(cam, "/api/cameras")["sources"] if s["id"] == "front")
                lidar = get(mp, "/api/map")["cloud"]
                data = meta()
                return {
                    "clock": [True, json.loads((witness / "clock.json").read_bytes())],
                    "camera-sensor": [camera["state"] == "online", camera["stamp_ns"]],
                    "map3d-sensor": [lidar["status"] == "online", lidar.get("stamp_s")],
                    "rgbd-sensor": [data["status"] == "online", data.get("depth_stamp_ns")],
                }

            def all_online():
                sample = observations()
                return sample if all(v[0] and v[1] for v in sample.values()) else None

            report["station_cycles"] = []
            for iteration in range(2):
                for station in ("camera-sensor", "map3d-sensor", "rgbd-sensor"):
                    before_cycle = wait(all_online, "all three physical simulations emit new observations")
                    dc("stop", station)

                    def isolated():
                        current = observations()
                        others = [k for k in current if k != station]
                        if current[station][0] or not all(
                            current[k][0] and current[k][1] > before_cycle[k][1] for k in others
                        ):
                            return None
                        return current

                    during = wait(
                        isolated, station + ": clock and both other sensors advance after unplug", 18
                    )
                    dc("start", station)

                    def resumed():
                        current = all_online()
                        return current if current and current[station][1] > before_cycle[station][1] else None

                    renewed = wait(resumed, station + " resumes with a new timestamp", 30)
                    report["station_cycles"].append(
                        {
                            "iteration": iteration + 1,
                            "stopped": station,
                            "before": before_cycle,
                            "while_unplugged": during,
                            "resumed": renewed,
                        }
                    )
            dc("stop", "rgbd-sensor")
            wait(lambda: meta()["status"] != "online", "three streams stale")
            set_mode("matched")

            def mm():
                d = meta()
                if d["status"] != "online" or d["width"] != 4:
                    return None
                good = sample(d["pair_id"], 2, 0)
                return (
                    {**good, "invalid": error_sample(d["pair_id"], 0, 0)} if good["depth_m"] == 1.25 else None
                )

            mm_result = wait(mm, "aligned 16UC1 in millimetres")
            check(
                mm_result["invalid"][0] == 422 and mm_result["rgb"] == [64, 128, 192],
                "invalid/valid RGB-D pixels incorrectly handled",
            )
            report["millimetre_fixture"] = mm_result
            problems = {}
            for mode, reason in (
                ("skew", "timestamp_skew"),
                ("wrong-frame", "frame_mismatch"),
                ("malformed", "depth_layout"),
            ):
                set_mode(mode)

                def invalid():
                    d = meta()
                    if d["status"] != "invalid" or d.get("reason") != reason:
                        return None
                    rejected = error_sample(mm_result["pair_id"], 2, 0)
                    return (
                        {"status": d["status"], "reason": d.get("reason"), "http": rejected[0]}
                        if rejected and rejected[0] == 503
                        else None
                    )

                problems[mode] = wait(invalid, mode + " fails closed")
                set_mode("matched")
                wait(mm, mode + " recovers")
            report["rejections"] = problems
            publish(
                inputs / "d4xx.json",
                {
                    "id": "d4xx",
                    "name": "D4xx driver cloud",
                    "type": "sensor_msgs/msg/PointCloud2",
                    "topic": CLOUD,
                    "expected_frame": "fixture_cloud_frame",
                    "ttl_s": 3,
                },
            )

            def cloud_active():
                m = get(mp, "/api/map")
                s = m.get("cloud_sources", {}).get("d4xx", {})
                return s if s.get("status") == "online" and m["cloud"]["status"] == "online" else None

            cloud = wait(cloud_active, "independent textured driver cloud can be attached")
            check(sorted(map(tuple, cloud["points"])) == sorted(WORLD_POINTS), "driver cloud XYZ lost")
            report["driver_cloud"] = {
                "topic": cloud["topic"],
                "ros_packed_colors": PACKED_COLORS,
                "map_xyz": cloud["points"],
                "map_colors": cloud.get("colors"),
            }

            def activity():
                camera = next(s for s in get(cam, "/api/cameras")["sources"] if s["id"] == "front")
                layers = get(mp, "/api/map")
                g = get(console, "/api/state")["sources"]["map"]
                if not (
                    g["fresh"]
                    and layers["cloud"]["status"] == "online"
                    and camera["state"] == "online"
                    and layers["cloud_sources"]["d4xx"]["status"] == "online"
                ):
                    return None
                return [
                    g["stamp_s"],
                    layers["cloud"]["stamp_s"],
                    camera["stamp_ns"],
                    layers["cloud_sources"]["d4xx"]["stamp_s"],
                ]

            before = wait(activity, "timestamps before stopping RGB-D")
            dc("stop", "rgbd")

            def independent():
                after = activity()
                return after if after and all(b > a for a, b in zip(before, after)) else None

            after = wait(independent, "2D, both clouds and independent RGB advance after RGB-D stops")
            report["timestamps_before_after"] = {
                "order": ["2d_map_s", "primary_cloud_s", "camera_ns", "driver_cloud_s"],
                "before": before,
                "after": after,
            }
            check(unchanged() == start, "core or independent plugins were replaced")
            report.update(passed=True, unchanged=start)
        except Exception as err:
            report["error"] = str(err)[:320]
            report["components"] = dc("ps", "-a", "--format", "table {{.Service}} {{.Status}}")
            report["diagnostic"] = {
                name: dc("logs", "--no-color", "--tail", "8", name)[-1600:]
                for name in ("sim", "rgbd", "rgbd-sensor", "witness-rgbd", "map3d")
            }
            raise
        finally:
            try:
                dc("--profile", "*", "down", "--volumes", "--remove-orphans", "--timeout", "4")
            finally:
                args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] == ["witness"]:
        sensor_witness()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--image", required=True)
        parser.add_argument("--core-image")
        parser.add_argument("--out", required=True, type=Path)
        run(parser.parse_args())
