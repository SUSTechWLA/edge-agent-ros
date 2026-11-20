"""Read one approved ROS CameraInfo stream and publish its immutable calibration artifact."""
import argparse
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
import tempfile
import time

from edge_contracts.descriptor import canonical
from edge_contracts.spatial_facts import check_sample, identifier, ns, require


MAX_ARTIFACTS = 32


def camera_info(msg, actor, source, frame):
    require(all(identifier(x) for x in (actor, source, frame)) and msg.header.frame_id == frame,
            "sensing.camera_binding_mismatch")
    require(all(type(x) is int and 1 <= x <= 65535 for x in (msg.width, msg.height))
            and identifier(msg.distortion_model), "sensing.invalid_camera_info")
    stamp = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
    require(0 <= msg.header.stamp.nanosec < 10**9 and ns(stamp), "sensing.invalid_camera_time")
    payload = {"schema_version": 1, "type": "sensor_msgs/msg/CameraInfo", "frame_id": frame,
               "width": msg.width, "height": msg.height, "distortion_model": msg.distortion_model}
    for name, size in (("k", 9), ("r", 9), ("p", 12), ("d", None)):
        values = getattr(msg, name)
        require((len(values) == size if size is not None else len(values) <= 32)
                and all(isinstance(x, Real) and not isinstance(x, bool) and math.isfinite(x) for x in values),
                "sensing.invalid_camera_intrinsics")
        payload[name] = [float(x) for x in values]
    require(payload["k"][0] > 0 and payload["k"][4] > 0, "sensing.camera_uncalibrated")
    payload["roi"] = {key: getattr(msg.roi, key) for key in ("x_offset", "y_offset", "width", "height", "do_rectify")}
    payload.update(binning_x=msg.binning_x, binning_y=msg.binning_y)
    data = canonical(payload)
    reference = "sha256:" + hashlib.sha256(data).hexdigest()
    sample = {"kind": "artifact", "actor_ref": actor, "source_id": source, "frame_id": frame, "clock_id": "unix",
              "stamp_ns": stamp, "artifact_ref": reference, "calibration_ref": reference, "media_type": "application/json"}
    check_sample(sample, actor)
    return sample, data


def _atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            pending.unlink(missing_ok=True)
            raise
    try:
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def publish(directory, sample, data):
    """Only the writer holding the site's directory lock may change this private directory."""
    require(directory.resolve(strict=True) == directory.absolute(), "sensing.invalid_camera_directory")
    store = directory / "artifacts"
    store.mkdir(exist_ok=True)
    require(store.resolve(strict=True) == store.absolute(), "sensing.invalid_camera_directory")
    digest = hashlib.sha256(data).hexdigest()
    require(sample["artifact_ref"] == "sha256:" + digest and len(data) <= 8192, "sensing.invalid_camera_artifact")
    path = store / (digest + ".json")
    if path.is_symlink():
        raise ValueError("sensing.invalid_camera_artifact")
    if path.exists():
        require(path.is_file() and path.stat().st_size == len(data) and path.read_bytes() == data,
                "sensing.camera_artifact_conflict")
    else:
        require(len(list(store.iterdir())) < MAX_ARTIFACTS, "sensing.camera_artifact_limit")
        _atomic_write(path, data)
    batch = {"schema_version": 1, "basis": "published", "actor_ref": sample["actor_ref"], "clock_id": "unix",
             "samples": [sample]}
    _atomic_write(directory / "observations.json", json.dumps(batch, allow_nan=False).encode())


def main():
    import fcntl
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.validate_full_topic_name import validate_full_topic_name
    from sensor_msgs.msg import CameraInfo

    cli = argparse.ArgumentParser(description=__doc__)
    for arg in ("actor-ref", "source-id", "topic", "frame-id"):
        cli.add_argument("--" + arg, required=True)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    validate_full_topic_name(args.topic)
    require(all(identifier(x) for x in (args.actor_ref, args.source_id, args.frame_id)), "sensing.invalid_request")
    args.output.mkdir(parents=True, exist_ok=True)
    require(args.output.resolve(strict=True) == args.output.absolute(), "sensing.invalid_camera_directory")
    lock = os.open(args.output, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    rclpy.init()
    node = rclpy.create_node("edge_camera_calibration_reader", enable_rosout=False, start_parameter_services=False)
    last_write = 0.0

    def observe(msg):
        nonlocal last_write
        now = time.monotonic()
        if now - last_write < 0.15:
            return
        last_write = now
        try:
            sample, data = camera_info(msg, args.actor_ref, args.source_id, args.frame_id)
            publish(args.output, sample, data)
        except (OSError, ValueError):
            (args.output / "observations.json").unlink(missing_ok=True)

    try:
        node.create_subscription(CameraInfo, args.topic, observe, qos_profile_sensor_data)
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
        os.close(lock)


if __name__ == "__main__":
    main()
