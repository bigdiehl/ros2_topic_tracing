"""Dummy ROS 2 system for trying topic_stats.py and topic_diagnostics.py.

    ros2 launch demo/demo.launch.py                 # robot1 nominal, robot2 lidar_dropout
    ros2 launch demo/demo.launch.py robots:=4       # robot3 slow_camera, robot4 dead_imu
    ros2 launch demo/demo.launch.py diagnostics:=false

Start tracing first: ./trace_ctl.py start   (stop: ./trace_ctl.py stop)
Then any time: ./topic_stats.py -s 5   or   ./topic_stats.py -n '/robot1/*'
"""
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, IncludeLaunchDescription, OpaqueFunction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

DEMO = Path(__file__).resolve().parent
ROOT = DEMO.parent
FAULTS = ["none", "lidar_dropout", "slow_camera", "dead_imu"]


def launch_setup(context):
    count = int(LaunchConfiguration("robots").perform(context))
    diagnostics = LaunchConfiguration("diagnostics").perform(context).lower() == "true"

    robots = []
    for i in range(1, count + 1):
        robots.append(ExecuteProcess(
            cmd=["python3", str(DEMO / "robot.py"), "--ros-args",
                 "-r", f"__ns:=/robot{i}", "-p", f"fault:={FAULTS[(i - 1) % len(FAULTS)]}"],
            name=f"robot{i}", output="screen"))
    robots += [
        Node(package="demo_nodes_cpp", executable="talker", output="screen"),
        Node(package="demo_nodes_py", executable="listener", output="screen"),
    ]

    if not diagnostics:
        return robots
    return robots + [IncludeLaunchDescription(
        PythonLaunchDescriptionSource(str(ROOT / "launch" / "topic_diagnostics.launch.py")),
        launch_arguments={"config": str(DEMO / "diagnostics.yaml")}.items())]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("robots", default_value="2"),
        DeclareLaunchArgument("diagnostics", default_value="true"),
        OpaqueFunction(function=launch_setup),
    ])
