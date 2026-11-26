"""Run the campus perception node and both GZ-to-ROS observation bridges together."""
from launch import LaunchDescription
from launch.actions import ExecuteProcess, Shutdown
from launch_ros.actions import Node
import json
import os
from pathlib import Path


def generate_launch_description():
    params = []
    if extension := os.environ.get("EDGE_CAMPUS_SITE_EXTENSION"):
        grid = json.loads(Path(extension).read_bytes())["grid"]
        for key, value in (("grid_cells",grid["cells"]), ("resolution_m",grid["resolution_m"]),
                           ("grid_origin_x",grid["origin"][0]), ("grid_origin_y",grid["origin"][1])):
            params.extend(["-p",f"{key}:={value}"])
    perception = ExecuteProcess(
        cmd=["python3", "-B", "/opt/edge/perception/node.py", "--ros-args",
             "-p", "map_reset_service:=/sdd058/reset_mapping", *params],
        name="campus_perception",
        output="screen",
        on_exit=[Shutdown(reason="Campus perception exited")],
    )
    bridges = [
        Node(package="ros_gz_bridge", executable="parameter_bridge", name=name,
             parameters=[{"config_file": config}], output="screen",
             on_exit=[Shutdown(reason=f"{name} exited")])
        for name, config in (("edge_campus_truth_bridge", "/fixture-truth.yaml"),
                             ("edge_campus_rgbd_bridge", "/fixture-rgbd.yaml"))
    ]
    return LaunchDescription([perception, *bridges])
