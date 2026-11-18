"""Expose the registered RGB-D projection and body-frame point cloud from one Gazebo station."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim_station import run_station

DIRECTORY = Path(__file__).resolve().parent
STATION = DIRECTORY / "station.sdf"


if __name__ == "__main__":
    model = ET.parse(STATION).getroot().find("model")
    x, y, z, roll, pitch, yaw = model.findtext("pose").split()
    body = model.findtext("link/sensor/gz_frame_id")
    optical = "camera_color_optical_frame"
    tf = ["ros2", "run", "tf2_ros", "static_transform_publisher"]
    bridge = ["ros2", "run", "ros_gz_bridge", "parameter_bridge"]
    run_station(STATION, [
        tf + ["--x", x, "--y", y, "--z", z, "--roll", roll, "--pitch", pitch, "--yaw", yaw,
              "--frame-id", "odom", "--child-frame-id", body],
        tf + ["--qx", "-0.5", "--qy", "0.5", "--qz", "-0.5", "--qw", "0.5",
              "--frame-id", body, "--child-frame-id", optical],
        # Gazebo's XYZ cloud is forward/left/up. Image and intrinsics use the optical axes.
        bridge + ["--ros-args", "-r", "__node:=edge_rgbd_image_bridge", "-p",
                  f"config_file:={DIRECTORY / 'bridge.yaml'}", "-p", "override_frame_id:=" + optical],
        bridge + ["/edge/rgbd_station/points@sensor_msgs/msg/PointCloud2[gz.msgs.PointCloudPacked",
                  "--ros-args", "-r", "__node:=edge_rgbd_cloud_bridge", "-r",
                  "/edge/rgbd_station/points:=/edge/rgbd/points"],
    ])
