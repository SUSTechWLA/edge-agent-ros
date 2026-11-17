"""Exercise the hardware image on native CPU or QEMU using isolated ROS inputs."""
import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def check(condition, reason):
    if not condition:
        raise AssertionError(reason)


def exercise():
    import rclpy
    from edge_interfaces.srv import GetMission
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Imu, LaserScan

    services = json.load(sys.stdin)
    processes = {}
    logs = {}
    node = thread = None
    try:
        for name, spec in services.items():
            logs[name] = tempfile.TemporaryFile(mode="w+b")
            processes[name] = subprocess.Popen(spec["command"], env={**os.environ, **spec["environment"]},
                                               stdout=logs[name], stderr=subprocess.STDOUT)
        rclpy.init()
        node = rclpy.create_node("board_input_fixture")
        core = node.create_client(GetMission, "/edge/get_mission")

        def wait(read, reason):
            end = time.monotonic() + 80
            while time.monotonic() < end:
                for name, proc in processes.items():
                    if proc.poll() is not None:
                        logs[name].seek(0)
                        raise RuntimeError(f"{name} exited: {logs[name].read()[-1600:]!r}")
                try:
                    value = read()
                    if value:
                        return value
                except (URLError, TimeoutError, ConnectionError):
                    pass
                time.sleep(.1)
            raise TimeoutError(reason)

        def api(path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = Request("http://127.0.0.1:8765"+path, data, headers={"Content-Type":"application/json"})
            with urlopen(req, timeout=5) as reply:
                return json.load(reply)

        def terminal_task(key):
            def terminal():
                result = api("/agent/tasks?id="+key)
                return result if result["state"] in ("SUCCEEDED", "BLOCKED", "RECOVERY_REQUIRED") else None
            return wait(terminal, "status task")

        wait(lambda: core.wait_for_service(.15), "get_mission service")
        future = core.call_async(GetMission.Request(mission_id="absent"))
        rclpy.spin_until_future_complete(node, future, timeout_sec=8)
        check(future.done() and not future.result().found, "unexpected existing ROS mission")
        wait(lambda: api("/agent/capabilities"), "public Agent endpoint")
        directory = api("/agent/plugins")
        check(directory == {"plugins":[]}, "isolated board unexpectedly declared services")
        try:
            api("/agent/services?name=verify.image")
        except HTTPError as exc:
            check(exc.code == 409 and json.load(exc)["code"] == "plugin.service_missing", "service lookup diagnosis")
        else:
            raise AssertionError("undeployed verifier resolved on board")
        state = api("/api/state")
        check(state["environment"]=="hardware" and not state["sim_world_aligned"], "hardware mode")
        for key in ("odom","scan","imu","truth","pose"):
            check(not state["sources"][key]["available"], f"unexpected initial {key}")
        task_id = "missing-odom"
        api("/agent/tasks", {"id":task_id, "text":"报告当前位置"})
        missing = terminal_task(task_id)
        check(missing["state"] in ("RECOVERY_REQUIRED", "BLOCKED") and not missing["steps"][0].get("evidence"),
              f"missing odom returned {missing['state']} or supplied stale evidence")
        check(any(json.loads(row["payload"]).get("trace",{}).get("code") == "tool.odom_stale"
                  for row in missing["events"]), "missing odometry diagnostic")

        odom = node.create_publisher(Odometry, "/fixture/odom", qos_profile_sensor_data)
        imu = node.create_publisher(Imu, "/fixture/imu", qos_profile_sensor_data)
        scan = node.create_publisher(LaserScan, "/fixture/scan", qos_profile_sensor_data)
        def emit():
            stamp = node.get_clock().now().to_msg()
            o, i, s = Odometry(), Imu(), LaserScan()
            o.header.frame_id, o.child_frame_id = "fixture_odom","fixture_base"
            o.pose.pose.position.x, o.pose.pose.orientation.w = .375, 1.
            i.header.frame_id, s.header.frame_id = "fixture_imu", "fixture_laser"
            s.angle_min, s.angle_increment, s.angle_max = -.1, .1, .1
            s.range_min, s.range_max, s.ranges = .05, 4., [1.25,1.25,1.25]
            for pub, message in ((imu,i),(odom,o),(scan,s)):
                message.header.stamp = stamp
                pub.publish(message)
        node.create_timer(.05, emit)
        thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
        thread.start()
        def sensed():
            sources = api("/api/state")["sources"]
            if all(sources[k]["fresh"] for k in ("odom","imu","scan","pose","perception","map")):
                return sources if 100 in sources["map"]["data"]["cells"] else None
        inputs = wait(sensed, "fresh sensors, fused pose and occupied map")
        check(inputs["odom"]["topic"] == "/fixture/odom" and inputs["pose"]["data"]["x"] == .375, "pose")
        check(inputs["imu"]["data"]["frame"] == "fixture_imu" and inputs["scan"]["data"]["ranges"] == [1.25]*3, "raw values")
        check(inputs["map"]["data"]["frame"] == "fixture_odom" and not inputs["truth"]["available"], "map or ground truth")
        api("/agent/retry", {"id":task_id})
        recovered = terminal_task(task_id)
        check(recovered["state"] == "SUCCEEDED", "status after recovery")
        operation = missing["steps"][0]["operation_id"]
        check(operation and operation == recovered["steps"][0]["operation_id"], "recovery changed the operation")
        evidence = recovered["steps"][0]["evidence"]
        check(evidence["source"] == "/fixture/odom" and evidence["frame"] == "fixture_odom"
              and evidence["x"] == .375 and evidence["stamp_ns"]>0, "measured ROS evidence")
        return {"passed":True, "architecture":platform.machine(), "revision":os.getenv("EDGE_REVISION"),
                "services":list(processes),"core_get_mission_found":future.result().found,
                "runtime_plugins":directory["plugins"],"missing_service":"plugin.service_missing",
                "without_odom":missing["state"], "diagnostic":"tool.odom_stale", "with_odom":recovered["state"],
                "operation_id_before_and_after":operation, "evidence":evidence,
                "occupied_cells":inputs["map"]["data"]["cells"].count(100), "truth_available":inputs["truth"]["available"]}
    finally:
        if rclpy.ok():
            rclpy.shutdown()
        if thread:
            thread.join(timeout=4)
        if node is not None:
            node.destroy_node()
        for p in processes.values():
            p.terminate()
        for log in logs.values():
            log.close()


def launch(image):
    root = Path(__file__).resolve().parents[2]
    host = {k:v for k,v in os.environ.items() if not k.startswith(("EDGE_", "COMPOSE_"))}
    host.update(EDGE_ODOM_TOPIC="/fixture/odom", EDGE_IMU_TOPIC="/fixture/imu", EDGE_SCAN_TOPIC="/fixture/scan",
                EDGE_BASE_FRAME="fixture_base", EDGE_ODOM_FRAME="fixture_odom", EDGE_IMU_FRAME="fixture_imu",
                EDGE_LASER_FRAME="fixture_laser", EDGE_ROS_DOMAIN_ID="154", EDGE_RETRY_LIMIT="0")
    with tempfile.TemporaryDirectory() as folder:
        envfile = Path(folder)/"empty.env"
        envfile.touch()
        config = json.loads(subprocess.check_output(["docker","compose","--env-file",str(envfile),"-f",
            "compose.hardware.yaml","config","--format","json"], cwd=root, env=host))
    services = {name:{k:config["services"][name][k] for k in ("command","environment")}
                for name in ("core","perception","agent","console")}
    info = json.loads(subprocess.check_output(["docker","image","inspect",image]))[0]
    tag = "edge-board-smoke-"+uuid.uuid4().hex[:12]
    try:
        result = subprocess.run(["docker","run","-i","--rm","--init","--name",tag,"--network","none",
            "--platform",f"{info['Os']}/{info['Architecture']}","-e","ROS_DOMAIN_ID=154","-e","RMW_IMPLEMENTATION=rmw_cyclonedds_cpp",
            "-v",f"{Path(__file__).resolve()}:/board_smoke.py:ro",image,"python3","/board_smoke.py","--inside"],
            text=True, input=json.dumps(services), capture_output=True, timeout=300)
        if result.returncode:
            raise RuntimeError(result.stderr[-3000:])
        report = json.loads(result.stdout.splitlines()[-1])
        return {**report,"image_id":info["Id"],"image":image,"host_machine":platform.machine(),"network":"none",
                "fixture":"synthetic ROS 2 wheel odometry, imu and planar laser; no actuator"}
    finally:
        subprocess.run(["docker","rm","-f",tag],capture_output=True)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",default="edge-agent-ros/hardware:v0.1")
    parser.add_argument("--out",type=Path)
    parser.add_argument("--inside",action="store_true",help=argparse.SUPPRESS)
    args=parser.parse_args()
    raw=json.dumps(exercise() if args.inside else launch(args.image))
    if args.out:
        args.out.parent.mkdir(exist_ok=True,parents=True)
        args.out.write_text(raw+"\n",encoding="utf-8")
    print(raw)
