"""Exercise the optional 3D map and model adapter, separately and together, on an isolated Gazebo robot."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import product
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[2]
CORE = ("sim", "bridge", "witness", "gateway", "core", "perception", "scene", "agent", "console")
GOAL = "Read the current odometry and report the robot's position and heading, without moving."


def check(ok, reason):
    if not ok:
        raise AssertionError(reason)


def publish(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value), encoding="utf-8")
    temp.replace(path)


def copy_shipped_contracts(target):
    """Start with versioned contracts, never import an operator's profile or deployment history."""
    files = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "-z", "HEAD", "contracts"], cwd=ROOT
    )
    for name in files.decode("utf-8").split("\0"):
        if not name:
            continue
        destination = target / Path(name).relative_to("contracts")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(subprocess.check_output(["git", "show", "HEAD:" + name], cwd=ROOT))


def wait(read, what, timeout=75):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = read()
            if value:
                return value
        except (URLError, OSError, ValueError) as exc:
            last = type(exc).__name__
        time.sleep(0.25)
    raise TimeoutError(f"{what}; last transport error={last}")


class LocalModel(BaseHTTPRequestHandler):
    """Stand-in only for the model server. Record the upstream alias and counts, never request bodies."""

    def log_message(self, *_):
        pass

    def reply(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/witness":
            return self.reply(self.server.models)
        if self.path == "/api/tags":
            return self.reply({"models": [{"name": name} for name in ("fixture-a", "fixture-b")]})
        self.send_error(404)

    def do_POST(self):
        if self.path != "/api/chat":
            return self.send_error(404)
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if msg.get("model") not in ("fixture-a", "fixture-b") or self.headers.get("Authorization"):
            return self.send_error(400)
        self.server.models.append(msg["model"])
        result = {"steps": [{"tool": "status", "args": {}}], "reason": "Read live odometry"}
        self.reply(
            {
                "model": msg["model"],
                "message": {"role": "assistant", "content": json.dumps(result)},
                "done": True,
                "done_reason": "stop",
                "eval_count": 26,
                "prompt_eval_count": 180,
            }
        )


def raw_cloud():
    """An independent ROS subscriber operating in the existing image. Validate finite source points."""
    import rclpy
    from sensor_msgs.msg import PointCloud2
    from rclpy.qos import qos_profile_sensor_data

    rclpy.init()
    node = rclpy.create_node("map_plugin_acceptance_witness")
    observed = []
    node.create_subscription(PointCloud2, "/edge/map3d/points", observed.append, qos_profile_sensor_data)
    deadline = time.monotonic() + 20
    try:
        while not observed and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.35)
        check(observed, "ROS witness received no cloud")
        msg = observed[0]
        fields = {f.name: f for f in msg.fields}
        check(all(fields[k].datatype == 7 for k in "xyz"), "expected float32 XYZ from Gazebo")
        points = []
        for y in range(msg.height):
            for x in range(msg.width):
                base = y * msg.row_step + x * msg.point_step
                point = [
                    struct.unpack_from(
                        (">" if msg.is_bigendian else "<") + "f", msg.data, base + fields[k].offset
                    )[0]
                    for k in "xyz"
                ]
                if all(math.isfinite(c) for c in point):
                    points.append(point)
        print(
            json.dumps(
                {
                    "frame": msg.header.frame_id,
                    "points": points,
                    "width": msg.width,
                    "height": msg.height,
                    "stamp": [msg.header.stamp.sec, msg.header.stamp.nanosec],
                }
            )
        )
    finally:
        node.destroy_node()
        rclpy.shutdown()


def compare_cloud(raw, cloud):
    check(raw["frame"] == cloud["source_frame"] == "map3d_survey", "unexpected fixed station frame")
    size = cloud["voxel_m"]
    check(
        0 < size <= 0.5 and all(len(p) == 3 and all(map(math.isfinite, p)) for p in cloud["points"]),
        "invalid voxel or map coordinates",
    )
    # Independent survey installation is at (0,0,1.2) in odom. Check source echoes at several heights.
    mapped = {tuple(math.floor(v / size) for v in p) for p in cloud["points"]}
    offsets = tuple(product((-1, 0, 1), repeat=3))
    matched_z = []
    for x, y, z in raw["points"]:
        world = (x, y, z + 1.2)
        key = tuple(math.floor(v / size) for v in world)
        if any(tuple(a + b for a, b in zip(key, off)) in mapped for off in offsets):
            matched_z.append(world[2])
    check(
        len(matched_z) >= 20 and max(matched_z) - min(matched_z) > 0.6,
        "insufficient independent echoes agree across multiple heights",
    )
    return {
        "raw_size": [raw["width"], raw["height"]],
        "finite_source_returns": len(raw["points"]),
        "matched_within_one_voxel_per_axis": len(matched_z),
        "matched_min_max_z_m": [min(matched_z), max(matched_z)],
        "voxel_m": size,
        "cloud_points": len(cloud["points"]),
        "source_frame": raw["frame"],
        "witness_ros_stamp": raw["stamp"],
    }


def run(args):
    started = datetime.now(timezone.utc).isoformat()
    project = "edge-mapisolation-" + uuid.uuid4().hex[:10]
    direct = build_opener(ProxyHandler({}))

    def http(base, path, body=None):
        req = Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with direct.open(req, timeout=4) as response:
            return json.load(response)

    report = {
        "passed": False,
        "project": project,
        "image": args.image,
        "started_utc": started,
        "scope": "Real Gazebo/ROS map, real Agent and adapter, deterministic local Ollama fixture; no cloud key.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        contracts = work / "contracts"
        copy_shipped_contracts(contracts)
        (work / "routes").mkdir()
        (work / "plugins").mkdir()

        def route(name):
            publish(
                work / "routes/routes.json",
                {"schema_version": 1, "routes": {"qwen2.5:1.5b": {"provider": "ollama", "model": name}}},
            )

        route("fixture-a")
        publish(
            contracts / "planner-profile.json",
            {
                "schema_version": 1,
                "version": "isolation",
                "protocol": "ollama",
                "url": "http://model-provider:8134",
                "model": "qwen2.5:1.5b",
            },
        )
        probe_path = "/opt/edge/tests/system/map_model_isolation.py"
        mounted = {
            "type": "bind",
            "source": str(Path(__file__).resolve()),
            "target": probe_path,
            "read_only": True,
        }
        services = {name: {"image": args.core_image} for name in CORE} if args.core_image else {}
        services.update(
            {
                "model-fixture": {
                    "image": args.image,
                    "entrypoint": ["python3", "-B", probe_path, "fixture"],
                    "ports": ["127.0.0.1:0:11434"],
                    "read_only": True,
                    "cap_drop": ["ALL"],
                    "volumes": [mounted],
                },
                "probe": {"volumes": [mounted]},
            }
        )
        publish(work / "test.json", {"services": services})
        env = {k: v for k, v in os.environ.items() if not k.startswith(("EDGE_", "COMPOSE_", "DEEPSEEK_"))}
        env.update(
            COMPOSE_DISABLE_ENV_FILE="1",
            EDGE_IMAGE=args.image,
            EDGE_MODEL_ADAPTER_IMAGE=args.image,
            EDGE_MODEL_PORT="0",
            EDGE_MAP3D_PORT="0",
            EDGE_MODEL_LOCAL_URL="http://model-fixture:11434",
            EDGE_MODEL_ROUTES_DIR=str(work / "routes"),
            EDGE_PLUGINS_DIR=str(work / "plugins"),
            EDGE_CONTRACTS_DIR=str(contracts),
            EDGE_CONSOLE_PORT="0",
            EDGE_ROS_DOMAIN_ID="162",
            EDGE_EXTRA_PLUGIN_DIR="",
        )
        compose = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(ROOT / "compose/model-provider.yaml"),
            "-f",
            str(work / "test.json"),
        ]

        def command(argv, timeout=180):
            return subprocess.check_output(
                argv, cwd=ROOT, env=env, encoding="utf-8", errors="replace", timeout=timeout
            ).strip()

        def dc(*args):
            return command([*compose, *args])

        def address(service, port):
            host = dc("port", service, str(port))
            check(host.startswith("127.0.0.1:"), "expected isolated loopback HTTP port")
            return "http://" + host

        def identities():
            ids = dc("ps", "-q", *CORE).splitlines()
            check(len(ids) == len(CORE), "missing baseline service")
            data = json.loads(command(["docker", "inspect", *ids]))
            return {
                c["Config"]["Labels"]["com.docker.compose.service"]: [c["Id"][:12], c["State"]["StartedAt"]]
                for c in data
            }

        def task(key, model):
            payload = {"text": f"{GOAL} Reference {key}." if model else "status", "id": key}
            http(console, "/agent/tasks", payload)

            def final():
                row = http(console, "/agent/tasks?id=" + key)
                return row if row["state"] in ("SUCCEEDED", "BLOCKED", "RECOVERY_REQUIRED") else None

            row = wait(final, key)
            check(
                row["state"] == "SUCCEEDED"
                and len(row["steps"]) == 1
                and row["steps"][0]["tool"] == "status",
                key + " failed: " + str(row.get("detail", ""))[:150],
            )
            step = row["steps"][0]
            evidence = step["evidence"]
            check(
                evidence["source"] == "/odom"
                and evidence["stamp_ns"] > 0
                and (row["provider"].startswith("ollama/") if model else row["provider"] == "local"),
                "wrong operation",
            )
            result = {
                "task": key,
                "provider": row["provider"],
                "stamp_ns": evidence["stamp_ns"],
                "operation": step["operation_id"],
            }
            if model:
                event = next(e for e in row["events"] if e["kind"] == "model.request")
                result["request_sha256"] = json.loads(event["payload"])["trace"]["artifact"]["sha256"]
                reply = next(e for e in row["events"] if e["kind"] == "model.response")
                result["adapter_receipt"] = json.loads(reply["payload"])["trace"]["artifact"]["content"][
                    "adapter"
                ]["event_id"]
            return result

        def grid():
            src = http(console, "/api/state")["sources"]["map"]
            return src["stamp_s"] if src["fresh"] and 100 in src["data"]["cells"] else None

        def scene_online():
            row = http(map_url, "/api/map")
            return (
                row
                if (
                    row["cloud"]["status"] == "online"
                    and row["scene"]["status"] == "online"
                    and row["pose"]["status"] == "online"
                    and row["cloud"]["points"]
                )
                else None
            )

        try:
            dc("up", "-d", "--no-build", *CORE, "model-fixture")
            console = address("console", 8080)
            upstream = address("model-fixture", 11434)
            report["initial_2d_grid_stamp"] = wait(grid, "baseline observed 2D grid", 100)
            original = identities()
            report["local_without_plugins"] = task("baseline", False)
            dc("up", "-d", "--no-build", "map3d")
            map_url = address("map3d", 8770)

            def waiting():
                m = http(map_url, "/api/map")
                return m if m["cloud"]["status"] != "online" and not m["cloud"]["points"] else None

            report["without_sensor"] = wait(waiting, "no invented points")["cloud"]["status"]
            dc("up", "-d", "--no-build", "map3d-sensor", "model-provider")
            first = wait(scene_online, "Gazebo survey cloud, semantic scene and pose", 120)
            check(first["frame"] == "odom", "expected map frame odom")
            adapter = address("model-provider", 8134)
            try:
                http(
                    adapter,
                    "/api/chat",
                    {
                        "model": "missing-test-route",
                        "stream": False,
                        "messages": [{"role": "user", "content": "Synthetic diagnostic"}],
                    },
                )
            except HTTPError as ex:
                check(
                    ex.code == 404 and json.load(ex)["error"] == "model.route_missing",
                    "wrong adapter rejection",
                )
            else:
                raise AssertionError("unknown model route accepted")
            raw = json.loads(
                dc("run", "--rm", "-T", "--no-deps", "probe", "python3", "-B", probe_path, "raw")
            )
            report["raw_and_map"] = compare_cloud(raw, scene_online()["cloud"])
            report["model_with_map"] = task("with-map", True)
            browser = args.out.with_name(args.out.stem + "-browser.json")
            command(
                ["node", str(ROOT / "tests/system/console_browser.mjs"), map_url, str(browser), "--map3d"],
                timeout=100,
            )
            report["browser"] = json.loads(browser.read_bytes())
            dc("stop", "map3d-sensor")

            def stale():
                row = http(map_url, "/api/map")
                return (
                    row if row["cloud"]["status"] == "stale" and row["pose"]["status"] == "online" else None
                )

            report["after_sensor_stopped"] = wait(stale, "survey timeout only")["cloud"]["status"]
            report["model_without_sensor"] = task("without-sensor", True)
            dc("start", "map3d-sensor")
            new = wait(scene_online, "survey restart", 100)
            check(new["cloud"]["stamp_s"] > first["cloud"]["stamp_s"], "old cloud after restart")
            dc("stop", "model-provider")
            a = new["cloud"]["stamp_s"]
            report["map_during_model_outage_stamp"] = wait(
                lambda: (v if (v := http(map_url, "/api/map")["cloud"]["stamp_s"]) > a else None),
                "continuing cloud",
            )
            report["local_without_model"] = task("without-model", False)
            route("fixture-b")
            dc("start", "model-provider")
            dc("stop", "map3d")
            report["model_without_map"] = task("without-map", True)
            route("fixture-a")
            report["model_after_hot_route"] = task("changed-route", True)
            report["upstream_witness"] = http(upstream, "/witness")
            check(
                report["upstream_witness"] == ["fixture-a", "fixture-a", "fixture-b", "fixture-a"],
                "unexpected model calls or stale route",
            )

            def events(module, kind):
                return http(console, f"/api/events?source=journal:{module}&kind={kind}&limit=100")["events"]

            tasks = [
                report[key]
                for key in (
                    "model_with_map",
                    "model_without_sensor",
                    "model_without_map",
                    "model_after_hot_route",
                )
            ]

            def receipts():
                rows = events("model.provider", "model.provider.completed")
                by_id = {r["event_id"]: r for r in rows}
                return (
                    [by_id[t["adapter_receipt"]] for t in tasks]
                    if all(t["adapter_receipt"] in by_id for t in tasks)
                    else None
                )

            received = wait(receipts, "four Agent requests matched by digest to archived model receipts")
            starts = {e["event_id"]: e for e in events("model.provider", "model.provider.requested")}
            for task_result, row in zip(tasks, received):
                check(
                    starts[row["parent_event_id"]]["data"]["request_sha256"]
                    == row["data"]["request_sha256"]
                    == task_result["request_sha256"],
                    "wrong model causal link",
                )
            report["model_receipts"] = [
                dict(
                    event_id=r["event_id"],
                    parent=r["parent_event_id"],
                    model=r["data"]["model"],
                    request_sha256=r["data"]["request_sha256"],
                )
                for r in received
            ]
            report["route_missing_event"] = wait(
                lambda: next(
                    (
                        r["event_id"]
                        for r in events("model.provider", "model.provider.failed")
                        if r["code"] == "model.route_missing"
                    ),
                    None,
                ),
                "archived unrecognized route",
            )
            report["stale_cloud_event"] = wait(
                lambda: next(
                    (r["event_id"] for r in events("map3d", "map3d.stale") if r["data"]["layer"] == "cloud"),
                    None,
                ),
                "archived stale cloud",
            )
            report["final_2d_grid_stamp"] = wait(grid, "2D grid after map unload")
            check(report["final_2d_grid_stamp"] > report["initial_2d_grid_stamp"], "2D publisher stopped")
            check(original == identities(), "a baseline container was restarted or replaced")
            report.update(
                core_containers=original,
                core_image=args.core_image or args.image,
                image_id=command(["docker", "image", "inspect", args.image, "--format", "{{.Id}}"]),
                passed=True,
            )
        except Exception as ex:
            report["error"] = str(ex)[:380]
            report["diagnostic"] = dc(
                "logs",
                "--no-color",
                "--tail",
                "8",
                "sim",
                "map3d",
                "map3d-sensor",
                "model-fixture",
                "model-provider",
            )[-3000:]
            raise
        finally:
            try:
                dc("--profile", "*", "down", "--volumes", "--remove-orphans", "--timeout", "4")
            finally:
                report["finished_utc"] = datetime.now(timezone.utc).isoformat()
                args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] == ["fixture"]:
        with ThreadingHTTPServer(("0.0.0.0", 11434), LocalModel) as server:
            server.models = []
            server.serve_forever()
    elif sys.argv[1:] == ["raw"]:
        raw_cloud()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--image", required=True)
        parser.add_argument(
            "--core-image", help="optional previously deployed image for the nine baseline services"
        )
        parser.add_argument("--out", required=True, type=Path)
        run(parser.parse_args())
