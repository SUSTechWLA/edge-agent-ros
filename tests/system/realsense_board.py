"""Verify production hardware Compose, wall-clock DDS and two independent RGB-D cameras without USB."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[2]
COLOR, DEPTH, INFO = "color/image_raw", "aligned_depth_to_color/image_raw", "color/camera_info"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def poll(function, description, timeout=25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = function()
            if result:
                return result
        except (OSError, ValueError) as exc:
            last = exc
        time.sleep(.1)
    raise RuntimeError(f"Timed out waiting for {description}: {last}")


def fixture(ports):
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image, CameraInfo

    rclpy.init()
    node = Node("independent_d4xx_hardware_witness")
    http = build_opener(ProxyHandler({}))

    def read(port, path):
        with http.open(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
            return json.load(response)

    streams = {}
    # Independent oracle, including camera namespace, optical frame and expected ROS subscriber.
    for i, (name, prefix, frame, expected_node) in enumerate((
        ("realsense-d4xx", "/camera/camera/", "camera_color_optical_frame", "edge_rgbd_measurement"),
        ("realsense-rear", "/rear/camera/", "rear_color_optical_frame", "edge_rgbd_rear"),
    )):
        topics = [prefix + path for path in (COLOR, DEPTH, INFO)]
        pubs = [node.create_publisher(cls, topic, qos_profile_sensor_data) for cls, topic in zip((Image,Image,CameraInfo), topics)]
        streams[name] = dict(i=i, topics=topics, pubs=pubs, frame=frame, port=ports[i], node=expected_node)
    def discovered():
        rclpy.spin_once(node, timeout_sec=.05)
        return all(p.get_subscription_count() for s in streams.values() for p in s["pubs"])
    poll(discovered, "six DDS subscriptions")
    for stream in streams.values():
        for topic in stream["topics"]:
            subscribers = [e.node_name for e in node.get_subscriptions_info_by_topic(topic)]
            require(subscribers == [stream["node"]], f"Unexpected subscriber on isolated test topic {topic}: {subscribers}")
    require(not node.get_parameter("use_sim_time").value, "fixture unexpectedly follows simulated time")
    require(not node.count_publishers("/clock"), "an unexpected simulation clock shares the isolated domain")
    evidence = {}
    try:
        remaining = set(streams)
        deadline = time.monotonic() + 20
        while remaining and time.monotonic() < deadline:
            for name in tuple(remaining):
                s = streams[name]
                stamp = node.get_clock().now().to_msg()
                stamp_ns = stamp.sec * 10**9 + stamp.nanosec
                require(abs(time.time_ns()-stamp_ns) < 500_000_000, "fixture stamp must be system time")
                frame = s["frame"]
                # Fill each pixel differently so that transposing, using padding or crossing cameras fails.
                width, height = 4, 3
                color = Image(width=width, height=height, encoding="rgb8", step=14)
                depth = Image(width=width, height=height, encoding="16UC1", step=10)
                color.data = b"".join(b"".join(bytes((i+s["i"], j+34, 150+i+j)) for i in range(width)) + b"\xff\xff" for j in range(height))
                depth.data = b"".join(struct.pack("<HHHH", *(1000*(s["i"]+1)+i*100+j*10+7 for i in range(width))) + b"\xee\xee" for j in range(height))
                info = CameraInfo(width=width, height=height, k=[4.,0.,2.,0.,4.,1.,0.,0.,1.])
                for publisher, msg in zip(s["pubs"], (color, depth, info)):
                    msg.header.frame_id, msg.header.stamp = frame, stamp
                    publisher.publish(msg)
                limit = time.monotonic()+.32
                while time.monotonic()<limit:
                    rclpy.spin_once(node, timeout_sec=.02)
                    meta = read(s["port"], "/api/rgbd")
                    if meta.get("color_stamp_ns") == stamp_ns:
                        break
                else:
                    continue
                require(meta["status"]=="online" and meta["frame"]==frame, "wrong stream or optical frame")
                require(meta["depth_encoding"]=="16UC1" and meta["skew_ms"]==0 and
                        meta["depth_stamp_ns"]==meta["info_stamp_ns"]==stamp_ns, "three stamped inputs disagree")
                require(meta["sources"]["alignment"]=="driver_aligned_to_color" and meta["sources"]["id"]==name,
                        "incorrect hardware registration profile")
                require([meta["sources"][key] for key in ("color_topic","aligned_depth_topic","info_topic")]==s["topics"],
                        "wrong ROS names")
                query = urlencode(dict(u=2,v=1,pair=meta["pair_id"]))
                pixel = read(s["port"], "/api/sample?"+query)
                expected_mm = 1217 + s["i"]*1000
                require(pixel["rgb"]==[2+s["i"],35,153] and abs(pixel["depth_m"]*1000-expected_mm)<1e-7
                        and pixel["pair_id"]==meta["pair_id"], "metric sample or RGB mismatch")
                identity = read(s["port"],"/health")
                require(identity["id"]=="rgbd" and identity["started_event"], "failed to identify provider")
                evidence[name] = {"sources":meta["sources"],"frame":frame,"width":meta["width"],"height":meta["height"],
                                  "start_event":identity["started_event"],"ROS_subscriber":s["node"],
                                  "source_stamp_ns":stamp_ns,"depth_encoding":meta["depth_encoding"],"skew_ms":meta["skew_ms"],
                                  "input_depth_mm":expected_mm,"pixel":pixel}
                remaining.remove(name)
        require(not remaining, f"No matching current frame from {sorted(remaining)}")
        require(evidence["realsense-d4xx"]["start_event"] != evidence["realsense-rear"]["start_event"],"same plugin instance")
        return evidence
    finally:
        node.destroy_node()
        rclpy.shutdown()


def run(image, output):
    def command(args, *, env=None, timeout=60, quiet=False):
        result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout,
                                encoding="utf-8", errors="replace")
        if result.returncode and not quiet:
            raise RuntimeError(f"{args[:3]} exited {result.returncode}: {result.stderr[-2400:]} {result.stdout[-1200:]}")
        return result.stdout.strip()
    arch, platform, digest = command(["docker","image","inspect",image,"--format","{{.Architecture}} {{.Os}} {{.Id}}"]).split()
    # Check packaged imports before using the official overlay's read-only bind mounts.
    probe = ("import socket,sys,json;sys.path.insert(0,'/opt/edge/perception/rgbd');import service;"
             "sockets=[socket.socket(),socket.socket()];[s.bind(('127.0.0.1',0)) for s in sockets];"
             "print(json.dumps([s.getsockname()[1] for s in sockets]))")
    ports = json.loads(command(["docker","run","--rm","--network","host",image,"python3","-B","-c",probe]))
    require(len(set(ports))==2 and 8772 not in ports, "isolated loopback ports required")
    domain = 150+secrets.randbelow(40)
    project = "edge-d4xx-board-"+uuid.uuid4().hex[:9]
    proof = dict(image=image, image_id=digest, image_architecture=f"{platform}/{arch}", baked_import=True,
                 ROS_DOMAIN_ID=domain, wall_clock=True, no_usb=True, overlays=["compose.hardware.yaml","compose.rgbd.yaml",
                 "compose.rgbd-hardware.yaml"], tested_at=datetime.now(timezone.utc).isoformat())
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        aux = json.loads((ROOT/"perception/rgbd/config/realsense.json").read_text(encoding="utf-8"))
        for key in ("color_topic","aligned_depth_topic","info_topic"):
            aux[key] = aux[key].replace("/camera/camera/", "/rear/camera/")
        aux["id"]="realsense-rear"
        (directory/"rear.json").write_text(json.dumps(aux),encoding="utf-8")
        extra = directory/"rear.compose.json"
        extra.write_text(json.dumps({"services":{"rgbd":{"command":["python3","-B","/opt/edge/perception/rgbd/service.py",
                                  "--ros-args","-p","use_sim_time:=false","-r","__node:=edge_rgbd_rear"]}}}),encoding="utf-8")
        owners = []
        try:
            for i, port in enumerate(ports):
                env = {**os.environ, "EDGE_HARDWARE_IMAGE":image,"EDGE_ROS_DOMAIN_ID":str(domain),
                       "EDGE_RMW":"rmw_cyclonedds_cpp","EDGE_RGBD_PORT":str(port),
                       "EDGE_RGBD_PROFILE":"rear" if i else "realsense",
                       "EDGE_RGBD_CONFIG_DIR":str(directory if i else ROOT/"perception/rgbd/config")}
                compose = ["docker","compose","-p",project+"-"+str(i)]
                for f in proof["overlays"]:
                    compose += ["-f",f]
                if i:
                    compose += ["-f",str(extra)]
                owners.append((compose,env))
                command(compose+["up","-d","--no-build","rgbd"],env=env)
            cmd = ["docker","run","--rm","--network","host","-e","ROS_DOMAIN_ID="+str(domain),
                   "-e","RMW_IMPLEMENTATION=rmw_cyclonedds_cpp","-v",f"{Path(__file__).resolve()}:/opt/witness/tests/board.py:ro",
                   image,"python3","-B","/opt/witness/tests/board.py","--witness",*(str(x) for x in ports)]
            proof["cameras"] = json.loads(command(cmd,timeout=70))
            proof["passed"] = True
        finally:
            for compose, env in reversed(owners):
                command(compose+["down","--volumes","--timeout","5"],env=env)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(proof,indent=2)+"\n",encoding="utf-8")
    print("PASS: hardware image imports, two independent DDS sources, system timestamps and exact metric pixels")


if __name__=="__main__":
    if sys.argv[1:2]==["--witness"]:
        print(json.dumps(fixture([int(p) for p in sys.argv[2:]])))
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--image",required=True)
        parser.add_argument("--out",type=Path,required=True)
        args=parser.parse_args()
        run(args.image,args.out)
