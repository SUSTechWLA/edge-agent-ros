"""Receive an actual ROS image and verify the exported pixels inside the projected hardware runtime."""
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import platform
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib


def exercise():
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image
    from task_recorder import cli, collect, manage
    from task_recorder.service import Archive, Handler

    if not all(callable(component.main) for component in (cli, collect, manage)):
        raise RuntimeError("missing operator entry points in deployment")
    topic = "/field_fixture/camera/color/image_raw"
    pixels = bytes([244,18,9, 25,201,0, 80,22,81, 135,240,99])
    rclpy.init()
    node = rclpy.create_node("recording_release_fixture")
    publisher = node.create_publisher(Image, topic, qos_profile_sensor_data)
    try:
        with tempfile.TemporaryDirectory() as directory, ThreadingHTTPServer(("127.0.0.1",0),Handler) as server:
            url = f"http://127.0.0.1:{server.server_port}"
            server.archive = Archive(Path(directory))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            process = None
            try:
                cli.endpoint(url, "/recordings/start", {"recording_id":"release", "task_id":"rgb", "sources":["rgb"]})
                process = subprocess.Popen([sys.executable, "-B", "-m", "task_recorder.ros_source", "--url", url,
                                            "--recording-id", "release", "--task-id", "rgb", "--camera", topic])
                deadline = time.monotonic()+35
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"camera source exited before data, code {process.returncode}")
                    frame = Image(width=2, height=2, step=6, encoding="rgb8", data=pixels)
                    frame.header.frame_id = "fixture_camera_optical"
                    frame.header.stamp = node.get_clock().now().to_msg()
                    publisher.publish(frame)
                    rclpy.spin_once(node,timeout_sec=.1)
                    if cli.endpoint(url, "/recordings/release")["counts"].get("rgb",0):
                        break
                else:
                    raise TimeoutError("camera recording was empty")
                summary = cli.endpoint(url, "/recordings/release/finish", {"task_id":"rgb", "status":"SUCCEEDED"})
                process.wait(timeout=12)
                if process.returncode or summary["missing_sources"]:
                    raise RuntimeError("incomplete camera recorder shutdown")
                media = Path(directory)/"release"/"media"/"mission"
                items = [json.loads(row) for row in (media/"timeline.jsonl").read_bytes().splitlines()]
                for record in items:
                    data = (media/record["file"]).read_bytes()
                    if (record["kind"]!="rgb" or record["task_id"]!="rgb"
                            or hashlib.sha256(data).hexdigest()!=record["sha256"]
                            or data[:8]!=b"\x89PNG\r\n\x1a\n" or struct.unpack_from(">II", data, 16)!=(2,2)):
                        raise ValueError("exported image provenance, digest or geometry differs")
                    blocks=[]
                    pos=8
                    while pos<len(data):
                        size = int.from_bytes(data[pos:pos+4],"big")
                        if data[pos+4:pos+8]==b"IDAT":
                            blocks.append(data[pos+8:pos+8+size])
                        pos+=12+size
                    if zlib.decompress(b"".join(blocks)) != b"\0"+pixels[:6]+b"\0"+pixels[6:]:
                        raise ValueError("exported camera pixels differ from ROS input")
                return dict(passed=True,machine=platform.machine(),fixture="ROS 2 synthetic sensor_msgs/Image over DDS",
                            accepted_frames=len(items),topic=topic,png_sha256=items[0]["sha256"],
                            source_exit=process.returncode,mode="hardware",network="none",actuators=False)
            finally:
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                server.shutdown()
                thread.join()
                server.archive.close()
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__=="__main__":
    print(json.dumps(exercise()))
