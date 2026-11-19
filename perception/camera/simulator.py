"""Expose an optional fixed RGB camera in Gazebo on ROS."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim_station import run_station


if __name__ == "__main__":
    run_station(Path(__file__).with_name("station.sdf"), [[
        "ros2", "run", "ros_gz_bridge", "parameter_bridge",
        "/edge/cameras/front/image@sensor_msgs/msg/Image[gz.msgs.Image",
        "/edge/cameras/front/camera_info@sensor_msgs/msg/CameraInfo[gz.msgs.CameraInfo",
        "--ros-args", "-r", "__node:=edge_camera_bridge",
    ]])
