"""Subscribe to one approved Odometry topic and atomically publish planar position facts."""
import argparse
import json
import os
from pathlib import Path
import time

from edge_contracts.spatial_facts import check_sample, identifier


def position_sample(msg, cfg):
    if msg.header.frame_id != cfg["frame_id"] or msg.child_frame_id != cfg["child_frame_id"]:
        raise ValueError("odometry frame differs from approved binding")
    pose = msg.pose.pose.position
    cov = msg.pose.covariance
    if cov[1] != 0 or cov[6] != 0:
        raise ValueError("source needs a full-covariance provider")
    stamp = msg.header.stamp
    sample = {key: cfg[key] for key in ("actor_ref", "source_id", "clock_id", "calibration_ref",
                                         "localization_epoch", "map_ref", "frame_id")}
    sample.update(kind="position", stamp_ns=stamp.sec * 10**9 + stamp.nanosec,
                  position_m=[pose.x, pose.y], variance_m2=[cov[0], cov[7]])
    check_sample(sample, cfg["actor_ref"])
    return sample


def main():
    import fcntl
    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data

    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--config", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    cfg = json.loads(args.config.read_bytes())
    if cfg["clock_id"] != "unix" or not identifier(cfg["localization_epoch"]):
        raise ValueError("this adapter requires ROS stamps on the Unix clock and an approved epoch")
    args.output.mkdir(parents=True, exist_ok=True)
    writer_lock = (args.output / "writer.lock").open("a")
    fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # A new writer must receive a new continuity epoch, including after an unclean exit.
    epoch_path = args.output / ("writer-" + cfg["localization_epoch"].encode().hex())
    epoch_path.touch(exist_ok=False)
    target = args.output / "observations.json"
    pending = args.output / ("pending-" + str(os.getpid()))
    last_stamp = None
    last_write = 0
    rclpy.init()
    node = rclpy.create_node("edge_spatial_observation_reader", enable_rosout=False,
                             start_parameter_services=False)

    def observe(msg):
        nonlocal last_stamp, last_write
        sample = position_sample(msg, cfg)
        stamp = sample["stamp_ns"]
        if last_stamp is not None and stamp < last_stamp:
            raise ValueError("odometry clock regressed; bind a new epoch")
        last_stamp = stamp
        now = time.monotonic()
        if now - last_write < 0.15:
            return
        last_write = now
        batch = {"schema_version": 1, "basis": "published", "clock_id": cfg["clock_id"],
                 "actor_ref": cfg["actor_ref"], "samples": [sample]}
        pending.write_text(json.dumps(batch, allow_nan=False), encoding="utf8")
        os.replace(pending, target)

    try:
        node.create_subscription(Odometry, cfg["topic"], observe, qos_profile_sensor_data)
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
        writer_lock.close()


if __name__ == "__main__":
    main()
