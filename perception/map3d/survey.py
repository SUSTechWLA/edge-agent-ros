"""Expose the simulated survey lidar and its measured points in the odom frame."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim_station import run_station


if __name__ == "__main__":
    run_station(Path(__file__).with_name("survey.sdf"), [
        ["ros2", "run", "tf2_ros", "static_transform_publisher",
         "--z", "1.2", "--frame-id", "odom", "--child-frame-id", "map3d_survey"],
        ["ros2", "run", "ros_gz_bridge", "parameter_bridge",
         "/edge/map3d/survey/points@sensor_msgs/msg/PointCloud2[gz.msgs.PointCloudPacked",
         "--ros-args", "-r", "/edge/map3d/survey/points:=/edge/map3d/points"],
    ])
