"""Publish immutable color previews or lossless full-resolution 16UC1 depth from one ROS Image topic."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import time
import zlib

from edge_contracts.image_frames import MAX_RAW, chunk, dimensions, raw_frame
from edge_contracts.spatial_facts import check_sample, identifier, ns, require
from perception.spatial_facts.artifact_ring import ArtifactRing, replace_bytes
from perception.spatial_facts.gateway import read_source


MAX_PNG = {"color": 1024 * 1024, "depth16": 4 * 1024 * 1024}
MAX_AGE_NS = 1_000_000_000


def depth_png(msg):
    """Encode PNG grayscale samples in wire big-endian order. Preserve zero and all 16-bit values."""
    width, height, step = msg.width, msg.height, msg.step
    dimensions(width, height)
    require(msg.encoding == "16UC1" and msg.is_bigendian in (0, 1), "sensing.depth_encoding_unsupported")
    require(step >= 2 * width and step * height == len(msg.data) and len(msg.data) <= MAX_RAW,
            "sensing.invalid_depth_layout")
    view = memoryview(msg.data)
    compressor = zlib.compressobj(2)
    encoded = []
    for y in range(height):
        row = bytes(view[y * step:y * step + 2 * width])
        if not msg.is_bigendian:
            swapped = bytearray(len(row))
            swapped[::2], swapped[1::2] = row[1::2], row[::2]
            row = swapped
        encoded.append(compressor.compress(b"\x00" + row))
    encoded.append(compressor.flush())
    header = struct.pack(">IIBBBBB", width, height, 16, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", b"".join(encoded)) + chunk(b"IEND", b"")


def frame_sample(msg, actor, source, frame, calibration_dir, calibration_source, now_ns, stream="color"):
    require(identifier(actor) and identifier(source) and identifier(frame) and
            msg.header.frame_id == frame, "sensing.image_binding_mismatch")
    stamp = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
    require(0 <= msg.header.stamp.nanosec < 10**9 and ns(stamp) and 0 <= now_ns - stamp <= MAX_AGE_NS,
            "sensing.image_stamp_invalid")
    require(stream in MAX_PNG, "sensing.invalid_image_stream")
    if stream == "color":
        require(msg.encoding.lower() in ("rgb8", "bgr8"), "sensing.color_encoding_unsupported")
    batch = read_source(calibration_dir / "observations.json", actor, now_ns)
    require(len(batch["samples"]) == 1, "sensing.image_calibration_mismatch")
    cal = batch["samples"][0]
    require(cal["source_id"] == calibration_source and cal["frame_id"] == frame and
            cal["kind"] == "artifact" and cal["media_type"] == "application/json" and
            cal["artifact_ref"] == cal["calibration_ref"] and
            0 <= now_ns - cal["stamp_ns"] <= MAX_AGE_NS, "sensing.image_calibration_mismatch")
    ref = cal["artifact_ref"]
    path = calibration_dir / "artifacts" / (ref[7:] + ".json")
    require(path.resolve(strict=True) == path.absolute() and path.is_file(), "sensing.invalid_calibration_path")
    with path.open("rb") as calibration_file:
        raw = calibration_file.read(8193)
    require(len(raw) <= 8192 and "sha256:" + hashlib.sha256(raw).hexdigest() == ref,
            "sensing.image_calibration_digest")
    binding = json.loads(raw)
    require(isinstance(binding, dict) and binding.get("type") == "sensor_msgs/msg/CameraInfo" and
            (binding.get("width"), binding.get("height"), binding.get("frame_id")) == (msg.width, msg.height, frame),
            "sensing.image_calibration_mismatch")
    png = depth_png(msg) if stream == "depth16" else raw_frame(msg)[0]
    require(len(png) <= MAX_PNG[stream], "sensing.image_artifact_size")
    sample = {"kind": "artifact", "actor_ref": actor, "source_id": source, "frame_id": frame, "clock_id": "unix",
              "stamp_ns": stamp, "calibration_ref": ref, "artifact_ref": "sha256:" + hashlib.sha256(png).hexdigest(),
              "media_type": "image/png"}
    check_sample(sample, actor)
    return sample, png


def main():
    import fcntl
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.validate_full_topic_name import validate_full_topic_name
    from sensor_msgs.msg import Image

    cli = argparse.ArgumentParser(description=__doc__)
    for name in ("actor-ref", "source-id", "topic", "frame-id", "calibration-source"):
        cli.add_argument("--" + name, required=True)
    cli.add_argument("--calibration-dir", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--stream", choices=("color", "depth16"), default="color")
    args = cli.parse_args()
    validate_full_topic_name(args.topic)
    require(all(identifier(x) for x in (args.actor_ref, args.source_id, args.frame_id, args.calibration_source)),
            "sensing.invalid_request")
    args.output.mkdir(parents=True, exist_ok=True)
    require(args.output.resolve(strict=True) == args.output.absolute(), "sensing.invalid_image_directory")
    lock = os.open(args.output, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ring = ArtifactRing(args.output, args.actor_ref, args.source_id, media_type="image/png",
                        suffix=".png", max_bytes=MAX_PNG[args.stream])
    binding = {"schema_version": 1, "actor_ref": args.actor_ref, "source_id": args.source_id, "topic": args.topic,
               "frame_id": args.frame_id, "ros_type": "sensor_msgs/msg/Image", "clock_id": "unix",
               "stream": args.stream, "calibration_source": args.calibration_source}
    replace_bytes(args.output / "source.json", json.dumps(binding).encode())
    rclpy.init()
    node = rclpy.create_node("edge_" + args.stream + "_image_reader", enable_rosout=False, start_parameter_services=False)
    last_read, last_issue = 0.0, 0.0

    def observe(msg):
        nonlocal last_read, last_issue
        now = time.monotonic()
        if now - last_read < 0.25:
            return
        last_read = now
        try:
            sample, data = frame_sample(msg, args.actor_ref, args.source_id, args.frame_id,
                                        args.calibration_dir, args.calibration_source, time.time_ns(), args.stream)
            ring.publish(sample, data)
        except (OSError, ValueError) as error:
            (args.output / "observations.json").unlink(missing_ok=True)
            if now - last_issue >= 5:
                node.get_logger().warning("Image withheld: " + str(error))
                last_issue = now

    try:
        node.create_subscription(Image, args.topic, observe, qos_profile_sensor_data)
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
        os.close(lock)


if __name__ == "__main__":
    main()
